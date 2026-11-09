"""Fixed calendar closures do not manufacture receipts or hide genuine price gaps."""

import hashlib
import json
import shutil
import sqlite3
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pyarrow.parquet as pq
import pytest
from tests.helpers.edinet_native import empty_edinet_input
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.lake_release import market_store
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.calendar_native import (
    CalendarAttempt,
    CalendarL1,
)
from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.price_native import PriceAttempt, PriceL1
from baibai_engine.machine_analysis.generation.calendar_inputs import read_calendar_input
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.price_inputs import read_price_input
from baibai_engine.machine_analysis.generation.query import read_generation
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_engine.storage.snapshots.release import create_release
from baibai_engine.storage.snapshots.writer import sealed_sqlite_snapshot
from baibai_jobs.integration.calendar_native import fixed_input
from baibai_jobs.integration.price_native import model_input, rebuild_saved_input
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.l1_price_export import export_prices_from_native_input
from baibai_jobs.publication.policy import release_policy_for_profile

FIRST = date(2020, 10, 16)
SATURDAY = FIRST + timedelta(days=1)
SUNDAY = FIRST + timedelta(days=2)
LAST = FIRST + timedelta(days=3)
AT = datetime(2026, 11, 4, tzinfo=UTC)
DAYS = tuple(FIRST + timedelta(days=i) for i in range(4))


def price_attempt(day, *, identity=None, at=AT, rows=None, partial=False):
    body = json.dumps(
        {
            "data": [{"Code": "13010", "Date": day.isoformat(), "C": 100}]
            if rows is None
            else rows,
            **({"pagination_key": "next"} if partial else {}),
        }
    ).encode()
    return PriceAttempt(
        identity or day.isoformat(),
        day,
        at,
        (JQuantsPage("daily_bars", {"date": day.isoformat()}, at, body),),
    )


def calendar_attempt(*, identity="calendar", at=AT, rows=None, partial=False):
    if rows is None:
        rows = [
            {"Date": day.isoformat(), "HolDiv": "0" if day in (SATURDAY, SUNDAY) else "1"}
            for day in DAYS
        ]
    body = json.dumps({"data": rows, **({"pagination_key": "next"} if partial else {})}).encode()
    return CalendarAttempt(
        identity,
        FIRST,
        LAST,
        at,
        (
            JQuantsPage(
                "market_calendar", {"from": FIRST.isoformat(), "to": LAST.isoformat()}, at, body
            ),
        ),
    )


def source_fixture(tmp_path):
    store = MemoryR2Store()
    price = PriceL1(store, tmp_path / "price")
    price.initialize()
    price.commit([price_attempt(FIRST), price_attempt(LAST)])
    calendar = CalendarL1(store, tmp_path / "calendar")
    calendar.initialize()
    calendar.commit([calendar_attempt()])
    market = market_store(tmp_path / "market.sqlite", daily_bars=(), short_sale_reports=())
    return store, price, calendar, market


def build(store, price, calendar, market, *, at=AT):
    selected = fixed_input(store, calendar.fixed(), as_of=LAST, evaluated_at=at)
    saved = model_input(
        store, price.fixed(), as_of=LAST, evaluated_at=at, market=market, calendar_input=selected
    )
    return selected, saved


