"""Offline replay from retained J-Quants pages into the current market schema."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime

import pytest
import requests

from baibai_engine.collection.jquants_http import JQuantsAcquisitionError, JQuantsHTTP
from baibai_engine.collection.jquants_receipts import (
    acquire_request,
    read_complete_request,
    read_request_pages,
)
from baibai_engine.market.jquants_decode import JQuantsProviderError
from baibai_engine.market.jquants_projection import project_request
from baibai_engine.market.jquants_reader import JQuantsReader
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
        assert conn.execute("SELECT COUNT(*) FROM jquants_daily_bars").fetchone()[0] == 0
        assert len(read_request_pages(conn, request_id)) == 1


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
