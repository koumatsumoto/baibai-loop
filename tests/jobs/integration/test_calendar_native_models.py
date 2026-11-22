"""Calendar model inputs retain native history and project only safe day evidence."""

import json
import sqlite3
from datetime import date, timedelta

import pytest
from tests.engine.data_collection.providers.jquants.test_calendar_native import (
    AT,
    DAY,
    attempt,
    owner,
)
from tests.helpers.lake_release import market_store

from baibai_engine.data_collection.providers.jquants.calendar_native import (
    HEAD_KEY,
    PREFIX,
    FixedCalendarL1,
)
from baibai_engine.machine_analysis.generation.calendar_inputs import (
    CALENDAR_PROJECTION_REVISION,
    INPUT_PREFIX,
    read_calendar_input,
)
from baibai_engine.machine_analysis.observations.prices.calendar_context import (
    observation_calendar_context,
)
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_engine.storage.sqlite.source_cache.coverage import covered_intervals
from baibai_jobs.integration.calendar_native import (
    fixed_input,
    materialize_saved_input,
    model_input,
)


def rows(market):
    with sqlite3.connect(market) as connection:
        return connection.execute("SELECT * FROM jquants_market_calendar ORDER BY day").fetchall()


def test_saved_input_preserves_all_history_and_future_days_without_latest(tmp_path):
    store, source = owner(tmp_path / "native")
    past, future = date(2025, 1, 1), date(2027, 1, 1)
    source.commit([attempt("past", past), attempt("current"), attempt("future", future)])
    fixed = source.fixed()
    market = market_store(tmp_path / "market.sqlite")
    store.get_keys.clear()
    saved = model_input(store, fixed, as_of=DAY, evaluated_at=AT, market=market)
    expected = [(past.isoformat(), "1"), (DAY.isoformat(), "1"), (future.isoformat(), "1")]
    assert rows(market) == expected
    assert saved.descriptor["summary"]["selection_start"] == past.isoformat()
    assert saved.descriptor["summary"]["selection_end"] == future.isoformat()
    assert saved.descriptor["summary"]["coverage_status"] == "partial"  # Missing dates stay absent.
    assert "scopes" not in saved.descriptor
    packs = [key for key in store.get_keys if key.startswith(PREFIX + "packs/")]
    assert len(packs) == len(set(packs)) == 1
    del store.values[HEAD_KEY]
    market.unlink()
    market_store(market)
    store.get_keys.clear()
    materialize_saved_input(store, saved, market=market)
    assert HEAD_KEY not in store.get_keys
    assert rows(market) == expected


def test_partial_overlap_recovery_does_not_reapply_full_range_failure(tmp_path):
    store, source = owner(tmp_path / "native")
    d2, d3 = DAY + timedelta(days=1), DAY + timedelta(days=2)
    first = attempt("A1", end=d2)
    source.commit([first, attempt("B2", d2, d3, hour=1, partial=True)])
    source.commit([attempt("A3", end=d2, hour=2)])  # Equal body, new confirmation clock.
    saved = fixed_input(store, source.fixed(), as_of=d3, evaluated_at=AT + timedelta(hours=2))
    selected = read_calendar_input(store, saved.descriptor, days=(DAY, d2, d3))
    assert selected["scopes"][d2.isoformat()]["confirmation_id"] == "A3"
    assert "pack" not in selected["receipts"]["A3"]
    assert selected["receipts"]["A1"]["pack"]
    original = next(item for item in source.fixed().requests(store) if item.request_id == "A1")
    assert original.value() == first.value()
    del store.values[HEAD_KEY]
    market = market_store(tmp_path / "market.sqlite")
    result = materialize_saved_input(store, saved, market=market)
    assert rows(market) == [(DAY.isoformat(), "1"), (d2.isoformat(), "1")]
    assert result["failed_days"] == result["unavailable_days"] == 1
    assert result["rows"] == 2
    with sqlite3.connect(market) as connection:
        assert covered_intervals(connection, "jquants_market_calendar") == ((DAY, d2),)