def test_known_closed_unrequested_days_publish_and_rebuild_without_mutable_inputs(
    tmp_path, monkeypatch
):
    store, price, calendar, market = source_fixture(tmp_path)
    selected, saved = build(store, price, calendar, market)
    summary = saved.descriptor["summary"]
    assert summary["coverage_status"] == "complete"
    assert summary["raw_coverage_status"] == "partial"
    assert summary["unavailable_days"] == summary["closed_unrequested_days"] == 2
    assert summary["normal_empty_days"] == 0
    scopes = read_price_input(store, saved.descriptor, days=DAYS)["scopes"]
    proof_digest = scopes[SATURDAY.isoformat()]["closed_calendar"]
    proof = read_calendar_input(
        store,
        {"format": selected.descriptor["format"], "selection_sha256": proof_digest},
        days=(SATURDAY, SUNDAY),
    )
    assert proof == read_calendar_input(store, selected.descriptor, days=(SATURDAY, SUNDAY))
    with sqlite3.connect(market) as connection:
        assert connection.execute(
            "SELECT coverage_start,status,record_count FROM source_coverage "
            "WHERE source='jquants_daily_bars' AND status='unavailable' ORDER BY 1"
        ).fetchall() == [
            (SATURDAY.isoformat(), "unavailable", 0),
            (SUNDAY.isoformat(), "unavailable", 0),
        ]
    mirror = tmp_path / "mirror"
    narrow_release_policy(monkeypatch, datasets=("jquants.daily_bars",))
    with sealed_sqlite_snapshot(sqlite_path=market, mirror_root=mirror) as seal:
        exported = export_prices_from_native_input(
            source_snapshot=seal, saved=saved, mirror_root=mirror, producer_git_commit="a" * 40
        )
    assert exported.manifest.coverage_status == "complete"
    create_release(
        dataset_manifest_paths=[exported.manifest_path],
        mirror_root=mirror,
        release_id="known-closures",
        catalog=LAKE_DATASETS,
        policy=release_policy_for_profile("production"),
    )
    # The later calendar says open. The old price input must still use its old closure proof.
    calendar.commit(
        [
            calendar_attempt(
                identity="correction",
                at=AT + timedelta(hours=1),
                rows=[{"Date": day.isoformat(), "HolDiv": "1"} for day in DAYS],
            )
        ]
    )
    for key in list(store.values):
        if key.endswith("/head.json"):
            del store.values[key]
    market.unlink()
    shutil.rmtree(mirror)
    store.get_keys.clear()
    result = rebuild_saved_input(store, saved, start=FIRST, end=LAST, output=tmp_path / "cold")
    assert result["coverage_status"] == "complete"
    assert result["raw_coverage_status"] == "partial"
    assert result["unavailable"] == result["closed_unrequested"] == 2
    assert len(pq.read_table(tmp_path / "cold/daily_bars.parquet")) == 2
    statuses = pq.read_table(tmp_path / "cold/price_receipt_status.parquet").to_pylist()
    assert sum(row["projection_status"] == "unavailable" for row in statuses) == 2
    assert all(row["l1_coverage_status"] == "partial" for row in statuses)
    assert not any(key.endswith("/head.json") for key in store.get_keys)
    root = "fixed-inputs/jquants-market-calendar/v1/selections/" + proof_digest + ".json"
    del store.values[root]
    with pytest.raises(LakePublishError, match="unavailable"):
        rebuild_saved_input(
            store, saved, start=FIRST, end=LAST, output=tmp_path / "missing-calendar"
        )


def test_unrelated_calendar_month_does_not_retain_old_price_months_again(tmp_path):
    start, end = date(2020, 10, 30), date(2020, 11, 2)
    days = tuple(start + timedelta(days=i) for i in range(4))
    store = MemoryR2Store()
    price = PriceL1(store, tmp_path / "price")
    price.initialize()
    price.commit([price_attempt(start), price_attempt(end)])
    calendar = CalendarL1(store, tmp_path / "calendar")
    calendar.initialize()
    calendar.commit(
        [
            CalendarAttempt(
                "old",
                start,
                end,
                AT,
                (
                    JQuantsPage(
                        "market_calendar",
                        {"from": start.isoformat(), "to": end.isoformat()},
                        AT,
                        json.dumps(
                            {
                                "data": [
                                    {
                                        "Date": day.isoformat(),
                                        "HolDiv": "1" if day in (start, end) else "0",
                                    }
                                    for day in days
                                ]
                            }
                        ).encode(),
                    ),
                ),
            )
        ]
    )
    market = market_store(tmp_path / "market.sqlite")
    old_calendar = fixed_input(store, calendar.fixed(), as_of=end, evaluated_at=AT)
    old_price = model_input(
        store,
        price.fixed(),
        as_of=end,
        evaluated_at=AT,
        market=market,
        calendar_input=old_calendar,
    )
    later, at = date(2020, 12, 1), AT + timedelta(seconds=1)
    calendar.commit(
        [
            CalendarAttempt(
                "new-month",
                later,
                later,
                at,
                (
                    JQuantsPage(
                        "market_calendar",
                        {"from": later.isoformat(), "to": later.isoformat()},
                        at,
                        json.dumps({"data": [{"Date": later.isoformat(), "HolDiv": "1"}]}).encode(),
                    ),
                ),
            )
        ]
    )
    new_calendar = fixed_input(store, calendar.fixed(), as_of=end, evaluated_at=at)
    assert (
        old_calendar.descriptor["selection_sha256"] != new_calendar.descriptor["selection_sha256"]
    )
    store.put_keys.clear()
    new_price = model_input(
        store,
        price.fixed(),
        as_of=end,
        evaluated_at=at,
        market=market,
        calendar_input=new_calendar,
    )
    assert new_price.descriptor["selection_sha256"] == old_price.descriptor["selection_sha256"]
    assert store.put_keys == []


