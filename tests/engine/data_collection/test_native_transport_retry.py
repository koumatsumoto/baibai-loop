"""Finite native GET retries must not replay a completed month or a parser."""

from __future__ import annotations

import io
from contextlib import closing
from datetime import UTC, date, datetime
from email.utils import format_datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from tests.helpers.network import _no_real_network as _no_real_network
from tests.helpers.provider_fixtures import _FakeBrowserFetcher
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.exceptions import ProtocolError, ReadTimeoutError
from urllib3.response import HTTPResponse

from baibai_engine.data_collection.acquisition.refresh import (
    SourceRefreshFailure,
    SourceRefreshSuccess,
    refresh_sources,
)
from baibai_engine.data_collection.providers.spglobal.pmi import fetch_pdf_bytes
from baibai_engine.data_collection.receipts.native_sources import store
from baibai_engine.data_collection.receipts.native_sources.records import read_snapshots
from baibai_engine.data_collection.transport.context import (
    BrowserUnavailableError,
    FetchContext,
    IndicatorsProviderError,
)

START = date(2026, 1, 31)
END = date(2026, 2, 1)
BASE = datetime(2026, 10, 13, tzinfo=UTC).timestamp()


class BodyTimeout(io.BytesIO):
    def read(self, *args, **kwargs):
        raise ReadTimeoutError(None, None, "synthetic body timeout")


def month_body(month):
    day, weighted, index_based = (
        ("2026.01.31", "0", "-") if month == "1" else ("2026.02.01", "-", "25.12")
    )
    return (
        f"<table><tr><td>{day}</td><td>{weighted}</td><td>{index_based}</td></tr></table>"
    ).encode()


def failed_response(kind):
    if kind == "body_timeout":
        return 200, {}, BodyTimeout(b"partial")
    if kind == "connection":
        raise ProtocolError("synthetic disconnect")
    if kind == "gzip":
        return 200, {"Content-Encoding": "gzip"}, b"not gzip"
    if kind == "schema":
        return 200, {}, b"<table><tr><td>2026.02.01</td></tr></table>"
    if kind == "utf8":
        return 200, {}, b"\xff"
    if kind == "oversized":
        return 200, {"Content-Length": "8000001"}, b"too large"
    return int(kind), {}, b"provider error"


@pytest.fixture
def wire(monkeypatch):
    class Wire:
        def __init__(self):
            self.calls = []
            self.waits = []
            self.second = lambda attempt: (200, {}, month_body("2"))

        def send(self, pool, conn, method, url, **kwargs):
            assert method == "GET"
            month = parse_qs(urlsplit(url).query).get("month", ["2"])[0]
            self.calls.append(month)
            status, headers, body = (
                (200, {}, month_body("1")) if month == "1" else self.second(self.calls.count("2"))
            )
            return HTTPResponse(
                body=body if hasattr(body, "read") else io.BytesIO(body),
                status=status,
                headers=headers,
                preload_content=False,
                pool=pool,
            )

        def refresh(self, path):
            return refresh_sources(["nikkei-per"], db_path=path, start=START, end=END)[0]

    wire = Wire()
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setattr(
        HTTPConnectionPool,
        "_make_request",
        lambda pool, conn, method, url, **kwargs: wire.send(pool, conn, method, url, **kwargs),
    )
    monkeypatch.setattr("time.sleep", wire.waits.append)
    monkeypatch.setattr("time.time", lambda: BASE)
    return wire


@pytest.mark.parametrize("kind", ["500", "502", "503", "504", "body_timeout", "connection", "gzip"])
def test_later_month_recovers_without_replaying_completed_month(wire, tmp_path, kind):
    wire.second = lambda attempt: (
        failed_response(kind) if attempt == 1 else (200, {}, month_body("2"))
    )
    path = tmp_path / "native.sqlite"
    outcome = wire.refresh(path)
    assert isinstance(outcome, SourceRefreshSuccess)
    assert outcome.record_count == 2
    assert wire.calls == ["1", "2", "2"]
    assert wire.waits == [1.0]
    with closing(store.open_read_only_connection(path)) as connection:
        rows = tuple(read_snapshots(connection, START, END, source_id="nikkei-per"))
        assert [dict(row.records[0]) for row in rows] == [
            {"date_text": "2026.01.31", "weighted_text": "0", "index_based_text": "-"},
            {"date_text": "2026.02.01", "weighted_text": "-", "index_based_text": "25.12"},
        ]