@pytest.mark.parametrize("problem", ["partial", "held", "duplicate", "unknown", "missing"])
def test_new_bad_day_removes_stale_holiday_without_touching_other_sources(tmp_path, problem):
    store, source = owner(tmp_path / "native")
    source.commit([attempt("old", rows=[{"Date": DAY.isoformat(), "HolDiv": "0"}])])
    market = market_store(tmp_path / "market.sqlite")
    model_input(store, source.fixed(), as_of=DAY, evaluated_at=AT, market=market)
    with sqlite3.connect(market) as connection:
        untouched = connection.execute("SELECT * FROM jquants_daily_bars").fetchall()
        other_coverage = connection.execute(
            "SELECT * FROM source_coverage WHERE source!='jquants_market_calendar'"
        ).fetchall()
    new_rows = [{"Date": DAY.isoformat(), "HolDiv": "1"}]
    if problem == "duplicate":
        new_rows.append({"Date": DAY.isoformat(), "HolDiv": "0"})
    elif problem == "unknown":
        new_rows[0]["HolDiv"] = "NEW"
    elif problem == "missing":
        new_rows = []
    source.commit(
        [
            attempt(
                "bad",
                hour=0 if problem == "held" else 1,
                rows=new_rows,
                partial=problem == "partial",
            )
        ]
    )
    saved = model_input(
        store, source.fixed(), as_of=DAY, evaluated_at=AT + timedelta(hours=1), market=market
    )
    assert rows(market) == []
    with sqlite3.connect(market) as connection:
        assert connection.execute("SELECT * FROM jquants_daily_bars").fetchall() == untouched
        assert (
            connection.execute(
                "SELECT * FROM source_coverage WHERE source!='jquants_market_calendar'"
            ).fetchall()
            == other_coverage
        )
        assert covered_intervals(connection, "jquants_market_calendar") == ()
        with pytest.raises(ValueError, match="unavailable"):
            observation_calendar_context(connection, DAY)
    assert saved.descriptor["summary"]["coverage_status"] == "partial"
    if problem == "unknown":
        assert saved.descriptor["summary"]["raw_coverage_status"] == "complete"
        assert source.fixed().plan(start=DAY, end=DAY) == ()


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("division", "0"),
        ("content_id", "b" * 64),
        ("valid", "true"),
        ("confirmed_at", (AT - timedelta(seconds=1)).isoformat()),
    ],
)
def test_current_projection_marks_unproved_day_unavailable_without_changing_raw(
    tmp_path, field, bad
):
    store, source = owner(tmp_path / "native")
    later = DAY + timedelta(days=1)
    source.commit([attempt("first"), attempt("unrelated", later)])
    fixed = source.fixed()
    head = fixed.value()
    raw_ref = head["days"][DAY.isoformat()]["adopted"]
    head["days"][DAY.isoformat()][field] = bad
    dispute = FixedCalendarL1(json.dumps(head).encode(), fixed.etag)
    original = dict(store.values)
    store.get_keys.clear()
    market = market_store(tmp_path / "market.sqlite")
    saved = model_input(store, dispute, as_of=later, evaluated_at=AT, market=market)
    assert saved.descriptor["summary"]["projection_revision"] == CALENDAR_PROJECTION_REVISION
    assert saved.descriptor["summary"]["invalid_days"] == 1
    assert saved.descriptor["summary"]["coverage_status"] == "partial"
    assert rows(market) == [(later.isoformat(), "1")]
    with sqlite3.connect(market) as connection:
        coverage = connection.execute(
            "SELECT status,record_count FROM source_coverage WHERE source=? AND coverage_start=?",
            ("jquants_market_calendar", DAY.isoformat()),
        ).fetchone()
        assert coverage == ("unavailable", 0)
    assert len([key for key in store.get_keys if key.startswith(PREFIX + "packs/")]) == 1
    retained = read_calendar_input(store, saved.descriptor, days=(DAY, later))
    assert retained["scopes"][DAY.isoformat()]["valid"] is False
    assert retained["scopes"][DAY.isoformat()]["adopted"] == raw_ref
    assert retained["receipts"][raw_ref] == head["receipts"][raw_ref]
    assert all(store.values[key] == value for key, value in original.items())
    cold = market_store(tmp_path / "cold.sqlite")
    assert materialize_saved_input(store, saved, market=cold)["invalid_days"] == 1
    assert rows(cold) == rows(market)