@pytest.mark.parametrize("problem", ["missing", "corrupt"])
def test_cold_price_closure_requires_intact_calendar_original_pack(tmp_path, problem):
    store, price, calendar, market = source_fixture(tmp_path)
    _, saved = build(store, price, calendar, market)
    pack = next(key for key in store.values if "market-calendar" in key and "/packs/" in key)
    for key in list(store.values):
        if key.endswith("/head.json"):
            del store.values[key]
    market.unlink()
    if problem == "missing":
        del store.values[pack]
    else:
        store.values[pack].body = b"corrupt"
    with pytest.raises(LakePublishError, match=r"calendar pack|failed integrity"):
        rebuild_saved_input(store, saved, start=FIRST, end=LAST, output=tmp_path / "cold")


@pytest.mark.parametrize(
    "problem",
    ["open-1", "open-2", "unknown", "blank", "duplicate", "missing", "partial", "failure", "held"],
)
def test_unsafe_calendar_never_hides_an_unrequested_price_day(tmp_path, problem):
    store, price, calendar, market = source_fixture(tmp_path)
    rows = [
        {"Date": day.isoformat(), "HolDiv": "0" if day in (SATURDAY, SUNDAY) else "1"}
        for day in DAYS
    ]
    if problem in {"open-1", "open-2", "unknown", "blank"}:
        rows[1]["HolDiv"] = {"open-1": "1", "open-2": "2", "unknown": "X", "blank": ""}[problem]
    elif problem == "duplicate":
        rows.append(dict(rows[1]))
    elif problem == "missing":
        rows.pop(1)
    elif problem == "held":
        rows[1]["HolDiv"] = "1"
    if problem == "failure":
        calendar.commit(
            [CalendarAttempt("failure", FIRST, LAST, AT + timedelta(hours=1), (), "timeout")]
        )
    else:
        calendar.commit(
            [
                calendar_attempt(
                    identity="bad",
                    at=AT if problem == "held" else AT + timedelta(hours=1),
                    rows=rows,
                    partial=problem == "partial",
                )
            ]
        )
    _, saved = build(store, price, calendar, market, at=AT + timedelta(hours=2))
    assert saved.descriptor["summary"]["coverage_status"] == "partial"
    assert saved.descriptor["summary"]["unavailable_days"] == 2
    scopes = read_price_input(store, saved.descriptor, days=DAYS)["scopes"]
    assert "closed_calendar" not in scopes[SATURDAY.isoformat()]


