"""Command-side J-Quants refresh plans against temporary projected stores only."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime

import pytest
import requests
from tests.engine.test_jquants_projection import source_record
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.screening_sqlite import make_master_records

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsAcquisitionError,
)
from baibai_engine.data_collection.providers.jquants.http import (
    JQuantsHTTP,
)
from baibai_engine.data_collection.providers.jquants.receipts import read_request_pages
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.machine_analysis.observations.jquants_writer import (
    store_jquants_daily_bars,
    store_jquants_fin_summaries,
    store_jquants_margin_alerts,
    store_jquants_market_calendar,
    store_jquants_master,
    store_jquants_short_sale_reports,
)
from baibai_engine.machine_analysis.observations.prices.price_reader import read_daily_bars
from baibai_engine.storage.snapshots.release import create_release
from baibai_engine.storage.snapshots.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.sqlite.source_cache.coverage import range_covered
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.maintenance.jquants_inputs import JQuantsReader
from baibai_jobs.maintenance.source_refresh.jquants import JQuantsRefresh
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.policy import release_policy_for_profile


def day(value: str) -> date:
    return date.fromisoformat(value)


def native(value: str | date) -> str:
    return day(value).strftime("%Y%m%d") if isinstance(value, str) else value.strftime("%Y%m%d")


def financial(value: str) -> dict[str, str]:
    return {"Code": "13010", "DiscDate": value, "DiscNo": value, "CurPerType": "FY"}


def bar(value: str, close: float) -> dict[str, str | float]:
    return {"Code": "13010", "Date": value, "C": close, "AdjFactor": 1.0}


class Session(requests.Session):
    def __init__(self, replies: dict[tuple[str, str], list[dict[str, object]] | Exception]) -> None:
        super().__init__()
        self.replies = replies
        self.calls: list[tuple[str, str]] = []

    def get(self, url, **kwargs):
        params = kwargs["params"]
        scope = (
            f"{params['from']}..{params['to']}"
            if "from" in params
            else params.get("disc_date", params.get("date", ""))
        )
        key = (url.rsplit("/", 1)[-1], scope)
        self.calls.append(key)
        lookup = (
            (key[0], day(key[1]).isoformat()) if key[0] in {"summary", "margin-interest"} else key
        )
        if lookup not in self.replies:
            raise AssertionError(f"unplanned fake HTTP request: {key}")
        if isinstance(self.replies[lookup], Exception):
            raise self.replies[lookup]
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"data": self.replies[lookup]}).encode()
        response._content_consumed = True
        return response


def refresh(database, replies):
    session = Session(replies)
    client = JQuantsHTTP(
        "synthetic-test-key",
        session=session,
        clock=lambda: datetime(2026, 10, 17, tzinfo=UTC),
    )
    return JQuantsRefresh(database, None, client=client), session


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "market.sqlite"
    open_connection(path).close()
    return path


def test_covered_financial_input_needs_neither_request_nor_api_key(database):
    start = day("2026-10-13")
    store_jquants_fin_summaries(
        database, [financial(start.isoformat())], requested_start=start, requested_end=start
    )
    maintenance = JQuantsRefresh(database, None)
    assert maintenance.ensure_fin_summaries(start, start) == 1
    with pytest.raises(JQuantsAcquisitionError, match="JQUANTS_API_KEY is required"):
        maintenance.ensure_fin_summaries(day("2026-10-14"), day("2026-10-14"))


def test_covered_bar_and_financial_ensure_count_without_building_models(database, monkeypatch):
    target = day("2026-10-16")
    store_jquants_daily_bars(
        database,
        [bar(target.isoformat(), 100.0)],
        requested_start=target,
        requested_end=target,
    )
    store_jquants_fin_summaries(
        database,
        [financial(target.isoformat())],
        requested_start=target,
        requested_end=target,
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("count-only maintenance must not construct source models")

    monkeypatch.setattr(JQuantsReader, "get_eq_bars_daily_range", forbidden)
    monkeypatch.setattr(JQuantsReader, "get_fin_summary_range", forbidden)
    monkeypatch.setattr(
        "baibai_engine.machine_analysis.observations.prices.price_reader.JQuantsDailyBar", forbidden
    )
    monkeypatch.setattr(
        "baibai_engine.machine_analysis.observations.financials.reader.normalize_financial_summary",
        forbidden,
    )
    maintenance = JQuantsRefresh(database, None)
    assert maintenance.ensure_daily_bars(target, target) == 1
    assert maintenance.ensure_fin_summaries(target, target) == 1


def test_cached_exact_master_uses_offline_reader_without_acquisition(database):
    target = day("2026-05-07")
    records = make_master_records(target)
    records[0]["CoName"] = "極洋"
    store_jquants_master(database, records, requested_asof=target)
    assert JQuantsRefresh(database, None).ensure_master(target) == len(records)
    masters = JQuantsReader(database).get_eq_master(target)
    assert masters[0].name == "極洋"


def test_financial_refresh_merges_missing_days_with_correction_overlap(database):
    start, end = day("2026-10-13"), day("2026-10-16")
    for value in ("2026-10-13", "2026-10-15"):
        current = day(value)
        store_jquants_fin_summaries(
            database, [financial(value)], requested_start=current, requested_end=current
        )
    maintenance, session = refresh(
        database,
        {
            ("summary", value): [financial(value)]
            for value in ("2026-10-14", "2026-10-15", "2026-10-16")
        },
    )
    progress = []
    assert (
        maintenance.refresh_fin_summaries(
            start, end, revision_overlap_days=1, progress=lambda *args: progress.append(args)
        )
        == 4
    )
    assert session.calls == [
        ("summary", "20261014"),
        ("summary", "20261015"),
        ("summary", "20261016"),
    ]
    assert [item[:2] for item in progress] == [(1, 3), (2, 3), (3, 3)]
    with sqlite3.connect(database) as conn:
        request_id = conn.execute(
            "SELECT request_id FROM jquants_source_pages ORDER BY received_at_utc LIMIT 1"
        ).fetchone()[0]
        assert read_request_pages(conn, request_id)[0].query["date"] == "20261014"


def test_financial_outage_longer_than_overlap_fetches_gap_once(database):
    start, covered_end, end = day("2026-04-01"), day("2026-04-10"), day("2026-04-20")
    store_jquants_fin_summaries(database, [], requested_start=start, requested_end=covered_end)
    missing_days = [date.fromordinal(covered_end.toordinal() + offset) for offset in range(1, 11)]
    maintenance, session = refresh(
        database,
        {
            ("summary", current.isoformat()): [financial(current.isoformat())]
            for current in missing_days
        },
    )
    assert maintenance.refresh_fin_summaries(start, end, revision_overlap_days=7) == 10
    assert session.calls == [("summary", native(current)) for current in missing_days]
    with sqlite3.connect(database) as conn:
        assert range_covered(conn, "jquants_fin_summaries", start, end)


def test_later_financial_http_failure_preserves_earlier_projection_and_resume(database):
    start, middle, end = day("2026-07-01"), day("2026-07-02"), day("2026-07-03")
    maintenance, session = refresh(
        database,
        {
            ("summary", start.isoformat()): [financial(start.isoformat())],
            ("summary", middle.isoformat()): requests.ConnectionError("synthetic failure"),
        },
    )
    with pytest.raises(JQuantsAcquisitionError, match="ConnectionError"):
        maintenance.ensure_fin_summaries(start, end)
    assert session.calls == [("summary", native(start)), ("summary", native(middle))]
    with sqlite3.connect(database) as conn:
        assert range_covered(conn, "jquants_fin_summaries", start, start)
        assert not range_covered(conn, "jquants_fin_summaries", start, end)
        assert conn.execute("SELECT disclosed_at FROM jquants_fin_summaries").fetchall() == [
            (start.isoformat(),)
        ]
    resumed, resumed_session = refresh(
        database,
        {
            ("summary", current.isoformat()): [financial(current.isoformat())]
            for current in (middle, end)
        },
    )
    assert resumed.ensure_fin_summaries(start, end) == 3
    assert resumed_session.calls == [
        ("summary", native(middle)),
        ("summary", native(end)),
    ]


def test_empty_financial_response_is_readable_and_reused(database):
    start, end = day("2026-05-02"), day("2026-05-03")
    maintenance, session = refresh(
        database, {("summary", current.isoformat()): [] for current in (start, end)}
    )
    assert maintenance.ensure_fin_summaries(start, end) == 0
    assert maintenance.ensure_fin_summaries(start, end) == 0
    assert session.calls == [("summary", native(start)), ("summary", native(end))]
    assert JQuantsReader(database).get_fin_summary_range(start, end) == []
    with sqlite3.connect(database) as conn:
        assert range_covered(conn, "jquants_fin_summaries", start, end)


def test_rejected_financial_quality_does_not_claim_coverage(database):
    target = day("2026-05-15")
    store_jquants_fin_summaries(
        database,
        [{"Code": "", "DiscDate": target.isoformat()}],
        requested_start=target,
        requested_end=target,
    )
    with sqlite3.connect(database) as conn:
        assert not range_covered(conn, "jquants_fin_summaries", target, target)
        assert conn.execute("SELECT record_count, status FROM source_coverage").fetchall() == [
            (0, "partial")
        ]
    with pytest.raises(JQuantsProviderError, match="incomplete"):
        JQuantsReader(database).get_fin_summary_range(target, target)


def test_partial_financial_refetch_preserves_empty_outer_windows(database):
    start, end, bad_day = day("2026-05-01"), day("2026-05-31"), day("2026-05-15")
    store_jquants_fin_summaries(database, [], requested_start=start, requested_end=end)
    store_jquants_fin_summaries(
        database,
        [{"Code": "", "DiscDate": bad_day.isoformat()}],
        requested_start=bad_day,
        requested_end=bad_day,
    )
    with sqlite3.connect(database) as conn:
        assert range_covered(conn, "jquants_fin_summaries", start, day("2026-05-14"))
        assert not range_covered(conn, "jquants_fin_summaries", bad_day, bad_day)
        assert range_covered(conn, "jquants_fin_summaries", day("2026-05-16"), end)


def test_historical_holiday_edge_does_not_refetch_but_current_tail_does(database):
    start, newer = day("2026-10-09"), day("2026-10-12")
    store_jquants_daily_bars(
        database,
        [bar(start.isoformat(), 100.0), bar(newer.isoformat(), 102.0)],
        requested_start=start,
        requested_end=newer,
    )
    maintenance, session = refresh(
        database,
        {
            ("daily", "2026-10-12"): [bar("2026-10-12", 102.0)],
            ("daily", "2026-10-13"): [bar("2026-10-13", 103.0)],
        },
    )
    assert maintenance.ensure_daily_bars(start, day("2026-10-11")) == 1
    assert session.calls == []
    assert maintenance.ensure_daily_bars(newer, day("2026-10-13")) == 2
    assert session.calls == [("daily", "2026-10-13")]


def test_daily_bars_repairs_only_the_nine_unfetched_days_between_receipts(database):
    start, end = day("2026-10-01"), day("2026-10-16")
    store_jquants_daily_bars(
        database,
        [bar("2026-10-06", 100.0)],
        requested_start=start,
        requested_end=day("2026-10-06"),
    )
    store_jquants_daily_bars(
        database,
        [bar("2026-10-16", 110.0)],
        requested_start=end,
        requested_end=end,
    )
    assert read_daily_bars(database, start, end) is None
    gap_days = [date.fromordinal(day("2026-10-07").toordinal() + offset) for offset in range(9)]
    maintenance, session = refresh(
        database,
        {
            ("daily", value.isoformat()): (
                [bar(value.isoformat(), 105.0)] if value == day("2026-10-13") else []
            )
            for value in gap_days
        },
    )
    assert maintenance.ensure_daily_bars(start, end) == 3
    assert session.calls == [("daily", value.isoformat()) for value in gap_days]
    with sqlite3.connect(database) as conn:
        assert range_covered(conn, "jquants_daily_bars", start, end)
    assert len(read_daily_bars(database, start, end) or []) == 3
    assert maintenance.ensure_daily_bars(start, end) == 3
    assert session.calls == [("daily", value.isoformat()) for value in gap_days]


def test_daily_bars_reuses_successful_empty_historical_range(database):
    target = day("2019-05-01")
    store_jquants_daily_bars(database, [], requested_start=target, requested_end=target)
    assert read_daily_bars(database, target, target) == []
    assert JQuantsRefresh(database, None).ensure_daily_bars(target, target) == 0


def test_partial_daily_bar_response_does_not_satisfy_coverage(database):
    target = day("2026-10-07")
    store_jquants_daily_bars(
        database,
        [{"Code": "", "Date": target.isoformat(), "C": 100.0}],
        requested_start=target,
        requested_end=target,
    )
    assert read_daily_bars(database, target, target) is None
    with sqlite3.connect(database) as conn:
        assert not range_covered(conn, "jquants_daily_bars", target, target)
    maintenance, session = refresh(
        database, {("daily", target.isoformat()): [bar(target.isoformat(), 100.0)]}
    )
    assert maintenance.ensure_daily_bars(target, target) == 1
    assert session.calls == [("daily", target.isoformat())]


def test_daily_bar_interruption_resumes_only_unfetched_days(database, monkeypatch):
    start, end = day("2026-10-07"), day("2026-10-10")
    maintenance = JQuantsRefresh(database, None)
    attempted: list[date] = []

    def interrupted_fetch(source, params):
        value = day(params["date"])
        attempted.append(value)
        if value == day("2026-10-09"):
            raise RuntimeError("interrupted")
        store_jquants_daily_bars(
            database,
            [],
            requested_start=value,
            requested_end=value,
        )

    monkeypatch.setattr(maintenance, "_fetch", interrupted_fetch)
    with pytest.raises(RuntimeError, match="interrupted"):
        maintenance.ensure_daily_bars(start, end)
    assert attempted == [day("2026-10-07"), day("2026-10-08"), day("2026-10-09")]
    assert read_daily_bars(database, start, end) is None
    resumed, session = refresh(
        database,
        {("daily", value): [] for value in ("2026-10-09", "2026-10-10")},
    )
    assert resumed.ensure_daily_bars(start, end) == 0
    assert session.calls == [("daily", "2026-10-09"), ("daily", "2026-10-10")]
    assert read_daily_bars(database, start, end) == []


def test_covered_current_tail_is_explicitly_rechecked(database):
    start, end = day("2026-10-12"), day("2026-10-13")
    store_jquants_daily_bars(
        database,
        [bar(start.isoformat(), 100.0)],
        requested_start=start,
        requested_end=end,
    )
    maintenance, session = refresh(
        database,
        {
            ("daily", start.isoformat()): [bar(start.isoformat(), 100.0)],
            ("daily", end.isoformat()): [bar(end.isoformat(), 101.0)],
        },
    )
    assert maintenance.ensure_daily_bars(start, end) == 2
    assert session.calls == [("daily", start.isoformat()), ("daily", end.isoformat())]


def test_daily_backfill_paces_after_31_days_and_reuses_covered_data(database, monkeypatch):
    start, end = day("2026-01-01"), day("2026-02-01")
    dates = [date.fromordinal(start.toordinal() + offset) for offset in range(32)]
    maintenance, session = refresh(
        database,
        {("daily", value.isoformat()): [bar(value.isoformat(), 100.0)] for value in dates},
    )
    waits = []
    monkeypatch.setattr("baibai_jobs.maintenance.source_refresh.jquants.time.sleep", waits.append)
    assert maintenance.ensure_daily_bars(start, end) == 32
    assert len(session.calls) == 32
    assert waits == [3.0]
    assert maintenance.ensure_daily_bars(start, end) == 32
    assert len(session.calls) == 32
    assert waits == [3.0]


def test_short_sale_refresh_repairs_outage_gap_and_requested_overlap(database):
    for value in ("2026-10-12", "2026-10-14"):
        current = day(value)
        store_jquants_short_sale_reports(
            database, [], requested_start=current, requested_end=current
        )
    maintenance, session = refresh(
        database,
        {("short-sale-report", value): [] for value in ("2026-10-13", "2026-10-14", "2026-10-15")},
    )
    assert maintenance.refresh_short_sale_reports(day("2026-10-14"), day("2026-10-15")) == 0
    assert session.calls == [
        ("short-sale-report", "2026-10-13"),
        ("short-sale-report", "2026-10-14"),
        ("short-sale-report", "2026-10-15"),
    ]


def test_short_sale_explicit_refresh_keeps_numeric_correction(database):
    target = day("2026-04-24")
    original = {
        "DiscDate": target.isoformat(),
        "CalcDate": target.isoformat(),
        "Code": "72030",
        "SSName": "Reporter A",
        "ShrtPosToSO": 0.006,
    }
    store_jquants_short_sale_reports(
        database, [original], requested_start=target, requested_end=target
    )
    maintenance, session = refresh(
        database,
        {("short-sale-report", target.isoformat()): [{**original, "ShrtPosToSO": 0.007}]},
    )
    assert maintenance.ensure_short_sale_reports(target, target) == 1
    assert session.calls == []
    assert (
        JQuantsReader(database).get_mkt_short_sale_report_range(target, target)[0].short_ratio
        == 0.006
    )
    assert maintenance.refresh_short_sale_reports(target, target) == 1
    assert session.calls == [("short-sale-report", target.isoformat())]
    assert (
        JQuantsReader(database).get_mkt_short_sale_report_range(target, target)[0].short_ratio
        == 0.007
    )


@pytest.mark.parametrize("source", ["market_calendar", "margin_alerts"])
def test_accumulated_history_becomes_publishable_after_outage_repair(
    database, tmp_path, monkeypatch, source
):
    dataset = f"jquants.{source}"
    narrow_release_policy(monkeypatch, datasets=(dataset,), carries_history=None)
    start, old_end = day("2026-10-01"), day("2026-10-04")
    current = day("2026-10-16")
    if source == "market_calendar":
        writer = store_jquants_market_calendar
        end = day("2026-10-18")
        window_start = current
        old_records = [{"Date": f"2026-10-0{value}", "HolDiv": "1"} for value in range(1, 5)]
        new_records = [{"Date": f"2026-10-{value}", "HolDiv": "0"} for value in range(16, 19)]
        replies = {
            ("calendar", "20261005..20261015"): [
                {"Date": date(2026, 10, value).isoformat(), "HolDiv": "1"} for value in range(5, 16)
            ]
        }
        expected_calls = [("calendar", "20261005..20261015")]
        expected_count = 3
    else:
        writer = store_jquants_margin_alerts
        end = current
        window_start = day("2026-10-09")
        old_records = [source_record(source, start.isoformat())]
        new_records = [source_record(source, current.isoformat())]
        replies = {
            ("margin-alert", date(2026, 10, value).isoformat()): (
                new_records if value == 16 else []
            )
            for value in range(5, 17)
        }
        expected_calls = [
            ("margin-alert", date(2026, 10, value).isoformat()) for value in range(5, 17)
        ]
        expected_count = 1
    writer(database, old_records, requested_start=start, requested_end=old_end)
    bad_day = day("2026-10-07")
    writer(database, [{}], requested_start=bad_day, requested_end=bad_day)
    writer(database, new_records, requested_start=current, requested_end=end)
    with sqlite3.connect(database) as conn:
        assert not range_covered(conn, f"jquants_{source}", start, end)
        assert conn.execute(
            "SELECT status FROM source_coverage WHERE source = ? AND status <> 'ok'",
            (f"jquants_{source}",),
        ).fetchall() == [("partial",)]

    mirror = tmp_path / "lake"

    def export(phase):
        with sealed_sqlite_snapshot(sqlite_path=database, mirror_root=mirror) as snapshot:
            return export_dataset_snapshot(
                dataset=LAKE_DATASETS[dataset],
                mirror_root=mirror,
                producer_git_commit="a" * 40,
                source_snapshot=snapshot,
                build_id=f"{source}-{phase}",
            )

    incomplete = export("before")
    assert incomplete.manifest.coverage_status == "partial"
    with pytest.raises(ValueError, match="coverage is partial"):
        create_release(
            dataset_manifest_paths=[incomplete.manifest_path],
            mirror_root=mirror,
            release_id="incomplete-history",
            catalog=LAKE_DATASETS,
            policy=release_policy_for_profile("production"),
        )
    maintenance, session = refresh(database, replies)
    operation = (
        maintenance.ensure_market_calendar
        if source == "market_calendar"
        else maintenance.refresh_margin_alerts
    )
    assert operation(window_start, end) == expected_count
    assert session.calls == expected_calls
    with sqlite3.connect(database) as conn:
        assert range_covered(conn, f"jquants_{source}", start, end)
        assert not conn.execute(
            "SELECT 1 FROM source_coverage WHERE source = ? AND status <> 'ok'",
            (f"jquants_{source}",),
        ).fetchall()
    complete = export("after")
    assert complete.manifest.coverage_status == "complete"
    _, release = create_release(
        dataset_manifest_paths=[complete.manifest_path],
        mirror_root=mirror,
        release_id="recovered-history",
        catalog=LAKE_DATASETS,
        policy=release_policy_for_profile("production"),
    )
    assert release.datasets[dataset].coverage_status == "complete"
    if source == "market_calendar":
        assert operation(window_start, end) == expected_count
        assert session.calls == expected_calls


def test_margin_alert_empty_coverage_anchor_survives_interrupted_repair(database):
    start, old_end = day("2026-10-01"), day("2026-10-03")
    store_jquants_margin_alerts(database, [], requested_start=start, requested_end=old_end)
    maintenance, session = refresh(
        database,
        {
            ("margin-alert", "2026-10-04"): [],
            ("margin-alert", "2026-10-05"): requests.ConnectionError("synthetic outage"),
        },
    )
    window_start, end = day("2026-10-09"), day("2026-10-10")
    with pytest.raises(JQuantsAcquisitionError, match="ConnectionError"):
        maintenance.refresh_margin_alerts(window_start, end)
    assert session.calls == [("margin-alert", "2026-10-04"), ("margin-alert", "2026-10-05")]
    with sqlite3.connect(database) as conn:
        assert range_covered(conn, "jquants_margin_alerts", start, day("2026-10-04"))
        assert not range_covered(conn, "jquants_margin_alerts", start, end)
    resumed, resumed_session = refresh(
        database,
        {("margin-alert", date(2026, 10, value).isoformat()): [] for value in range(5, 11)},
    )
    assert resumed.refresh_margin_alerts(window_start, end) == 0
    assert resumed_session.calls == [
        ("margin-alert", date(2026, 10, value).isoformat()) for value in range(5, 11)
    ]
    assert JQuantsReader(database).get_mkt_margin_alert_range(start, end) == []


@pytest.mark.parametrize("source", ["market_calendar", "margin_alerts"])
def test_new_store_uses_only_the_requested_initial_window(database, source):
    start, end = day("2026-10-09"), day("2026-10-10")
    if source == "market_calendar":
        expected_calls = [("calendar", "20261009..20261010")]
        replies = {
            expected_calls[0]: [{"Date": f"2026-10-{value:02}", "HolDiv": "0"} for value in (9, 10)]
        }
        maintenance, session = refresh(database, replies)
        assert maintenance.ensure_market_calendar(start, end) == 2
    else:
        expected_calls = [("margin-alert", value.isoformat()) for value in (start, end)]
        maintenance, session = refresh(database, {call: [] for call in expected_calls})
        assert maintenance.refresh_margin_alerts(start, end) == 0
    assert session.calls == expected_calls


def test_weekly_and_daily_margin_boundaries_fail_before_http(database):
    maintenance, session = refresh(database, {})
    with pytest.raises(ValueError, match="legacy weekly series"):
        maintenance.ensure_weekly_margin(day("2026-09-25"))
    with pytest.raises(ValueError, match="daily all-issues series"):
        maintenance.ensure_all_issues_daily_margin(day("2026-09-18"))
    assert session.calls == []


def test_margin_alert_limited_population_projects_native_reason(database):
    target = day("2026-08-10")
    record = {
        "PubDate": target.isoformat(),
        "Code": "72030",
        "AppDate": target.isoformat(),
        "PubReason": "日々公表",
        "LongOut": 5000,
        "ShrtOut": 1000,
        "ShrtOutChg": 10,
        "ShrtOutRatio": 0.2,
        "LongOutChg": 20,
        "LongOutRatio": 0.8,
        "SLRatio": 0.2,
        "ShrtNegOut": 200,
        "ShrtNegOutChg": 2,
        "ShrtStdOut": 800,
        "ShrtStdOutChg": 8,
        "LongNegOut": 2000,
        "LongNegOutChg": 10,
        "LongStdOut": 3000,
        "LongStdOutChg": 10,
        "TSEMrgnRegCls": "委託保証金率50%",
    }
    maintenance, session = refresh(database, {("margin-alert", target.isoformat()): [record]})
    assert maintenance.ensure_margin_alerts(target, target) == 1
    assert session.calls == [("margin-alert", target.isoformat())]
    rows = JQuantsReader(database).get_mkt_margin_alert_range(target, target)
    assert len(rows) == 1
    assert rows[0].tse_margin_regulation_classification == "委託保証金率50%"


def test_post_transition_margin_interest_only_populates_daily_table(database):
    target = day("2026-09-25")
    record = {
        "Date": target.isoformat(),
        "Code": "72030",
        "LongVol": 5000,
        "ShrtVol": 1000,
        "LongStdVol": 3000,
        "LongNegVol": 2000,
        "ShrtStdVol": 800,
        "ShrtNegVol": 200,
        "IssType": "2",
    }
    maintenance, session = refresh(database, {("margin-interest", target.isoformat()): [record]})
    assert maintenance.ensure_all_issues_daily_margin(target) == 1
    assert session.calls == [("margin-interest", native(target))]
    assert JQuantsReader(database).get_mkt_all_issues_daily_margin(target)[0].long_vol == 5000
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jquants_all_issues_daily_margin").fetchone() == (
            1,
        )
        assert conn.execute("SELECT COUNT(*) FROM jquants_weekly_margin").fetchone() == (0,)


def test_failed_projection_keeps_committed_source_receipt(database):
    maintenance, session = refresh(
        database,
        {("daily", "2026-10-16"): [bar("2026-10-15", 100.0)]},
    )
    with pytest.raises(ValueError, match="outside the requested range"):
        maintenance.ensure_daily_bars(day("2026-10-16"), day("2026-10-16"))
    assert session.calls == [("daily", "2026-10-16")]
    with sqlite3.connect(database) as conn:
        request_id = conn.execute(
            "SELECT DISTINCT request_id FROM jquants_source_pages"
        ).fetchone()[0]
        assert [json.loads(page.body) for page in read_request_pages(conn, request_id)] == [
            {"data": [bar("2026-10-15", 100.0)]}
        ]
        assert conn.execute("SELECT COUNT(*) FROM jquants_daily_bars").fetchone()[0] == 0