def test_earlier_scope_clock_cannot_hide_future_raw_page_or_publish_partial_input(tmp_path):
    store, source = owner(tmp_path / "native")
    source.commit([attempt("initial")])
    market = market_store(tmp_path / "market.sqlite")
    model_input(store, source.fixed(), as_of=DAY, evaluated_at=AT, market=market)
    source.commit([attempt("updated", hour=2, rows=[{"Date": DAY.isoformat(), "HolDiv": "0"}])])
    fixed = source.fixed()
    claimed = fixed.value()
    for field in ("confirmed_at", "last_attempt_at", "changed_at"):
        claimed["days"][DAY.isoformat()][field] = AT.isoformat()
    dispute = FixedCalendarL1(json.dumps(claimed).encode(), fixed.etag)
    store.put_keys.clear()
    with pytest.raises(ValueError, match="later than the evaluation clock"):
        model_input(store, dispute, as_of=DAY, evaluated_at=AT, market=market)
    assert rows(market) == [(DAY.isoformat(), "1")]
    assert not store.put_keys


def test_monthly_metadata_reuses_unchanged_month_and_keeps_old_roots(tmp_path):
    store, source = owner(tmp_path / "native")
    prior = DAY.replace(month=10)
    source.commit([attempt("prior", prior), attempt("current")])
    saved = fixed_input(store, source.fixed(), as_of=DAY, evaluated_at=AT)
    original = dict(store.values)
    root_key = INPUT_PREFIX + "selections/" + saved.descriptor["selection_sha256"] + ".json"
    root = json.loads(store.get_bytes(root_key))
    source.commit([attempt("equal", hour=1)])
    store.put_keys.clear()
    next_saved = fixed_input(store, source.fixed(), as_of=DAY, evaluated_at=AT + timedelta(hours=1))
    current = json.loads(
        store.get_bytes(
            INPUT_PREFIX + "selections/" + next_saved.descriptor["selection_sha256"] + ".json"
        )
    )
    assert root["months"]["2026-10"] == current["months"]["2026-10"]
    assert root["months"]["2026-11"] != current["months"]["2026-11"]
    assert len([key for key in store.put_keys if "/months/" in key]) == 1
    assert all(store.values[key] == value for key, value in original.items() if key != HEAD_KEY)
    assert not any(key.startswith(PREFIX + "packs/") for key in store.put_keys)
    assert (
        read_calendar_input(store, saved.descriptor, days=(DAY,))["scopes"][DAY.isoformat()][
            "confirmed_at"
        ]
        == AT.isoformat()
    )


def test_full_evaluation_clock_not_asof_and_atomic_failure(tmp_path):
    store, source = owner(tmp_path / "native")
    source.commit([attempt("late", hour=2)])
    market = market_store(tmp_path / "market.sqlite")
    with pytest.raises(ValueError, match="evaluation clock"):
        model_input(
            store, source.fixed(), as_of=DAY, evaluated_at=AT + timedelta(hours=1), market=market
        )
    with pytest.raises(ValueError, match="timezone"):
        fixed_input(store, source.fixed(), as_of=DAY, evaluated_at=AT.replace(tzinfo=None))
    saved = model_input(
        store,
        source.fixed(),
        as_of=DAY - timedelta(days=1),
        evaluated_at=AT + timedelta(hours=2),
        market=market,
    )
    assert rows(market) == [(DAY.isoformat(), "1")]
    pack = next(key for key in store.values if key.startswith(PREFIX + "packs/"))
    del store.values[pack]
    with pytest.raises(LakePublishError, match="unavailable"):
        materialize_saved_input(store, saved, market=market)
    assert rows(market) == [(DAY.isoformat(), "1")]