@pytest.mark.parametrize("problem", ["failed", "partial", "held", "invalid-row"])
def test_price_attempts_on_closed_dates_keep_their_unresolved_state(tmp_path, problem):
    store, price, calendar, market = source_fixture(tmp_path)
    if problem == "failed":
        price.commit([PriceAttempt("failed", SATURDAY, AT, (), "timeout")])
    elif problem == "held":
        price.commit([price_attempt(SATURDAY, rows=[])])
        price.commit([price_attempt(SATURDAY, identity="conflict")])
    else:
        price.commit(
            [
                price_attempt(
                    SATURDAY,
                    rows=[{"Code": "invalid"}] if problem == "invalid-row" else None,
                    partial=problem == "partial",
                )
            ]
        )
    _, saved = build(store, price, calendar, market)
    assert saved.descriptor["summary"]["coverage_status"] == "partial"
    assert saved.descriptor["summary"]["closed_unrequested_days"] == 1
    scopes = read_price_input(store, saved.descriptor, days=DAYS)["scopes"]
    assert "closed_calendar" not in scopes[SATURDAY.isoformat()]
    result = rebuild_saved_input(store, saved, start=FIRST, end=LAST, output=tmp_path / "cold")
    assert result["coverage_status"] == "partial"


def test_without_fixed_calendar_and_old_v1_input_keep_original_gap_meaning(tmp_path):
    store, price, calendar, market = source_fixture(tmp_path)
    saved = model_input(store, price.fixed(), as_of=LAST, evaluated_at=AT, market=market)
    descriptor = {**saved.descriptor, "format": "jquants-price-input-v1"}
    old = InputSnapshot(
        kind="native_l1",
        reference="old",
        descriptor=descriptor,
        sha256=hashlib.sha256(canonical_json(descriptor).encode()).hexdigest(),
    )
    serialized = old.model_dump_json()
    build(store, price, calendar, market)  # A later generation can use confirmed holidays.
    store.get_keys.clear()
    result = rebuild_saved_input(store, old, start=FIRST, end=LAST, output=tmp_path / "old")
    assert result["coverage_status"] == result["raw_coverage_status"] == "partial"
    assert result["unavailable"] == 2
    assert result["closed_unrequested"] == 0
    assert old.model_dump_json() == serialized
    assert not any("calendar" in key for key in store.get_keys)


def test_price_input_rejects_a_later_calendar_clock(tmp_path):
    store, price, calendar, market = source_fixture(tmp_path)
    selected = fixed_input(
        store, calendar.fixed(), as_of=LAST, evaluated_at=AT + timedelta(hours=1)
    )
    with pytest.raises(ValueError, match="later"):
        model_input(
            store,
            price.fixed(),
            as_of=LAST,
            evaluated_at=AT,
            market=market,
            calendar_input=selected,
        )


