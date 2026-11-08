"""Command-side J-Quants refresh plans against temporary projected stores only."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime

import pytest
import requests
from tests.helpers.jquants_receipts import source_record
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.network import _no_real_network as _no_real_network
from tests.helpers.screening_sqlite import make_master_records

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


def test_cached_exact_master_uses_offline_reader_without_acquisition(database):
    target = day("2026-05-07")
    records = make_master_records(target)
    records[0]["CoName"] = "極洋"
    store_jquants_master(database, records, requested_asof=target)
    assert JQuantsRefresh(database, None).ensure_master(target) == len(records)
    masters = JQuantsReader(database).get_eq_master(target)
    assert masters[0].name == "極洋"


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


@pytest.mark.parametrize("source", ["market_calendar"])
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


@pytest.mark.parametrize("source", ["market_calendar"])
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


def test_failed_projection_keeps_committed_source_receipt(database):
    maintenance, session = refresh(
        database,
        {("daily", "2026-10-16"): [bar("2026-10-15", 100.0)]},
    )
    with pytest.raises(ValueError, match="outside the requested range"):
        maintenance._fetch("daily_bars", {"date": "2026-10-16"})
    assert session.calls == [("daily", "2026-10-16")]
    with sqlite3.connect(database) as conn:
        request_id = conn.execute(
            "SELECT DISTINCT request_id FROM jquants_source_pages"
        ).fetchone()[0]
        assert [json.loads(page.body) for page in read_request_pages(conn, request_id)] == [
            {"data": [bar("2026-10-15", 100.0)]}
        ]
        assert conn.execute("SELECT COUNT(*) FROM jquants_daily_bars").fetchone()[0] == 0
