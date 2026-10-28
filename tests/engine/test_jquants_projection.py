"""Offline replay from retained J-Quants pages into the current market schema."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime

import pyarrow.parquet as pq
import pytest
import requests
from tests.helpers.lake_policy import narrow_release_policy

from baibai_engine.data_collection.providers.jquants.http import JQuantsAcquisitionError, JQuantsHTTP
from baibai_engine.data_collection.providers.jquants.receipts import (
    acquire_request,
    read_complete_request,
    read_request_pages,
)
import baibai_engine.machine_analysis.observations.stocks.master_snapshot as master_snapshot
from baibai_engine.market.jquants_decode import JQuantsProviderError
from baibai_engine.market.jquants_projection import project_request
from baibai_jobs.maintenance.jquants_inputs import JQuantsReader
from baibai_engine.machine_analysis.observations.trading_activity.margin_reader import read_margin_alerts
from baibai_engine.storage.lake.datasets import JQUANTS_MARGIN_ALERTS
from baibai_engine.storage.lake.duck import lake_session
from baibai_engine.storage.lake.hydrate import hydrate_market_store
from baibai_engine.storage.lake.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.lake.reader import resolve_release
from baibai_engine.storage.lake.release import create_release
from baibai_engine.storage.lake.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.observations.schema import open_connection

DAY = "2026-10-16"
FIRST_AT = datetime(2026, 10, 16, 16, 30, tzinfo=UTC)
CORRECTION_AT = datetime(2026, 10, 17, 12, 45, tzinfo=UTC)


class Session(requests.Session):
    def __init__(self, *replies):
        super().__init__()
        self.replies = iter(replies)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        response = requests.Response()
        response.status_code = 200
        response._content = reply
        response._content_consumed = True
        return response


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "market.sqlite"
    open_connection(path).close()
    return path


def body(records, *, cursor=None):
    payload = {"data": records}
    if cursor is not None:
        payload["pagination_key"] = cursor
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def receive(db_path, source, query, raw_body, received_at):
    session = Session(raw_body)
    client = JQuantsHTTP("synthetic-test-key", session=session, clock=lambda: received_at)
    conn = open_connection(db_path)
    try:
        return acquire_request(conn, client, source, query)
    finally:
        conn.close()


def coverage(db_path, source):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT coverage_key, coverage_start, coverage_end, fetched_at_utc, "
            "record_count, status, error FROM source_coverage WHERE source = ?",
            (source,),
        ).fetchall()


def source_record(source, on_date):
    if source == "financial_summaries":
        return {"Code": "13010", "DiscDate": on_date, "DiscNo": on_date, "CurPerType": "FY"}
    if source == "short_sale_reports":
        return {
            "Code": "13010",
            "DiscDate": on_date,
            "CalcDate": on_date,
            "SSName": "Reporter A",
            "ShrtPosToSO": 0.006,
        }
    if source == "margin_interest":
        return {
            "Code": "13010",
            "Date": on_date,
            "LongVol": 5000,
            "ShrtVol": 1000,
            "LongStdVol": 3000,
            "LongNegVol": 2000,
            "ShrtStdVol": 800,
            "ShrtNegVol": 200,
            "IssType": "2",
        }
    if source == "margin_alerts":
        return {
            "Code": "13010",
            "PubDate": on_date,
            "AppDate": on_date,
            "PubReason": "designation",
            "ShrtOut": 1000,
            "ShrtOutChg": 0,
            "ShrtOutRatio": 0.1,
            "LongOut": 2000,
            "LongOutChg": 0,
            "LongOutRatio": 0.2,
            "SLRatio": 0.5,
            "ShrtNegOut": 100,
            "ShrtNegOutChg": 0,
            "ShrtStdOut": 900,
            "ShrtStdOutChg": 0,
            "LongNegOut": 200,
            "LongNegOutChg": 0,
            "LongStdOut": 1800,
            "LongStdOutChg": 0,
            "TSEMrgnRegCls": "none",
        }
    raise AssertionError(f"unexpected source: {source}")


@pytest.mark.parametrize(
    ("source", "target", "table"),
    [
        ("master", DAY, "jquants_master_snapshots"),
        ("daily_bars", DAY, "jquants_daily_bars"),
        ("financial_summaries", DAY, "jquants_fin_summaries"),
        ("market_calendar", DAY, "jquants_market_calendar"),
        ("margin_interest", "2026-07-24", "jquants_weekly_margin"),
        ("margin_interest", "2026-10-02", "jquants_all_issues_daily_margin"),
        ("margin_alerts", DAY, "jquants_margin_alerts"),
        ("short_sale_reports", DAY, "jquants_short_sale_reports"),
    ],
    ids=(
        "master",
        "bars",
        "financial",
        "calendar",
        "weekly-margin",
        "daily-margin",
        "alerts",
        "short-sale",
    ),
)
def test_source_only_fixed_release_replays_typed_rows_and_coverage(
    tmp_path, monkeypatch, source, target, table
):
    narrow_release_policy(monkeypatch, datasets=("jquants.source_pages",), carries_history=None)
    if source == "master":
        monkeypatch.setattr(master_snapshot, "MIN_COMMON_STOCK_MASTER_ROWS", 1)
        record = {
            "Date": target,
            "Code": "13010",
            "CoName": "Fixture",
            "MktNm": "Prime",
            "S33Nm": "情報・通信業",
            "ProdCat": "011",
        }
    elif source == "daily_bars":
        record = {"Code": "13010", "Date": target, "C": 111.0, "AdjFactor": 1.0}
    elif source == "market_calendar":
        record = {"Date": target, "HolDiv": "1"}
    else:
        record = source_record(source, target)
    query = (
        {"from": target, "to": target}
        if source == "market_calendar"
        else {"disc_date" if source == "short_sale_reports" else "date": target}
    )
    original = tmp_path / "source.sqlite"
    received = Session(body([record]))
    with open_connection(original) as conn:
        request_id = acquire_request(
            conn,
            JQuantsHTTP("synthetic-test-key", session=received, clock=lambda: FIRST_AT),
            source,
            query,
        )
    assert len(received.calls) == 1
    assert project_request(original, request_id) == 1
    with sqlite3.connect(original) as conn:
        expected_rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        expected_coverage = conn.execute(
            "SELECT * FROM source_coverage WHERE source = ?", (table,)
        ).fetchall()
    assert len(expected_rows) == len(expected_coverage) == 1
    assert FIRST_AT.isoformat(timespec="microseconds") in expected_coverage[0]

    mirror = tmp_path / "mirror"
    with sealed_sqlite_snapshot(sqlite_path=original, mirror_root=mirror) as snapshot:
        exported = export_dataset_snapshot(
            dataset_name="jquants.source_pages",
            mirror_root=mirror,
            producer_git_commit="a" * 40,
            source_snapshot=snapshot,
            build_id=f"replay-{source}-{target}",
        )
    assert exported.manifest.layer == "l1_source"
    manifest_path, release_manifest = create_release(
        dataset_manifest_paths=[exported.manifest_path],
        mirror_root=mirror,
        release_id=f"source-only-{source}-{target}",
    )
    assert set(release_manifest.datasets) == {"jquants.source_pages"}
    source_mirror = LocalMirrorSource(mirror)
    fixed = resolve_release(
        source_mirror,
        release_id=release_manifest.release_id,
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )
    rebuilt = tmp_path / "rebuilt.sqlite"
    open_connection(rebuilt).close()

    def no_http(*_args, **_kwargs):
        pytest.fail("raw-only hydrate/replay attempted HTTP")

    monkeypatch.setattr(requests.Session, "get", no_http)
    monkeypatch.setattr(Session, "get", no_http)
    with lake_session() as session:
        report = hydrate_market_store(
            session,
            release=fixed,
            cache=LakeObjectCache(root=mirror, source=source_mirror),
            store=rebuilt,
            dataset_names=("jquants.source_pages",),
        )
    assert report.rows == {"jquants.source_pages": 1}
    with sqlite3.connect(rebuilt) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
        assert conn.execute(
            "SELECT COUNT(*) FROM source_coverage WHERE source = ?", (table,)
        ).fetchone() == (0,)
        assert [page.body for page in read_request_pages(conn, request_id)] == [body([record])]
    assert project_request(rebuilt, request_id) == 1
    with sqlite3.connect(rebuilt) as conn:
        assert conn.execute(f"SELECT * FROM {table}").fetchall() == expected_rows
        assert (
            conn.execute("SELECT * FROM source_coverage WHERE source = ?", (table,)).fetchall()
            == expected_coverage
        )


@pytest.mark.parametrize(
    ("source", "target", "outside", "table", "date_column", "coverage_source"),
    [
        (
            "financial_summaries",
            DAY,
            "2026-10-09",
            "jquants_fin_summaries",
            "disclosed_at",
            "jquants_fin_summaries",
        ),
        (
            "short_sale_reports",
            DAY,
            "2026-10-09",
            "jquants_short_sale_reports",
            "disclosed_at",
            "jquants_short_sale_reports",
        ),
        (
            "margin_interest",
            "2026-07-24",
            "2026-07-17",
            "jquants_weekly_margin",
            "week_end",
            "jquants_weekly_margin",
        ),
        (
            "margin_interest",
            "2026-10-02",
            "2026-09-25",
            "jquants_all_issues_daily_margin",
            "balance_date",
            "jquants_all_issues_daily_margin",
        ),
        (
            "margin_alerts",
            DAY,
            "2026-10-09",
            "jquants_margin_alerts",
            "publication_date",
            "jquants_margin_alerts",
        ),
    ],
    ids=("financial", "short-sale", "weekly-margin", "daily-margin", "margin-alert"),
)
def test_complete_empty_receipt_projects_identically_with_or_without_old_rows(
    tmp_path, source, target, outside, table, date_column, coverage_source
):
    empty_body = body([])
    target_coverage = []
    for had_rows in (False, True):
        db = tmp_path / ("populated.sqlite" if had_rows else "fresh.sqlite")
        if had_rows:
            for day in (outside, target):
                key = "disc_date" if source == "short_sale_reports" else "date"
                request_id = receive(
                    db, source, {key: day}, body([source_record(source, day)]), FIRST_AT
                )
                assert project_request(db, request_id) == 1
        key = "disc_date" if source == "short_sale_reports" else "date"
        request_id = receive(db, source, {key: target}, empty_body, CORRECTION_AT)
        assert project_request(db, request_id) == 0

        with sqlite3.connect(db) as conn:
            assert conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {date_column} = ?", (target,)
            ).fetchone() == (0,)
            if had_rows:
                assert conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {date_column} = ?", (outside,)
                ).fetchone() == (1,)
            pages = read_request_pages(conn, request_id)
            assert len(pages) == 1
            assert pages[0].body == empty_body
            assert pages[0].received_at == CORRECTION_AT
        selected = [
            row for row in coverage(db, coverage_source) if row[1] == target and row[2] == target
        ]
        assert len(selected) == 1
        assert selected[0][3:] == (CORRECTION_AT.isoformat(timespec="microseconds"), 0, "ok", None)
        target_coverage.append(selected[0])
    assert target_coverage[0] == target_coverage[1]


def test_daily_bar_correction_and_same_receipt_replay_are_stable(db_path):
    first_body = body([{"Code": "13010", "Date": DAY, "C": 100.0, "Unknown": "raw"}])
    correction_body = body([{"Code": "13010", "Date": DAY, "C": 111.0}])
    first_id = receive(db_path, "daily_bars", {"date": DAY}, first_body, FIRST_AT)
    correction_id = receive(db_path, "daily_bars", {"date": DAY}, correction_body, CORRECTION_AT)

    assert project_request(db_path, first_id) == 1
    assert project_request(db_path, correction_id) == 1
    with sqlite3.connect(db_path) as conn:
        corrected_rows = conn.execute(
            "SELECT ticker, traded_at, close FROM jquants_daily_bars ORDER BY ticker"
        ).fetchall()
    corrected_coverage = coverage(db_path, "jquants_daily_bars")
    assert corrected_rows == [("1301", DAY, 111.0)]
    assert corrected_coverage == [
        (
            f"get_eq_bars_daily_range:{DAY}..{DAY}",
            DAY,
            DAY,
            CORRECTION_AT.isoformat(timespec="microseconds"),
            1,
            "ok",
            None,
        )
    ]

    assert project_request(db_path, correction_id) == 1
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute(
                "SELECT ticker, traded_at, close FROM jquants_daily_bars ORDER BY ticker"
            ).fetchall()
            == corrected_rows
        )
        assert [page.body for page in read_request_pages(conn, first_id)] == [first_body]
    assert coverage(db_path, "jquants_daily_bars") == corrected_coverage


def test_financial_correction_and_same_receipt_replay_are_stable(db_path):
    original = {
        "Code": "13010",
        "DiscDate": DAY,
        "DiscNo": "document-one",
        "CurPerType": "FY",
        "EPS": 10.0,
        "Unknown": None,
    }
    corrected = {**original, "EPS": 12.5}
    original_body = body([original])
    correction_body = body([corrected])
    original_id = receive(db_path, "financial_summaries", {"date": DAY}, original_body, FIRST_AT)
    correction_id = receive(
        db_path, "financial_summaries", {"date": DAY}, correction_body, CORRECTION_AT
    )

    assert project_request(db_path, original_id) == 1
    assert project_request(db_path, correction_id) == 1
    with sqlite3.connect(db_path) as conn:
        corrected_rows = conn.execute(
            "SELECT ticker, disclosed_at, disclosure_number, document_payload "
            "FROM jquants_fin_summaries ORDER BY ticker"
        ).fetchall()
    corrected_coverage = coverage(db_path, "jquants_fin_summaries")
    assert len(corrected_rows) == 1
    assert corrected_rows[0][:3] == ("1301", DAY, "document-one")
    assert json.loads(corrected_rows[0][3])["EPS"] == 12.5
    assert corrected_coverage == [
        (
            f"get_fin_summary_range:{DAY}..{DAY}",
            DAY,
            DAY,
            CORRECTION_AT.isoformat(timespec="microseconds"),
            1,
            "ok",
            None,
        )
    ]

    assert project_request(db_path, correction_id) == 1
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute(
                "SELECT ticker, disclosed_at, disclosure_number, document_payload "
                "FROM jquants_fin_summaries ORDER BY ticker"
            ).fetchall()
            == corrected_rows
        )
        assert [page.body for page in read_request_pages(conn, original_id)] == [original_body]
    assert coverage(db_path, "jquants_fin_summaries") == corrected_coverage


def test_code_filtered_query_cannot_replace_whole_day(db_path):
    full_id = receive(
        db_path,
        "daily_bars",
        {"date": DAY},
        body([{"Code": "13010", "Date": DAY, "C": 100.0}]),
        FIRST_AT,
    )
    filtered_id = receive(
        db_path,
        "daily_bars",
        {"date": DAY, "code": "13010"},
        body([{"Code": "13010", "Date": DAY, "C": 999.0}]),
        CORRECTION_AT,
    )
    assert project_request(db_path, full_id) == 1
    before_coverage = coverage(db_path, "jquants_daily_bars")

    with pytest.raises(ValueError, match="unfiltered explicit source date"):
        project_request(db_path, filtered_id)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT close FROM jquants_daily_bars").fetchall() == [(100.0,)]
    assert coverage(db_path, "jquants_daily_bars") == before_coverage


def test_incomplete_source_request_is_refused_before_projection(db_path):
    original_id = receive(
        db_path,
        "daily_bars",
        {"date": DAY},
        body([{"Code": "13010", "Date": DAY, "C": 100.0}]),
        FIRST_AT,
    )
    assert project_request(db_path, original_id) == 1
    before_coverage = coverage(db_path, "jquants_daily_bars")
    session = Session(
        body([{"Code": "13010", "Date": DAY, "C": 100.0}], cursor="next"),
        requests.ConnectionError("synthetic-test-key"),
    )
    client = JQuantsHTTP("synthetic-test-key", session=session, clock=lambda: FIRST_AT)
    conn = open_connection(db_path)
    try:
        with pytest.raises(JQuantsAcquisitionError, match="ConnectionError") as caught:
            acquire_request(conn, client, "daily_bars", {"date": DAY})
        request_id = caught.value.request_id
    finally:
        conn.close()
    assert request_id is not None

    with pytest.raises(JQuantsAcquisitionError, match="incomplete"):
        project_request(db_path, request_id)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT close FROM jquants_daily_bars").fetchall() == [(100.0,)]
        assert len(read_request_pages(conn, request_id)) == 1
    assert coverage(db_path, "jquants_daily_bars") == before_coverage


def test_invalid_financial_record_keeps_partial_coverage_after_raw_projection(db_path):
    original_id = receive(
        db_path,
        "financial_summaries",
        {"date": DAY},
        body([source_record("financial_summaries", DAY)]),
        FIRST_AT,
    )
    assert project_request(db_path, original_id) == 1
    invalid_body = body([{"Code": "", "DiscDate": DAY, "DiscNo": "invalid-code"}])
    invalid_id = receive(db_path, "financial_summaries", {"date": DAY}, invalid_body, CORRECTION_AT)
    assert project_request(db_path, invalid_id) == 0
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jquants_fin_summaries").fetchone() == (0,)
        assert read_request_pages(conn, invalid_id)[0].body == invalid_body
    assert coverage(db_path, "jquants_fin_summaries") == [
        (
            f"get_fin_summary_range:{DAY}..{DAY}",
            DAY,
            DAY,
            CORRECTION_AT.isoformat(timespec="microseconds"),
            0,
            "partial",
            "1 rejected records during normalization",
        )
    ]


def test_failed_projection_keeps_retained_raw_body(db_path):
    invalid_body = body([{"Code": "13010", "Date": "2026-10-15", "C": 100.0}])
    request_id = receive(db_path, "daily_bars", {"date": DAY}, invalid_body, FIRST_AT)

    with pytest.raises(ValueError, match="outside the requested range"):
        project_request(db_path, request_id)
    with sqlite3.connect(db_path) as conn:
        assert [page.body for page in read_request_pages(conn, request_id)] == [invalid_body]
        assert read_complete_request(conn, request_id).records() == [
            {"Code": "13010", "Date": "2026-10-15", "C": 100.0}
        ]
        assert conn.execute("SELECT COUNT(*) FROM jquants_daily_bars").fetchone()[0] == 0


def test_offline_reader_rejects_missing_input_without_store_or_http(tmp_path, monkeypatch):
    database = tmp_path / "absent.sqlite"
    reader = JQuantsReader(database)
    day = date.fromisoformat(DAY)

    def unexpected_http(*args, **kwargs):
        pytest.fail("offline reader attempted HTTP")

    monkeypatch.setattr(requests.Session, "get", unexpected_http)
    for read in (
        lambda: reader.get_eq_bars_daily_range(day, day),
        lambda: reader.get_adjustment_factor_bars_range(day, day),
        lambda: reader.get_mkt_calendar(day, day),
        lambda: reader.get_eq_master(day),
        lambda: reader.get_fin_summary_range(day, day),
    ):
        with pytest.raises(JQuantsProviderError, match="Stored J-Quants input is incomplete"):
            read()
    assert not database.exists()


def test_offline_reader_uses_projected_rows_without_writing(db_path, monkeypatch):
    day = date.fromisoformat(DAY)
    bars_id = receive(
        db_path,
        "daily_bars",
        {"date": DAY},
        body(
            [
                {"Code": "13010", "Date": DAY, "C": 100.0, "AdjFactor": 1.0},
                {"Code": "130A0", "Date": DAY, "C": None, "AdjFactor": 0.5, "ExRT": "1"},
            ]
        ),
        FIRST_AT,
    )
    financial_id = receive(
        db_path,
        "financial_summaries",
        {"date": DAY},
        body(
            [
                {"Code": "13010", "DiscDate": DAY, "DiscNo": "fy", "CurPerType": "FY"},
                {"Code": "130A0", "DiscDate": DAY, "DiscNo": "q1", "CurPerType": "1Q"},
            ]
        ),
        FIRST_AT,
    )
    calendar_id = receive(
        db_path,
        "market_calendar",
        {"from": DAY, "to": DAY},
        body([{"Date": DAY, "HolDiv": "1"}]),
        FIRST_AT,
    )
    for request_id in (bars_id, financial_id, calendar_id):
        project_request(db_path, request_id)
    original_db = db_path.read_bytes()

    def unexpected_http(*args, **kwargs):
        pytest.fail("offline reader attempted HTTP")

    monkeypatch.setattr(requests.Session, "get", unexpected_http)
    reader = JQuantsReader(db_path)
    assert [(bar.ticker, bar.close) for bar in reader.get_eq_bars_daily_range(day, day)] == [
        ("1301", 100.0)
    ]
    events = reader.get_adjustment_factor_bars_range(day, day)
    assert [(event.ticker, event.adjustment_factor, event.ex_right_type) for event in events] == [
        ("130A", 0.5, "1")
    ]
    assert [row.fiscal_period for row in reader.get_fin_summary_range(day, day)] == ["FY", "1Q"]
    assert [row.fiscal_period for row in reader.get_fy_summary_range(day, day)] == ["FY"]
    assert reader.get_mkt_calendar(day, day)[0].is_business_day
    assert db_path.read_bytes() == original_db


def _margin_alert_records_2018() -> list[dict[str, object]]:
    """Two real-shaped balances published for code 13090 on the same day."""
    reason = {
        "DailyPublication": "0",
        "Monitoring": "0",
        "PrecautionByJSF": "1",
        "Restricted": "0",
        "RestrictedByJSF": "0",
        "UnclearOrSecOnAlert": "0",
    }
    later = {
        "AppDate": "2018-08-09",
        "Code": "13090",
        "LongNegOut": 1627.0,
        "LongNegOutChg": 2.0,
        "LongOut": 3130.0,
        "LongOutChg": 3.0,
        "LongOutRatio": "*",
        "LongStdOut": 1503.0,
        "LongStdOutChg": 1.0,
        "PubDate": "2018-08-10",
        "PubReason": reason,
        "SLRatio": 34.7,
        "ShrtNegOut": 0.0,
        "ShrtNegOutChg": 0.0,
        "ShrtOut": 1086.0,
        "ShrtOutChg": -37.0,
        "ShrtOutRatio": "*",
        "ShrtStdOut": 1086.0,
        "ShrtStdOutChg": -37.0,
        "TSEMrgnRegCls": "001",
    }
    earlier = {
        **later,
        "AppDate": "2018-08-08",
        "LongNegOut": 1625.0,
        "LongNegOutChg": 5.0,
        "LongOut": 3127.0,
        "LongOutChg": -48.0,
        "LongStdOut": 1502.0,
        "LongStdOutChg": -53.0,
        "SLRatio": 35.9,
        "ShrtOut": 1123.0,
        "ShrtOutChg": -21.0,
        "ShrtStdOut": 1123.0,
        "ShrtStdOutChg": -21.0,
    }
    return [later, earlier]


def test_margin_alert_offline_replay_keeps_distinct_application_dates(db_path, monkeypatch):
    records = _margin_alert_records_2018()
    raw = body([*records, records[0]])
    request_id = receive(db_path, "margin_alerts", {"date": "2018-08-10"}, raw, FIRST_AT)
    with sqlite3.connect(db_path) as conn:
        request = read_complete_request(conn, request_id)
        assert [page.body for page in request.pages] == [raw]
        assert request.records() == [*records, records[0]]
        assert request.records()[0]["PubReason"] == records[0]["PubReason"]
        assert request.records()[0]["LongOutRatio"] == "*"

    def no_http(*_args, **_kwargs):
        pytest.fail("offline replay attempted HTTP")

    monkeypatch.setattr(requests.Session, "get", no_http)
    assert project_request(db_path, request_id) == 2
    assert project_request(db_path, request_id) == 2
    alerts = read_margin_alerts(db_path, date(2018, 8, 10), date(2018, 8, 10))
    assert alerts is not None
    assert [(row.ticker, row.applied_date, row.short_outstanding) for row in alerts] == [
        ("1309", date(2018, 8, 8), 1123.0),
        ("1309", date(2018, 8, 9), 1086.0),
    ]
    assert all(row.publication_reason == str(records[0]["PubReason"]) for row in alerts)
    assert all(row.short_ratio is None and row.long_ratio is None for row in alerts)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jquants_margin_alerts").fetchone() == (2,)


def test_margin_alert_later_publication_retains_correction_to_old_application(db_path):
    later, earlier = _margin_alert_records_2018()
    first_id = receive(
        db_path, "margin_alerts", {"date": "2018-08-10"}, body([later, earlier]), FIRST_AT
    )
    assert project_request(db_path, first_id) == 2
    correction = {**earlier, "PubDate": "2018-08-11", "LongOut": 3131.0}
    correction_id = receive(
        db_path, "margin_alerts", {"date": "2018-08-11"}, body([correction]), CORRECTION_AT
    )
    assert project_request(db_path, correction_id) == 3
    alerts = read_margin_alerts(db_path, date(2018, 8, 10), date(2018, 8, 11))
    assert alerts is not None
    assert [(row.publication_date, row.applied_date, row.long_outstanding) for row in alerts] == [
        (date(2018, 8, 10), date(2018, 8, 8), 3127.0),
        (date(2018, 8, 10), date(2018, 8, 9), 3130.0),
        (date(2018, 8, 11), date(2018, 8, 8), 3131.0),
    ]


def test_margin_alert_same_three_column_identity_conflict_preserves_stored_rows(db_path):
    later, earlier = _margin_alert_records_2018()
    first_id = receive(
        db_path, "margin_alerts", {"date": "2018-08-10"}, body([later, earlier]), FIRST_AT
    )
    assert project_request(db_path, first_id) == 2
    with sqlite3.connect(db_path) as conn:
        before_rows = conn.execute(
            "SELECT * FROM jquants_margin_alerts ORDER BY applied_date"
        ).fetchall()
        before_coverage = coverage(db_path, "jquants_margin_alerts")
    conflicting = {**earlier, "LongOut": 9999.0}
    conflict_id = receive(
        db_path,
        "margin_alerts",
        {"date": "2018-08-10"},
        body([later, earlier, conflicting]),
        CORRECTION_AT,
    )
    with pytest.raises(JQuantsProviderError, match="conflicting margin alerts observation"):
        project_request(db_path, conflict_id)
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute("SELECT * FROM jquants_margin_alerts ORDER BY applied_date").fetchall()
            == before_rows
        )
        assert [page.body for page in read_request_pages(conn, conflict_id)] == [
            body([later, earlier, conflicting])
        ]
    assert coverage(db_path, "jquants_margin_alerts") == before_coverage


def test_margin_alert_lake_round_trip_preserves_three_column_identity(
    db_path, tmp_path, monkeypatch
):
    narrow_release_policy(monkeypatch, datasets=("jquants.margin_alerts",), carries_history=None)
    records = _margin_alert_records_2018()
    request_id = receive(db_path, "margin_alerts", {"date": "2018-08-10"}, body(records), FIRST_AT)
    assert project_request(db_path, request_id) == 2
    mirror = tmp_path / "mirror"
    with sealed_sqlite_snapshot(sqlite_path=db_path, mirror_root=mirror) as snapshot:
        exported = export_dataset_snapshot(
            dataset_name="jquants.margin_alerts",
            mirror_root=mirror,
            producer_git_commit="a" * 40,
            source_snapshot=snapshot,
            build_id="margin-alert-three-column-identity",
        )
    assert JQUANTS_MARGIN_ALERTS.primary_key == ("publication_date", "ticker", "applied_date")
    assert exported.manifest.contract_version == 2
    assert JQUANTS_MARGIN_ALERTS.columns[2].nullable is False
    object_key = exported.manifest.partitions[0].objects[0].key
    assert "contract=v2" in object_key
    arrow_schema = pq.read_schema(mirror / object_key)
    assert arrow_schema.field("applied_date").nullable is False
    assert arrow_schema.metadata is not None
    assert arrow_schema.metadata[b"baibai.primary_key"] == b"publication_date,ticker,applied_date"
    manifest_path, manifest = create_release(
        dataset_manifest_paths=[exported.manifest_path],
        mirror_root=mirror,
        release_id="margin-alert-three-column-identity",
    )
    source = LocalMirrorSource(mirror)
    release = resolve_release(
        source,
        release_id=manifest.release_id,
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )
    rebuilt = tmp_path / "rebuilt.sqlite"
    open_connection(rebuilt).close()
    with lake_session() as session:
        report = hydrate_market_store(
            session,
            release=release,
            cache=LakeObjectCache(root=mirror, source=source),
            store=rebuilt,
            dataset_names=("jquants.margin_alerts",),
        )
    assert report.rows == {"jquants.margin_alerts": 2}
    with sqlite3.connect(db_path) as original, sqlite3.connect(rebuilt) as hydrated:
        assert (
            hydrated.execute("SELECT * FROM jquants_margin_alerts ORDER BY applied_date").fetchall()
            == original.execute(
                "SELECT * FROM jquants_margin_alerts ORDER BY applied_date"
            ).fetchall()
        )
        assert [
            (row[3], row[5])
            for row in hydrated.execute("PRAGMA table_info(jquants_margin_alerts)").fetchall()
        ][:3] == [(1, 1), (1, 2), (1, 3)]
