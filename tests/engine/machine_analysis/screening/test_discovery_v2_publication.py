"""旧索引と、一部を取り去っても自己整合して見える新索引を全readerで対照する。"""

import gzip
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from hashlib import sha256

import pytest
from tests.engine.machine_analysis.screening.test_discovery_index import fixed

from baibai_engine.investment_judgment.workflow.case_screening import read_case_screening
from baibai_engine.machine_analysis.observations.prices.price_observation import NativePriceSession
from baibai_engine.machine_analysis.screening.discovery_contract import (
    DISCOVERY_V2,
    DiscoveryPolicyV2,
    DiscoverySelectionV2,
    discovery_publication_id,
)
from baibai_engine.machine_analysis.screening.discovery_index import read_discovery_index
from baibai_engine.machine_analysis.screening.selection_query import read_publication
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.primitives.json import encode_json
from baibai_engine.primitives.time import JST


def editions(path):
    meta, _, old, old_id = fixed(path)
    identity = discovery_publication_id(meta.generation_id, DISCOVERY_V2)
    session = NativePriceSession(
        expected_date=meta.as_of, recorded_at=meta.evaluation_time, status="ok"
    )
    rows = tuple(
        {
            **{
                k: v
                for k, v in row.items()
                if k not in {"price", "basis", "price_ready", "price_date", "expected_price_date"}
            },
            "execution_price": {"raw_close_yen": 510.0, "day_state": "mixed"},
        }
        for row in old.result.rows
    )
    new = replace(
        old,
        result=DiscoverySelectionV2(DiscoveryPolicyV2(method=DISCOVERY_V2), rows, {}, session),
        price_inputs=None,
    )
    return meta, old_id, old, identity, new


def test_exact_v1_v2_coexist_unchanged_in_finite_case_and_latest(tmp_path):
    path = tmp_path / "runs"
    _, old_id, old, identity, new = editions(path)
    store = ScreeningSelectionStore(path)
    for key, body in ((old_id, old), (identity, new)):
        assert store.publish(key, body)
    cutoff = datetime(2030, 1, 1, tzinfo=UTC)
    with sqlite3.connect(path) as conn:
        wire = gzip.decompress(
            conn.execute(
                "select payload_gzip from screening_selection where selection_id=?", (old_id,)
            ).fetchone()[0]
        )
    assert (
        sha256(wire).hexdigest()
        == "7f234ba481f9ecd8339dfd57462884d054f3a9d34aff34fc6e5052bb4af9c741"
    )
    original = path.read_bytes()
    for key, source in ((identity, new), (old_id, old)):
        index = read_discovery_index(path=path, selection_id=key, known_at=cutoff)
        assert index.rows == source.result.rows
        assert index.universe_tickers == old.universe_tickers
        target = index.rows[1]["ticker"]
        _, case = read_case_screening(key, path=path, known_at=cutoff, subjects=(target,))
        assert case.result.rows == (index.rows[1],)
        assert case.universe_tickers == index.universe_tickers
        assert case.legacy_candidate_count is None
        if key == identity:
            assert index.price_session == new.result.price_session == case.result.price_session
            assert "price_ready" not in case.result.rows[0]
        else:
            assert case.source_sha256 == sha256(wire).hexdigest()
            assert index.price_session is None
    assert read_discovery_index(path=path, known_at=cutoff).selection_id == identity
    assert path.read_bytes() == original


def test_v2_writer_and_all_exact_readers_compare_against_independent_stock_plan(tmp_path):
    path = tmp_path / "runs"
    _, old_id, old, identity, new = editions(path)
    store = ScreeningSelectionStore(path)
    assert store.publish(old_id, old)
    # Leave a genuine non-first positive and drop one company from BOTH self-reported sets.
    shortened = replace(
        new,
        universe_tickers=new.universe_tickers[:-1],
        result=replace(new.result, rows=new.result.rows[:-1]),
    )
    assert any(
        "profit_growth" in row["matched_paths"]["growth"] for row in shortened.result.rows[1:]
    )
    with pytest.raises(ValueError, match="saved stock plan"):
        store.publish(identity, shortened)
    assert store.publish(identity, new)
    with sqlite3.connect(path) as conn:
        doc = json.loads(
            gzip.decompress(
                conn.execute(
                    "select payload_gzip from screening_selection where selection_id=?", (identity,)
                ).fetchone()[0]
            )
        )
        doc["result"]["rows"].pop()
        doc["universe_tickers"].pop()
        conn.execute(
            "update screening_selection set payload_gzip=? where selection_id=?",
            (gzip.compress(encode_json(doc).encode()), identity),
        )
    cutoff = datetime(2030, 1, 1, tzinfo=UTC)
    for read in (
        lambda: store.read(identity),
        lambda: read_discovery_index(path=path),
        lambda: read_publication(path=path, selection_id=identity),
        lambda: read_case_screening(identity, path=path, known_at=cutoff, subjects=("1234",)),
    ):
        with pytest.raises(ValueError, match="saved stock plan"):
            read()
    assert store.read(old_id) == old