def test_new_projection_requires_healthy_existing_immutable_month_before_sql_replace(tmp_path):
    store, source = owner(tmp_path / "native")
    old = DAY.replace(month=10)
    source.commit([attempt("older month", old), attempt("current")])
    market = market_store(tmp_path / "market.sqlite")
    old_input = model_input(store, source.fixed(), as_of=DAY, evaluated_at=AT, market=market)
    root_key = INPUT_PREFIX + "selections/" + old_input.descriptor["selection_sha256"] + ".json"
    stale = json.loads(store.get_bytes(root_key))["months"]["2026-10"]
    month = store.values[INPUT_PREFIX + "months/" + stale + ".json"]
    # Same declared SHA and byte count, different actual bytes.
    month.body = b"!" + month.body[1:]
    source.commit(
        [attempt("current correction", hour=1, rows=[{"Date": DAY.isoformat(), "HolDiv": "0"}])]
    )
    with pytest.raises(LakePublishError, match="integrity"):
        model_input(
            store, source.fixed(), as_of=DAY, evaluated_at=AT + timedelta(hours=1), market=market
        )
    assert rows(market) == [(old.isoformat(), "1"), (DAY.isoformat(), "1")]


def test_default_full_day_metadata_shape_and_bytes_are_unchanged(tmp_path):
    from tests.helpers.r2_store import MemoryR2Store

    from baibai_engine.machine_analysis.generation.full_day_inputs import (
        FullDayInputContract,
        retain_full_day_input,
    )
    from baibai_engine.primitives.json import canonical_json

    store = MemoryR2Store()
    contract = FullDayInputContract("test/", "input", "selection", "month")
    scopes = {DAY.isoformat(): {"adopted": "a", "confirmed_at": AT.isoformat()}}
    receipt = {"a": {"pack": "b" * 64, "fingerprint": "c" * 64}}
    retain_full_day_input(
        store,
        {
            "scopes": scopes,
            "receipts": receipt,
            "head_sha256": "a" * 64,
            "as_of": DAY.isoformat(),
            "summary": {},
        },
        contract=contract,
    )
    month = next(value.body for key, value in store.values.items() if "/months/" in key)
    assert (
        month
        == canonical_json(
            {"format": "month", "month": "2026-11", "scopes": scopes, "receipts": receipt}
        ).encode()
    )


def test_projection_drops_only_old_calendar_raw_from_disposable_execution_copy(tmp_path):
    import gzip

    store, source = owner(tmp_path / "native")
    source.commit([attempt("native")])
    market = market_store(tmp_path / "market.sqlite")
    with sqlite3.connect(market) as connection:
        for identity, provider in (("old-calendar", "market_calendar"), ("other", "margin_alerts")):
            connection.execute(
                "INSERT INTO jquants_source_pages VALUES (?,?,?,?,?,?,?)",
                (
                    identity,
                    0,
                    provider,
                    "{}",
                    AT.isoformat(),
                    DAY.isoformat(),
                    gzip.compress(b'{"data":[]}'),
                ),
            )
    model_input(store, source.fixed(), as_of=DAY, evaluated_at=AT, market=market)
    with sqlite3.connect(market) as connection:
        assert connection.execute(
            "SELECT request_id,source FROM jquants_source_pages"
        ).fetchall() == [("other", "margin_alerts")]


def test_complete_pages_remain_usable_after_late_transport_error(tmp_path):
    from dataclasses import replace

    store, source = owner(tmp_path / "native")
    completed = replace(attempt("completed"), failure="TransportErrorAfterFinalPage")
    source.commit([completed])
    assert source.fixed().plan(start=DAY, end=DAY) == ()
    saved = fixed_input(store, source.fixed(), as_of=DAY, evaluated_at=AT)
    del store.values[HEAD_KEY]
    market = market_store(tmp_path / "market.sqlite")
    result = materialize_saved_input(store, saved, market=market)
    assert rows(market) == [(DAY.isoformat(), "1")]
    assert result["coverage_status"] == "complete"
