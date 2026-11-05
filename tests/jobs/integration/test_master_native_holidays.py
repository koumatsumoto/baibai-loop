"""Only confirmed closed sessions adjust the master consumer, never L1 acquisition."""

import json
import sqlite3
from datetime import date, timedelta

import pytest
from tests.helpers.lake_policy import narrow_release_policy
from tests.jobs.integration.test_master_native_models import AT, attempt, records, setup_native

from baibai_engine.machine_analysis.generation.master_inputs import read_master_input
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.machine_analysis.observations.stocks.reader import (
    read_eq_master_asof,
    read_eq_master_exact,
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
        assert read_eq_master_asof(market, SUNDAY).status == "prior_snapshot"
        assert read_eq_master_exact(market, SUNDAY) is None
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


@pytest.mark.parametrize("response", ["empty", "prior-date", "matching-date"])
def test_normal_holiday_generation_preserves_asof_but_reads_business_master(
    tmp_path, monkeypatch, response
):
    import baibai_engine.machine_analysis.generation.inputs as generation
    import baibai_jobs.integration.models as job
    import baibai_jobs.publication.snapshot_publication as publication
    from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
    from baibai_engine.data_collection.providers.jquants.price_native import PriceAttempt, PriceL1
    from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
    from baibai_engine.machine_analysis.generation.store import ModelGenerationStore
    from baibai_engine.machine_analysis.screening.policies.economic_paths import EconomicPathPolicy
    from baibai_jobs.publication.datasets import LAKE_DATASETS

    monkeypatch.setenv("BAIBAI_MASTER_NATIVE", "true")
    monkeypatch.setenv("BAIBAI_PRICE_NATIVE", "true")
    names = ("jquants.master_snapshots", "jquants.daily_bars")
    narrow_release_policy(monkeypatch, datasets=names)
    monkeypatch.setattr(publication, "LAKE_DATASETS", {name: LAKE_DATASETS[name] for name in names})
    monkeypatch.setattr(publication, "lake_verified_git_commit", lambda: "a" * 40)
    monkeypatch.setattr(job, "_load_handle", lambda _path: None)
    monkeypatch.setattr(job, "verify_held", lambda *_args: None)
    raw = [] if response == "empty" else records(FRIDAY if response == "prior-date" else SUNDAY)
    store, _owner, market = setup_native(
        tmp_path, [attempt("business", day=FRIDAY), attempt("holiday", day=SUNDAY, rows=raw)]
    )
    calendar(market)
    price = PriceL1(store, tmp_path / "price")
    price.initialize()
    price.commit(
        [
            PriceAttempt(
                str(day),
                day,
                AT,
                (
                    JQuantsPage(
                        "daily_bars",
                        {"date": day.isoformat()},
                        AT,
                        json.dumps(
                            {
                                "data": [{"Code": "13010", "Date": day.isoformat(), "C": 100}]
                                if day == FRIDAY
                                else []
                            }
                        ).encode(),
                    ),
                ),
            )
            for day in (FRIDAY, SATURDAY, SUNDAY)
        ]
    )
    original = generation.read_master_snapshot
    consumed = []

    def read(*args, **kwargs):
        consumed.append(kwargs["as_of"])
        return original(*args, **kwargs)

    monkeypatch.setattr(generation, "read_master_snapshot", read)
    economic = tmp_path / "economic.sqlite"
    initialize_database(economic).close()
    result = job.integrate_models(
        store=store,
        market=market,
        economic=economic,
        runs=tmp_path / "runs",
        mirror=tmp_path / "mirror",
        as_of=SUNDAY,
        lease_handle=tmp_path / "lease",
        growth_start=None,
        policy=EconomicPathPolicy(candidate_limit=36),
    )
    saved = ModelGenerationStore(tmp_path / "runs").read(result["generation_id"])
    assert saved.as_of == SUNDAY
    assert consumed == [FRIDAY]
    assert saved.inputs["master"].descriptor["as_of"] == SUNDAY.isoformat()
    assert saved.inputs["master"].descriptor["summary"]["master_basis_date"] == FRIDAY.isoformat()
    assert "lake/pointers/market/current.json" in store.values
