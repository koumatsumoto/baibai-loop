import gzip
import json
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.external_assessment_stages import wire_success
from tests.helpers.external_assessment_universe import build_master_lake
from tests.helpers.jquants_receipts import Session, body, receive
from tests.helpers.screening_sqlite import make_master_records

import baibai_jobs.acquisition.external_assessments.daily as daily
from baibai_engine.data_collection.providers.jquants.dependencies import read_dependency
from baibai_engine.data_collection.providers.jquants.http import JQuantsHTTP
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.machine_analysis.observations.jquants_projection import project_request
from baibai_engine.storage.snapshots.keys import current_release_pointer_key
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.dependencies import load_dependency
from baibai_jobs.acquisition.economic_staging import save_handoff
from baibai_jobs.acquisition.inputs import export_source_input
from baibai_jobs.integration.source_handoff import _export
from baibai_jobs.maintenance.source_refresh.jquants import JQuantsRefresh

DAY = date(2026, 10, 19)
PREVIOUS = date(2026, 10, 16)
RECEIVED = datetime(2026, 10, 19, 7, 43, tzinfo=UTC)


def stage_dependencies(
    store, root, *, source="jquants-market", fault=None, rest_day=False, basic_calendar=True
):
    database = root / "source.sqlite"
    query_day = PREVIOUS if fault == "old" else DAY
    record_day = PREVIOUS if fault == "wrong_date" else query_day
    records = [
        {
            "Code": f"{ticker}0",
            "Date": record_day.isoformat(),
            "CoName": "合成会社",
            "MktNm": "PRIME",
            "S33Nm": "電気機器",
            "ProdCat": "011",
        }
        for ticker in range(1000, 3500)
    ]
    master = receive(database, "master", {"date": query_day.isoformat()}, body(records), RECEIVED)
    if fault == "partial":
        with closing(open_connection(database)) as conn, conn:
            conn.execute(
                "UPDATE jquants_source_pages SET body_gzip=? WHERE request_id=?",
                (gzip.compress(body(records, cursor="unfinished"), mtime=0), master),
            )
    rows = [
        {
            "Date": f"2026-10-{day}",
            "HolDiv": "1" if day == 16 or (day == 19 and not rest_day) else "0",
        }
        for day in range(16, 20)
    ]
    calendar = receive(
        database,
        "market_calendar",
        (
            {"from": "20261016", "to": "20261019"}
            if basic_calendar
            else {"from": "2026-10-16", "to": "2026-10-19"}
        ),
        body(rows),
        RECEIVED,
    )
    if fault is None:
        project_request(database, master)
    project_request(database, calendar)
    # A market handoff also contains prices: consumers must never download it.
    receive(
        database, "daily_bars", {"date": DAY.isoformat()}, body([{"private_price": 999}]), RECEIVED
    )
    directory = root / "stage"
    _export(source, DAY, RECEIVED - timedelta(seconds=1), database, directory)
    save_handoff(store, f"{source}-{DAY.isoformat()}-new", directory, market=True)
    return master, calendar


@pytest.mark.parametrize("source", ["jquants-market", "tradingview"])
@pytest.mark.parametrize("basic_calendar", [True, False], ids=["native-query", "extended-query"])
def test_previous_lake_and_ready_today_dependencies_initialize_then_acquire(
    tmp_path,
    monkeypatch,
    source,
    basic_calendar,
):
    store, release, _ = build_master_lake(
        tmp_path,
        monkeypatch,
        snapshot=PREVIOUS,
        calendar=[("2026-10-16", 1), ("2026-10-17", 0), ("2026-10-18", 0)],
    )
    original_pointer = store.values[current_release_pointer_key()].body
    root = tmp_path / "acquisition"
    root.mkdir()
    master, calendar = stage_dependencies(store, root, source=source, basic_calendar=basic_calendar)
    store.get_keys.clear()
    plan = daily.initialize(
        store,
        run_id="monex-before-integration",
        source="monex",
        master_asof=None,
        observation_date=DAY,
        mirror=tmp_path / "new-plan",
    )
    binding = json.loads(plan.universe_binding)
    assert binding["master_asof"] == DAY.isoformat()
    assert binding["base_release_manifest_sha256"] == release.manifest_sha256
    assert binding["master_receipt"]["request_id"] == master
    assert binding["calendar_receipt"]["request_id"] == calendar
    expected_query = (
        {"from": "20261016", "to": "20261019"}
        if basic_calendar
        else {"from": "2026-10-16", "to": "2026-10-19"}
    )
    assert binding["calendar_receipt"]["params"] == expected_query
    dependency = read_dependency(load_dependency(store, binding["calendar_receipt"]))
    assert dict(dependency.params) == dict(dependency.pages[0].query) == expected_query
    assert datetime.fromisoformat(binding["master_receipt"]["received_at"]) == RECEIVED
    assert len(plan.tickers) == 2500
    price_manifest = "lake/manifests/datasets/jquants.daily_bars/synthetic-jquants-daily_bars.json"
    assert price_manifest in store.get_keys
    assert [key for key in store.get_keys if "daily_bars/" in key] == [price_manifest]
    assert not any(
        key.endswith(".sqlite") or ("daily_bars/" in key and key.endswith(".parquet"))
        for key in store.get_keys
    )
    assert not any(
        "jquants.master_snapshots/" in key and key.endswith(".parquet") for key in store.get_keys
    )
    assert not list((tmp_path / "new-plan").rglob("*.sqlite"))
    calls = wire_success(monkeypatch, store, plan)
    acquired = daily.acquire_cohort(store, plan, 0)
    assert acquired["cohort_complete"] is True
    assert calls
    assert store.values[current_release_pointer_key()].body == original_pointer
    # A retry does not reread dependency pointers, release or a new Universe.
    store.store_object(current_release_pointer_key(), b"newer unreadable release")
    store.get_keys.clear()
    assert (
        daily.initialize(
            store,
            run_id=plan.run_id,
            source="monex",
            master_asof=None,
            observation_date=DAY,
            mirror=None,
        )
        == plan
    )
    assert store.get_keys == [f"coordination/external-assessment/{plan.run_id}/plan.json"]