@pytest.mark.parametrize(
    ("kind", "attempts", "message"),
    [
        ("503", 2, "failed to fetch"),
        ("body_timeout", 2, "failed to fetch"),
        ("gzip", 2, "failed to fetch"),
        ("schema", 1, "three cells"),
        ("utf8", 1, "not UTF-8"),
        ("oversized", 1, "too large"),
        ("401", 1, "401"),
        ("403", 1, "withheld"),
        ("404", 1, "404"),
        ("429", 1, "rate-limited"),
    ],
)
def test_later_month_failure_is_finite_and_keeps_previous_native_receipts(
    wire, tmp_path, kind, attempts, message
):
    path = tmp_path / "native.sqlite"
    assert isinstance(wire.refresh(path), SourceRefreshSuccess)
    with closing(store.open_read_only_connection(path)) as connection:
        before = connection.execute("SELECT * FROM economic_source_records").fetchall()
    wire.calls.clear()
    wire.second = lambda attempt: failed_response(kind)
    outcome = wire.refresh(path)
    assert isinstance(outcome, SourceRefreshFailure)
    assert message in outcome.message
    assert wire.calls == ["1", *(["2"] * attempts)]
    assert wire.waits == ([1.0] if attempts == 2 else [])
    with closing(store.open_read_only_connection(path)) as connection:
        assert connection.execute("SELECT * FROM economic_source_records").fetchall() == before
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT status, record_count FROM economic_source_runs WHERE status = 'failed'"
            )
        ] == [("failed", 0)]


@pytest.mark.parametrize(
    ("header", "delay"),
    [
        ("17", 17),
        ("0", 0),
        (format_datetime(datetime.fromtimestamp(BASE + 60, UTC), usegmt=True), 60),
        ("invalid", 1.0),
    ],
)
def test_native_request_honors_retry_after_without_extending_its_attempt_budget(
    wire, tmp_path, header, delay
):
    wire.second = lambda attempt: (
        (503, {"Retry-After": header}, b"unavailable")
        if attempt == 1
        else (200, {}, month_body("2"))
    )
    assert isinstance(wire.refresh(tmp_path / "native.sqlite"), SourceRefreshSuccess)
    assert wire.calls == ["1", "2", "2"]
    assert wire.waits == ([delay] if delay else [])


def test_native_request_stops_before_retry_after_would_exhaust_job(wire, tmp_path, monkeypatch):
    wire.second = lambda attempt: (503, {"Retry-After": "20"}, b"unavailable")
    monkeypatch.setenv("BAIBAI_ACQUISITION_DEADLINE", str(BASE + 10))
    outcome = wire.refresh(tmp_path / "native.sqlite")
    assert isinstance(outcome, SourceRefreshFailure)
    assert "deadline exhausted" in outcome.message
    assert wire.calls == ["1", "2"]
    assert wire.waits == []


def test_document_transport_does_not_inherit_native_retry_budget(wire):
    wire.second = lambda attempt: failed_response("body_timeout")
    with FetchContext() as context, pytest.raises(IndicatorsProviderError, match="failed to fetch"):
        fetch_pdf_bytes(
            "https://example.test/release.pdf", session=context.session, context=context
        )
    assert wire.calls == ["2"]
    assert wire.waits == []


def test_retry_success_can_supersede_prior_day_with_an_empty_native_snapshot(wire, tmp_path):
    path = tmp_path / "native.sqlite"
    assert isinstance(wire.refresh(path), SourceRefreshSuccess)
    wire.calls.clear()
    wire.second = lambda attempt: (
        failed_response("503")
        if attempt == 1
        else (200, {}, month_body("2").replace(b"2026.02.01", b"2026.02.02"))
    )
    outcome = wire.refresh(path)
    assert isinstance(outcome, SourceRefreshSuccess)
    assert outcome.record_count == 1
    assert wire.calls == ["1", "2", "2"]
    with closing(store.open_read_only_connection(path)) as connection:
        (latest,) = read_snapshots(connection, END, END, source_id="nikkei-per")
        assert latest.records == ()


def test_h15_transient_http_failure_recovers_without_selecting_browser(wire, tmp_path, monkeypatch):
    wire.second = lambda attempt: (
        failed_response("503")
        if attempt == 1
        else (200, {}, b'"Time Period","RIFLGFCY10_N.B"\n2026-02-01,ND\n')
    )
    monkeypatch.setattr(
        FetchContext, "browser_fetcher", lambda self: pytest.fail("HTTP failure is not fallback")
    )
    outcome = refresh_sources(["fed-h15"], db_path=tmp_path / "native.sqlite", start=END, end=END)[
        0
    ]
    assert isinstance(outcome, SourceRefreshSuccess)
    assert outcome.record_count == 1
    assert wire.calls == ["2", "2"]
    assert wire.waits == [1.0]


def test_h15_does_not_replay_completed_plain_to_browser_selection(wire, tmp_path, monkeypatch):
    wire.second = lambda attempt: (200, {}, b"<html>not CSV</html>")
    browser = _FakeBrowserFetcher(BrowserUnavailableError("failed to launch"))
    contexts = []

    def fetch_browser(context):
        contexts.append(context)
        return browser

    monkeypatch.setattr(FetchContext, "browser_fetcher", fetch_browser)
    outcome = refresh_sources(["fed-h15"], db_path=tmp_path / "native.sqlite", start=END, end=END)[
        0
    ]
    assert isinstance(outcome, SourceRefreshFailure)
    assert "failed to launch" in outcome.message
    assert wire.calls == ["2"]
    assert wire.waits == []
    assert len(browser.urls) == 1
    assert contexts[0].blocked_browser_urls == {}
    assert contexts[0].bytes_cache == {}