def test_old_closed_gaps_outside_acquisition_window_reach_normal_daily_and_cold_cli(
    tmp_path, monkeypatch, capsys
):
    from tests.jobs.integration.test_master_native_models import attempt as master_attempt
    from tests.jobs.publication.test_calendar_native_objects import configure

    import baibai_jobs.integration.models as job
    import baibai_jobs.integration.price_native as price_job
    from baibai_engine.data_collection.providers.jquants.financial_native import (
        FinancialAttempt,
        FinancialL1,
    )
    from baibai_engine.data_collection.providers.jquants.master_native import MasterL1
    from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
    from baibai_engine.machine_analysis.screening.policies.economic_paths import EconomicPathPolicy
    from baibai_engine.primitives.market_windows import FINANCIAL_HISTORY_DAYS
    from baibai_engine.storage.snapshots.reader import resolve_current_release

    store, price, calendar, market = source_fixture(tmp_path)
    today = date(2026, 11, 5)
    day = LAST + timedelta(days=1)
    requests = []
    while day <= today:
        requests.append(price_attempt(day, rows=None if day == today else []))
        day += timedelta(days=1)
    price.commit(requests)
    calendar.commit(
        [
            CalendarAttempt(
                "today",
                today,
                today,
                AT,
                (
                    JQuantsPage(
                        "market_calendar",
                        {"from": today.isoformat(), "to": today.isoformat()},
                        AT,
                        json.dumps({"data": [{"Date": today.isoformat(), "HolDiv": "1"}]}).encode(),
                    ),
                ),
            )
        ]
    )
    master = MasterL1(store, tmp_path / "master")
    master.initialize()
    master.commit([master_attempt("master", day=today, at=AT)])
    financial = FinancialL1(store, tmp_path / "financial")
    financial.initialize()
    financial.commit(
        [
            FinancialAttempt(
                "financial",
                today,
                AT,
                (
                    JQuantsPage(
                        "financial_summaries", {"date": today.isoformat()}, AT, b'{"data":[]}'
                    ),
                ),
            )
        ]
    )
    plan = price.fixed().plan(
        start=today - timedelta(days=FINANCIAL_HISTORY_DAYS), end=today, revision_overlap_days=7
    )
    assert len(plan) == 8
    assert SATURDAY not in plan
    assert SUNDAY not in plan
    publication = configure(monkeypatch)
    monkeypatch.setattr(job, "_load_handle", lambda _: None)
    monkeypatch.setattr(job, "verify_held", lambda *_: None)
    economic = tmp_path / "economic.sqlite"
    initialize_database(economic).close()
    saved_edinet = empty_edinet_input(store, tmp_path)
    store.get_keys.clear()
    result = job.integrate_models(
        store=store,
        edinet_input=saved_edinet,
        market=market,
        economic=economic,
        runs=tmp_path / "runs.sqlite",
        mirror=tmp_path / "mirror",
        as_of=today,
        lease_handle=tmp_path / "lease",
        growth_start=None,
        policy=EconomicPathPolicy(candidate_limit=36),
    )
    saved = read_generation(result["generation_id"], path=tmp_path / "runs.sqlite")
    price_input = saved.inputs["price"]
    assert price_input.descriptor["summary"]["coverage_status"] == "complete"
    assert price_input.descriptor["summary"]["raw_coverage_status"] == "partial"
    assert price_input.descriptor["summary"]["closed_unrequested_days"] == 2
    scopes = read_price_input(store, price_input.descriptor, days=(SATURDAY, SUNDAY))["scopes"]
    selected_calendar = read_calendar_input(
        store, saved.inputs["calendar"].descriptor, days=(SATURDAY, SUNDAY)
    )
    for day, scope in scopes.items():
        proof = read_calendar_input(
            store,
            {
                "format": "jquants-market-calendar-input-v1",
                "selection_sha256": scope["closed_calendar"],
            },
            days=(date.fromisoformat(day),),
        )
        assert proof["scopes"][day] == selected_calendar["scopes"][day]
    for key in store.values:
        if key.endswith("/head.json"):
            assert store.get_keys.count(key) == (0 if key.startswith("l1/edinet/") else 1)
    release = resolve_current_release(
        SimpleNamespace(read_bytes=store.get_bytes),
        catalog=publication.LAKE_DATASETS,
        policy=release_policy_for_profile("production"),
    )
    assert release.dataset_manifest("jquants.daily_bars").coverage_status == "complete"
    assert result["models_ready"] is True
    assert result["company_count"] == 1
    source_input = tmp_path / "saved-price.json"
    source_input.write_text(price_input.model_dump_json())
    for key in list(store.values):
        if key.endswith("/head.json") or key.startswith("lake/"):
            del store.values[key]
    market.unlink()
    (tmp_path / "runs.sqlite").unlink()
    shutil.rmtree(tmp_path / "mirror")
    store.get_keys.clear()
    store.close = lambda: None
    monkeypatch.setattr(price_job, "Boto3R2Store", lambda **_: store)
    capsys.readouterr()
    assert (
        price_job.main(
            [
                "--saved-input",
                str(source_input),
                "--work-directory",
                str(tmp_path / "cold-work"),
                "--start",
                FIRST.isoformat(),
                "--end",
                today.isoformat(),
                "--output",
                str(tmp_path / "cold-cli"),
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["coverage_status"] == "complete"
    assert output["unavailable"] == output["closed_unrequested"] == 2
    assert output["raw_coverage_status"] == "partial"
    assert output["rows"] == 3
    assert not any(key.endswith("/head.json") or key.startswith("lake/") for key in store.get_keys)
