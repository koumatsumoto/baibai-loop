"""固定日付の再投影は原本選択を一度読み、未充足 coverage を一度確定する。"""

import sqlite3
from collections import Counter
from datetime import timedelta
from importlib import import_module

import pytest
from tests.helpers.margin_alerts_native import AT, DAY
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_engine.storage.sqlite.source_cache.coverage import range_covered


@pytest.fixture(params=["margin_alerts", "short_sale_reports"])
def source(request):
    name = request.param
    provider = import_module(f"baibai_engine.data_collection.providers.jquants.{name}_native")
    integration = import_module(f"baibai_jobs.integration.{name}_native")
    helpers = import_module(f"tests.helpers.{name}_native")
    owner_type = provider.MarginAlertsL1 if name == "margin_alerts" else provider.ShortSaleReportsL1
    return name, provider, integration, helpers, owner_type


def snapshot(path, source_name):
    with sqlite3.connect(path) as connection:
        return (
            connection.execute(f"SELECT * FROM jquants_{source_name} ORDER BY 1,2,3").fetchall(),
            connection.execute(
                "SELECT * FROM source_coverage WHERE source=? ORDER BY coverage_start",
                (f"jquants_{source_name}",),
            ).fetchall(),
        )


@pytest.mark.parametrize("state", ["populated", "empty", "partial", "missing"])
def test_fixed_projection_reuses_selection_and_finalizes_coverage_once(
    tmp_path, monkeypatch, source, state
):
    name, provider, integration, helpers, owner_type = source
    store = MemoryR2Store()
    owner = owner_type(store, tmp_path / "native")
    owner.initialize()
    first = DAY - timedelta(days=2)
    middle = first + timedelta(days=1)
    owner.commit(
        [
            helpers.attempt("first", day=first, rows=[] if state == "empty" else None),
            helpers.attempt("last", rows=[] if state == "empty" else None),
            *([] if state == "missing" else [helpers.attempt("empty", day=middle, rows=[])]),
        ]
    )
    evaluated_at = AT + timedelta(hours=1)
    if state == "partial":
        owner.commit([helpers.attempt("failed-refresh", at=evaluated_at, partial=True)])
    fixed = owner.fixed()
    unresolved = []
    record = integration.record_range_source_coverage

    def record_once(*args, **kwargs):
        if kwargs["status"] in {"partial", "unavailable"}:
            unresolved.append((kwargs["requested_start"], kwargs["status"]))
        return record(*args, **kwargs)

    monkeypatch.setattr(integration, "record_range_source_coverage", record_once)
    market = tmp_path / "market.sqlite"
    saved = integration.model_input(
        store,
        fixed,
        as_of=DAY,
        evaluated_at=evaluated_at,
        market=market,
        mirror_root=tmp_path / "mirror",
    )
    expected_unresolved = (
        [(DAY, "partial")]
        if state == "partial"
        else [(middle, "unavailable")]
        if state == "missing"
        else []
    )
    assert unresolved == expected_unresolved
    expected = snapshot(market, name)
    assert len(expected[0]) == (0 if state == "empty" else 2)
    with sqlite3.connect(market) as connection:
        for day in (first, middle, DAY):
            assert range_covered(connection, f"jquants_{name}", day, day) == (
                day not in {item[0] for item in expected_unresolved}
            )
    identity = saved.model_dump_json()

    # A newer correction cannot replace the saved selection, even with no current head.
    owner.commit(
        [
            helpers.attempt(
                "later",
                at=AT + timedelta(days=1),
                rows=helpers.records() if state == "empty" else [],
            )
        ]
    )
    del store.values[provider.HEAD_KEY]
    store.get_keys.clear()
    store.head_keys.clear()
    unresolved.clear()
    cold = tmp_path / "cold.sqlite"
    result = integration.materialize_saved_input(store, saved, output=cold)

    assert snapshot(cold, name) == expected
    assert unresolved == expected_unresolved
    assert saved.model_dump_json() == identity
    assert result == {
        "rows": len(expected[0]),
        "publication_days" if name == "margin_alerts" else "disclosure_days": 3,
        "unavailable_days": int(state == "missing"),
        "partial_days": int(state == "partial"),
        "coverage_status": "partial" if expected_unresolved else "complete",
        "input_sha256": saved.sha256,
    }
    for requests in (store.get_keys, store.head_keys):
        metadata = Counter(key for key in requests if "/selections/" in key or "/months/" in key)
        assert len(metadata) == 3  # One selection plus October and November metadata.
        assert set(metadata.values()) == {1}
        assert provider.HEAD_KEY not in requests


def test_projection_failure_rolls_back_existing_rows_and_coverage(tmp_path, source):
    name, provider, integration, helpers, owner_type = source
    store = MemoryR2Store()
    owner = owner_type(store, tmp_path / "native")
    owner.initialize()
    first = DAY - timedelta(days=2)
    owner.commit([helpers.attempt("original", day=first)])
    market = tmp_path / "market.sqlite"
    integration.model_input(
        store,
        owner.fixed(),
        as_of=DAY,
        evaluated_at=AT,
        market=market,
        mirror_root=tmp_path / "mirror",
    )
    expected = snapshot(market, name)
    later = AT + timedelta(hours=1)
    owner.commit([helpers.attempt("changed", at=later)])
    fixed = owner.fixed()
    saved = integration.fixed_input(store, fixed, start=first, end=DAY, evaluated_at=later)
    pack = fixed.value()["receipts"]["changed"]["pack"]
    del store.values[provider.PREFIX + "packs/" + pack + ".json.gz"]

    with pytest.raises(LakePublishError, match="pack is unavailable"):
        integration.model_input(
            store,
            fixed,
            as_of=DAY,
            evaluated_at=later,
            market=market,
            mirror_root=tmp_path / "mirror",
        )
    assert snapshot(market, name) == expected
    cold = tmp_path / "failed.sqlite"
    with pytest.raises(LakePublishError, match="pack is unavailable"):
        integration.materialize_saved_input(store, saved, output=cold)
    assert not cold.exists()