def test_scoped_calendar_tail_extension_retains_a_ready_dependency_covering_today(
    tmp_path, monkeypatch
):
    store, _, _ = build_master_lake(
        tmp_path,
        monkeypatch,
        snapshot=PREVIOUS,
        calendar=[("2026-10-16", 1), ("2026-10-17", 0), ("2026-10-18", 0)],
    )
    canonical, scoped = tmp_path / "canonical.sqlite", tmp_path / "scoped.sqlite"
    start, end = DAY - timedelta(days=7), DAY + timedelta(days=45)
    prior_end = end - timedelta(days=1)
    prior_records = [
        {
            "Date": (start + timedelta(days=offset)).isoformat(),
            "HolDiv": "1" if (start + timedelta(days=offset)).weekday() < 5 else "0",
        }
        for offset in range((prior_end - start).days + 1)
    ]
    prior_calendar = receive(
        canonical,
        "market_calendar",
        {"from": start.strftime("%Y%m%d"), "to": prior_end.strftime("%Y%m%d")},
        body(prior_records),
        RECEIVED - timedelta(days=1),
    )
    project_request(canonical, prior_calendar)
    export_source_input(canonical, scoped, group="jquants-market", asof=DAY)
    with closing(open_connection(scoped)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jquants_source_pages").fetchone()[0] == 0

    records = [*prior_records, {"Date": end.isoformat(), "HolDiv": "1"}]
    session = Session(body(make_master_records(DAY)), body(records))
    maintenance = JQuantsRefresh(
        scoped,
        None,
        client=JQuantsHTTP("synthetic-test-key", session=session, clock=lambda: RECEIVED),
    )
    assert maintenance.ensure_master(DAY) == 2500
    assert maintenance.ensure_market_calendar(start, end) == len(records)
    expected_query = {"from": start.strftime("%Y%m%d"), "to": end.strftime("%Y%m%d")}
    assert session.calls[1][1]["params"] == expected_query

    directory = tmp_path / "scoped-ready"
    _export("jquants-market", DAY, RECEIVED - timedelta(seconds=1), scoped, directory)
    dependencies = [json.loads(path.read_text()) for path in directory.glob("dependency-*.json")]
    calendar = next(value for value in dependencies if value["source"] == "market_calendar")
    assert calendar["params"] == expected_query
    save_handoff(store, f"jquants-market-{DAY.isoformat()}-scoped", directory, market=True)
    plan = daily.initialize(
        store,
        run_id="monex-scoped-tail-extension",
        source="monex",
        master_asof=None,
        observation_date=DAY,
        mirror=tmp_path / "scoped-plan",
    )
    binding = json.loads(plan.universe_binding)
    assert binding["calendar_receipt"]["request_id"] == calendar["request_id"]
    assert binding["calendar_receipt"]["params"] == expected_query
    assert len(plan.tickers) == 2500


@pytest.mark.parametrize("fault", ["old", "partial", "wrong_date"])
def test_unqualified_dependency_cannot_replace_today_exact_master(tmp_path, monkeypatch, fault):
    store, _, _ = build_master_lake(
        tmp_path,
        monkeypatch,
        snapshot=PREVIOUS,
        calendar=[("2026-10-16", 1), ("2026-10-17", 0), ("2026-10-18", 0)],
    )
    root = tmp_path / "invalid"
    root.mkdir()
    stage_dependencies(store, root, fault=fault)
    before = set(store.values)
    with pytest.raises((ValueError, JQuantsProviderError)):
        daily.initialize(
            store,
            run_id="no-false-current-master",
            source="monex",
            master_asof=None,
            observation_date=DAY,
            mirror=tmp_path / "refused",
        )
    assert set(store.values) == before
    with closing(open_connection(root / "source.sqlite")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jquants_source_pages").fetchone()[0] == 3


def test_ready_calendar_can_select_an_exact_published_rest_day_master(tmp_path, monkeypatch):
    store, release, _ = build_master_lake(
        tmp_path,
        monkeypatch,
        snapshot=PREVIOUS,
        calendar=[("2026-10-16", 1), ("2026-10-17", 0), ("2026-10-18", 0)],
    )
    root = tmp_path / "calendar-only"
    root.mkdir()
    _, calendar = stage_dependencies(store, root, fault="old", rest_day=True)
    plan = daily.initialize(
        store,
        run_id="monex-calendar-only",
        source="monex",
        master_asof=None,
        observation_date=DAY,
        mirror=tmp_path / "holiday",
    )
    binding = json.loads(plan.universe_binding)
    assert binding["master_asof"] == PREVIOUS.isoformat()
    assert binding["release_manifest_sha256"] == release.manifest_sha256
    assert binding["calendar_receipt"]["request_id"] == calendar
    assert "master_receipt" not in binding