@pytest.mark.parametrize(
    "damage",
    [
        "unknown_id",
        "wrong_body",
        "future_receipt",
        "pre_session",
        "pre_empty",
        "no_clock",
    ],
)
def test_latest_broken_edition_never_falls_back_to_earlier_v1(tmp_path, damage):
    path = tmp_path / "runs"
    meta, old_id, old, identity, new = editions(path)
    store = ScreeningSelectionStore(path)
    assert store.publish(old_id, old)
    assert store.publish(identity, new)
    with sqlite3.connect(path) as conn:
        if damage == "unknown_id":
            bad_id = identity.removesuffix("v2") + "v3"
            conn.execute(
                "update screening_selection set selection_id=? where selection_id=?",
                (bad_id, identity),
            )
            identity = bad_id
        elif damage == "wrong_body":
            conn.execute(
                "update screening_selection set payload_gzip=(select payload_gzip from screening_selection where selection_id=?) where selection_id=?",
                (old_id, identity),
            )
        else:
            doc = json.loads(
                gzip.decompress(
                    conn.execute(
                        "select payload_gzip from screening_selection where selection_id=?",
                        (identity,),
                    ).fetchone()[0]
                )
            )
            session = doc["result"]["price_session"]
            midnight = datetime.combine(meta.as_of, time.min, JST)
            if damage in {"pre_session", "pre_empty"}:
                session["recorded_at"] = (midnight - timedelta(microseconds=1)).isoformat()
                if damage == "pre_empty":
                    session["status"] = "empty"
                    for row in doc["result"]["rows"]:
                        row["execution_price"] = {"raw_close_yen": None, "day_state": None}
            elif damage == "no_clock":
                session["recorded_at"] = None
            else:
                session["recorded_at"] = "2035-06-26T00:00:00Z"
            conn.execute(
                "update screening_selection set payload_gzip=? where selection_id=?",
                (gzip.compress(encode_json(doc).encode()), identity),
            )
    cutoff = datetime(2030, 1, 1, tzinfo=UTC)
    for read in (
        lambda: read_discovery_index(path=path),
        lambda: read_discovery_index(path=path, selection_id=identity),
        lambda: read_publication(path=path),
        lambda: read_case_screening(identity, subjects=("5678",), known_at=cutoff, path=path),
    ):
        with pytest.raises(
            ValueError, match=r"version|identity|validation|cutoff|before its JST session"
        ):
            read()
    assert read_discovery_index(path=path, selection_id=old_id).rows == old.result.rows


def test_publish_checks_the_same_jst_cutoff_as_readers_before_saving(tmp_path):
    path = tmp_path / "runs"
    meta, old_id, old, key, selection = editions(path)
    store = ScreeningSelectionStore(path)
    assert store.publish(old_id, old)
    pre_session = datetime.combine(meta.as_of, time.min, JST) - timedelta(microseconds=1)
    price_session = selection.result.price_session.model_copy(update={"recorded_at": pre_session})
    invalid = replace(selection, result=replace(selection.result, price_session=price_session))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="before its JST session"):
        store.publish(key, invalid)
    assert path.read_bytes() == before
    assert read_discovery_index(path=path, selection_id=old_id).rows == old.result.rows


@pytest.mark.parametrize(
    ("status", "days_after", "observed"),
    [
        ("ok", 0, True),
        ("ok", 1, True),
        ("partial", -1, False),
        ("unconfirmed", None, False),
    ],
)
def test_v2_finite_case_observes_jst_session_even_without_ta(
    tmp_path, status, days_after, observed
):
    path = tmp_path / "runs"
    meta, _, _, key, selection = editions(path)
    expected = meta.as_of - timedelta(days=1)
    midnight = datetime.combine(expected, time.min, JST).astimezone(UTC)
    assert midnight.date() < expected
    receipt = None if days_after is None else midnight + timedelta(days=days_after)
    session = NativePriceSession(expected_date=expected, status=status, recorded_at=receipt)
    selection = replace(selection, result=replace(selection.result, price_session=session))
    assert ScreeningSelectionStore(path).publish(key, selection)
    with sqlite3.connect(path) as conn:
        (payload,) = conn.execute(
            "select payload_gzip from screening_selection where selection_id=?", (key,)
        ).fetchone()
    clock = json.loads(gzip.decompress(payload))["result"]["price_session"]["recorded_at"]
    assert (datetime.fromisoformat(clock) if clock is not None else None) == receipt
    _, case = read_case_screening(
        key, subjects=("5678",), known_at=datetime(2030, 1, 1, tzinfo=UTC), path=path
    )
    (row,) = case.result.rows
    assert row["technical_state"] == "unavailable"
    assert row["execution_price"] == {"day_state": "mixed", "raw_close_yen": 510.0}
    assert case.result.price_session == session
    assert session.observed(row["execution_price"]) is observed
