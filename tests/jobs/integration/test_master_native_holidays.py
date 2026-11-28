"""Only confirmed closed sessions adjust the master consumer, never L1 acquisition."""

import sqlite3
from datetime import date, timedelta

import pytest
from tests.jobs.integration.test_master_native_models import AT, attempt, records, setup_native

from baibai_engine.machine_analysis.generation.master_inputs import read_master_input
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.machine_analysis.observations.stocks.reader import (
    read_master_snapshot,
)
from baibai_engine.storage.sqlite.source_cache.coverage import record_source_coverage
from baibai_jobs.integration.master_native import materialize_saved_input, model_input

FRIDAY = date(2026, 10, 2)
SATURDAY = FRIDAY + timedelta(days=1)
SUNDAY = SATURDAY + timedelta(days=1)


def calendar(market, *, status="ok", missing=False, division="0"):
    with sqlite3.connect(market) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO jquants_market_calendar VALUES (?,?)",
            [(FRIDAY.isoformat(), "1"), (SUNDAY.isoformat(), division)]
            + ([] if missing else [(SATURDAY.isoformat(), "3")]),
        )
        record_source_coverage(
            conn,
            source="jquants_market_calendar",
            coverage_key="fixture",
            coverage_start=FRIDAY.isoformat(),
            coverage_end=SUNDAY.isoformat(),
            record_count=2 if missing else 3,
            status=status,
        )


@pytest.mark.parametrize("response", ["empty", "prior-date", "matching-date"])
def test_known_closed_master_selects_business_basis_and_cold_replays_without_calendar(
    tmp_path, response
):
    holiday_rows = (
        [] if response == "empty" else records(FRIDAY if response == "prior-date" else SUNDAY)
    )
    store, owner, market = setup_native(
        tmp_path,
        [attempt("business", day=FRIDAY), attempt("holiday", day=SUNDAY, rows=holiday_rows)],
    )
    calendar(market)
    saved = model_input(store, owner.fixed(), as_of=SUNDAY, evaluated_at=AT, market=market)
    assert saved.descriptor["as_of"] == SUNDAY.isoformat()
    summary = saved.descriptor["summary"]
    assert summary["master_basis_date"] == FRIDAY.isoformat()
    assert summary["snapshot_date"] == FRIDAY.isoformat()
    assert summary["closed_snapshot_days"] == 1
    assert summary["invalid_days"] == 0
    selected = read_master_input(store, saved.descriptor, days=(FRIDAY, SUNDAY))
    assert selected["scopes"][SUNDAY.isoformat()]["closed_basis"] == FRIDAY.isoformat()
    if response != "matching-date":
        assert read_master_snapshot(market, as_of=SUNDAY).as_of == FRIDAY
    with sqlite3.connect(market) as conn:
        conn.execute("DELETE FROM jquants_market_calendar")
        conn.execute("DELETE FROM source_coverage")
        conn.execute("DELETE FROM jquants_master_snapshots")
    from baibai_engine.data_collection.providers.jquants.master_native import HEAD_KEY

    del store.values[HEAD_KEY]
    store.get_keys.clear()
    materialize_saved_input(store, saved, market=market, days=(FRIDAY, SUNDAY), replace_all=True)
    assert HEAD_KEY not in store.get_keys
    if response != "matching-date":
        assert read_master_snapshot(market, as_of=SUNDAY).as_of == FRIDAY
        with sqlite3.connect(market) as conn:
            error = conn.execute(
                "SELECT error FROM source_coverage WHERE source='jquants_master_snapshots' AND coverage_start=?",
                (SUNDAY.isoformat(),),
            ).fetchone()[0]
        assert error.startswith("closed master request: ")


@pytest.mark.parametrize("state", ["unknown", "partial", "gap", "no-coverage", "business"])
def test_unconfirmed_or_business_calendar_does_not_excuse_invalid_master(tmp_path, state):
    store, owner, market = setup_native(
        tmp_path, [attempt("business", day=FRIDAY), attempt("holiday", day=SUNDAY, rows=[])]
    )
    calendar(
        market,
        status="partial" if state == "partial" else "ok",
        missing=state == "gap",
        division="99" if state == "unknown" else "1" if state == "business" else "0",
    )
    if state == "no-coverage":
        with sqlite3.connect(market) as conn:
            conn.execute("DELETE FROM source_coverage WHERE source='jquants_market_calendar'")
    with pytest.raises(JQuantsProviderError, match="empty"):
        model_input(store, owner.fixed(), as_of=SUNDAY, evaluated_at=AT, market=market)


def test_closed_invalid_cannot_hide_a_previous_business_invalid_boundary(tmp_path):
    monday = SUNDAY + timedelta(days=1)
    store, owner, market = setup_native(
        tmp_path,
        [
            attempt("older", day=FRIDAY - timedelta(days=1)),
            attempt("bad-business", day=FRIDAY, rows=[]),
            attempt("bad-closed", day=SUNDAY, rows=[]),
        ],
    )
    calendar(market)
    with pytest.raises(JQuantsProviderError, match="empty"):
        model_input(store, owner.fixed(), as_of=monday, evaluated_at=AT, market=market)
