"""Offline checks for raw J-Quants page reception before DataFrame conversion."""

from __future__ import annotations

import io
import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from tests.helpers.screening_sqlite import make_master_records
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.exceptions import ReadTimeoutError
from urllib3.response import HTTPResponse

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsAcquisitionError,
    JQuantsRateLimitError,
    page_records,
)
from baibai_engine.data_collection.providers.jquants.http import (
    JQuantsHTTP,
)
from baibai_engine.data_collection.providers.jquants.receipts import (
    read_complete_request,
    read_request_pages,
)
from baibai_engine.data_collection.transport.http import AcquisitionDeadlineExceeded
from baibai_engine.machine_analysis.observations.stocks.reader import read_eq_master_exact
from baibai_engine.storage.sqlite.source_cache.coverage import range_covered
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.maintenance.market.cache import (
    backfill_history_command,
    backfill_master_command,
    bootstrap_cache_command,
)
from baibai_jobs.maintenance.market.providers import ProviderBundle
from baibai_jobs.maintenance.source_refresh.jquants import JQuantsRefresh

RECEIVED_AT = datetime(2026, 10, 17, 12, tzinfo=UTC)


def payload(data, *, cursor=None):
    value = {"data": data}
    if cursor is not None:
        value["pagination_key"] = cursor
    return json.dumps(value, ensure_ascii=False).encode()


class Session(requests.Session):
    def __init__(self, *replies):
        super().__init__()
        self.replies = iter(replies)
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        status, body = reply
        result = requests.Response()
        result.status_code = status
        result._content = body
        result._content_consumed = True
        return result

    def close(self):
        self.closed = True
        super().close()


def receiver(session):
    return JQuantsHTTP("synthetic-test-key", session=session, clock=lambda: RECEIVED_AT)


@pytest.mark.parametrize(
    ("source", "params", "path"),
    [
        ("master", {"code": "9682"}, "/equities/master"),
        ("daily_bars", {"date": "2026-10-16"}, "/equities/bars/daily"),
        ("financial_summaries", {"date": "20261016"}, "/fins/summary"),
        ("market_calendar", {"hol_div": "1"}, "/markets/calendar"),
        ("margin_interest", {"date": "2026-10-16"}, "/markets/margin-interest"),
        ("margin_alerts", {"date": "2026-10-16"}, "/markets/margin-alert"),
        (
            "short_sale_reports",
            {"disc_date_from": "2026-10-01"},
            "/markets/short-sale-report",
        ),
    ],
)
def test_source_uses_fixed_endpoint_and_source_query(source, params, path):
    session = Session((200, payload([])))
    assert len(list(receiver(session).pages(source, params))) == 1
    assert session.calls[0][0] == "https://api.jquants.com/v2" + path
    assert session.calls[0][1]["params"] == params


def test_exact_body_unknown_fields_and_original_json_types_survive():
    body = (
        b' { "data": [{"Code":"9682","Unexpected":{"raw":[null,"42"]},'
        b'"Price":"1.00","Quantity":0,"Missing":null}],"extra":true}\n'
    )
    session = Session((200, body))
    page = next(receiver(session).pages("daily_bars", {"date": "2026-10-16"}))

    assert page.body == body
    assert page.received_at == RECEIVED_AT
    assert page_records(page) == [
        {
            "Code": "9682",
            "Unexpected": {"raw": [None, "42"]},
            "Price": "1.00",
            "Quantity": 0,
            "Missing": None,
        }
    ]
    assert session.calls == [
        (
            "https://api.jquants.com/v2/equities/bars/daily",
            {
                "params": {"date": "2026-10-16"},
                "headers": {"x-api-key": "synthetic-test-key"},
                "timeout": 30,
                "allow_redirects": False,
                "stream": True,
            },
        )
    ]


def test_two_pages_preserve_base_params_and_receipt_query():
    session = Session(
        (200, payload([{"Code": "9682"}], cursor="page-two")),
        (200, payload([{"Code": "9709"}])),
    )
    params = {"code": "9682", "disc_date_from": "2026-10-01"}
    pages = list(receiver(session).pages("short_sale_reports", params))

    assert [page_records(page)[0]["Code"] for page in pages] == ["9682", "9709"]
    assert dict(pages[0].query) == params
    assert dict(pages[1].query) == {**params, "pagination_key": "page-two"}
    assert params == {"code": "9682", "disc_date_from": "2026-10-01"}
    assert [kwargs["params"] for _, kwargs in session.calls] == [
        params,
        {**params, "pagination_key": "page-two"},
    ]
    with pytest.raises(TypeError):
        pages[0].query["code"] = "changed"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"{", "not JSON"),
        (b'{"data":{}}', "lacks a data array"),
        (b'{"data":[7]}', "non-record row"),
    ],
)
def test_body_is_yielded_before_invalid_json_or_envelope_raises(body, message):
    session = Session((200, body))
    pages = receiver(session).pages("master", {})
    assert next(pages).body == body
    with pytest.raises(JQuantsAcquisitionError, match=message):
        next(pages)
    assert len(session.calls) == 1


def test_repeated_cursor_is_rejected_after_second_page_receipt():
    session = Session(
        (200, payload([], cursor="repeat")),
        (200, payload([], cursor="repeat")),
    )
    pages = receiver(session).pages("market_calendar", {})
    assert next(pages).body == payload([], cursor="repeat")
    assert next(pages).body == payload([], cursor="repeat")
    with pytest.raises(JQuantsAcquisitionError, match="repeated"):
        next(pages)
    assert len(session.calls) == 2


@pytest.mark.parametrize(
    ("source", "params"),
    [
        ("unsupported", {}),
        ("master", {"pagination_key": "injected"}),
        ("master", {"disc_date": "2026-10-17"}),
        ("master", {"date": 42}),
    ],
)
def test_unsupported_source_or_query_fails_before_http(source, params):
    session = Session()
    with pytest.raises(ValueError, match="unsupported"):
        list(receiver(session).pages(source, params))
    assert session.calls == []


@pytest.mark.parametrize("status", [301, 302, 401, 403, 404, 500])
def test_redirect_and_non_200_status_are_rejected(status):
    session = Session((status, b"private body"))
    with pytest.raises(JQuantsAcquisitionError, match=f"HTTP status {status}"):
        list(receiver(session).pages("master", {}))
    assert len(session.calls) == 1
    assert session.calls[0][1]["allow_redirects"] is False


def test_request_exception_text_cannot_escape_and_caller_session_stays_open():
    session = Session(requests.ConnectionError("synthetic-test-key in request details"))
    client = receiver(session)
    with pytest.raises(JQuantsAcquisitionError, match="ConnectionError") as caught:
        list(client.pages("master", {}))
    assert "synthetic-test-key" not in str(caught.value)
    assert "synthetic-test-key" not in repr(caught.value)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    client.close()
    assert not session.closed


@pytest.fixture
def wire(monkeypatch):
    class Wire:
        calls = []
        waits = []
        reply = None

        def send(self, pool, connection, method, url, **kwargs):
            self.calls.append((method, url))
            assert method == "GET"
            assert kwargs["headers"]["x-api-key"] == "synthetic-test-key"
            status, headers, body = self.reply(url)
            return HTTPResponse(
                body=body if hasattr(body, "read") else io.BytesIO(body),
                status=status,
                headers={"Content-Type": "application/json", **headers},
                preload_content=False,
            )

    value = Wire()
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setattr(
        HTTPSConnectionPool,
        "_make_request",
        lambda pool, connection, method, url, **kwargs: value.send(
            pool, connection, method, url, **kwargs
        ),
    )
    monkeypatch.setattr("time.sleep", value.waits.append)

    def no_socket(*args, **kwargs):
        pytest.fail("offline transport must never connect")

    monkeypatch.setattr("socket.socket.connect", no_socket)
    return value


def test_job_cutoff_refuses_retry_after_without_another_request(wire, monkeypatch):
    monkeypatch.setattr("time.time", lambda: 100.0)
    monkeypatch.setenv("BAIBAI_ACQUISITION_DEADLINE", "110")
    wire.reply = lambda url: (429, {"Retry-After": "20"}, b"{}")
    client = JQuantsHTTP("synthetic-test-key")
    try:
        with pytest.raises(AcquisitionDeadlineExceeded):
            list(client.pages("master", {}))
    finally:
        client.close()
    assert len(wire.calls) == 1
    assert wire.waits == []


def test_each_get_retries_independently_with_backoff_and_retry_after(wire):
    attempts = {"first": 0, "second": 0}

    def reply(url):
        page = "second" if "pagination_key" in parse_qs(urlsplit(url).query) else "first"
        attempts[page] += 1
        if page == "first" and attempts[page] == 1:
            return 429, {"Retry-After": "17"}, b"{}"
        if page == "second" and attempts[page] == 1:
            return 503, {}, b"{}"
        return 200, {}, payload([], cursor="next" if page == "first" else None)

    wire.reply = reply
    client = JQuantsHTTP("synthetic-test-key", clock=lambda: RECEIVED_AT)
    try:
        assert len(list(client.pages("master", {}))) == 2
    finally:
        client.close()
    assert attempts == {"first": 2, "second": 2}
    assert wire.waits == [17, 30]


@pytest.mark.parametrize(
    ("retry_after", "waits"),
    [("0", []), ("invalid", [30]), ("17", [17])],
)
def test_retry_after_override_and_invalid_header_fallback(wire, retry_after, waits):
    def reply(url):
        if len(wire.calls) == 1:
            return 429, {"Retry-After": retry_after}, b"{}"
        return 200, {}, payload([])

    wire.reply = reply
    client = JQuantsHTTP("synthetic-test-key", clock=lambda: RECEIVED_AT)
    try:
        assert len(list(client.pages("master", {}))) == 1
    finally:
        client.close()
    assert len(wire.calls) == 2
    assert wire.waits == waits


def test_http_date_retry_after_precedes_default_backoff(wire, monkeypatch):
    base = datetime(2026, 10, 17, 12, tzinfo=UTC)
    monkeypatch.setattr("time.time", lambda: base.timestamp())
    header = format_datetime(base + timedelta(seconds=60), usegmt=True)
    wire.reply = lambda url: (
        (429, {"Retry-After": header}, b"{}") if len(wire.calls) == 1 else (200, {}, payload([]))
    )
    client = JQuantsHTTP("synthetic-test-key", clock=lambda: RECEIVED_AT)
    try:
        assert len(list(client.pages("master", {}))) == 1
    finally:
        client.close()
    assert len(wire.calls) == 2
    assert wire.waits == [60]


def test_timeout_before_response_has_finite_get_retry_budget(wire):
    def reply(url):
        raise ReadTimeoutError(None, url, "synthetic timeout")

    wire.reply = reply
    client = JQuantsHTTP("synthetic-test-key", clock=lambda: RECEIVED_AT)
    try:
        with pytest.raises(JQuantsAcquisitionError, match="ConnectionError"):
            list(client.pages("master", {}))
    finally:
        client.close()
    assert len(wire.calls) == 6
    assert wire.waits == [30, 60, 120, 300, 600]


def test_body_timeout_retains_complete_pages_and_explicit_retry_projects_once(wire, tmp_path):
    class InterruptedBody(io.BytesIO):
        prefix_read = False

        def read(self, *args, **kwargs):
            if not self.prefix_read:
                self.prefix_read = True
                return super().read(*args, **kwargs)
            raise ReadTimeoutError(None, None, "synthetic-test-key in body timeout details")

    target = date(2026, 10, 16)
    first = payload(
        [{"Code": "13010", "Date": target.isoformat(), "C": 100, "AdjFactor": 1.0}],
        cursor="next",
    )
    second = payload([{"Code": "72030", "Date": target.isoformat(), "C": 200, "AdjFactor": 1.0}])
    interrupted = InterruptedBody(second[:20])
    wire.reply = lambda url: (
        200,
        {},
        interrupted if "pagination_key" in parse_qs(urlsplit(url).query) else first,
    )
    database = tmp_path / "market.sqlite"
    open_connection(database).close()
    client = JQuantsHTTP("synthetic-test-key", clock=lambda: RECEIVED_AT)
    maintenance = JQuantsRefresh(database, None, client=client)
    try:
        with pytest.raises(JQuantsAcquisitionError, match="ConnectionError") as caught:
            maintenance.ensure_daily_bars(target, target)
        failed_id = caught.value.request_id
        assert failed_id is not None
        assert "synthetic-test-key" not in str(caught.value)
        assert caught.value.__context__ is None
        assert caught.value.__cause__ is None
        assert interrupted.prefix_read
        # Requests consumes the body after the adapter's retry boundary. A body
        # timeout fails this request; it does not silently restart either GET.
        assert len(wire.calls) == 2
        assert wire.waits == []
        with closing(sqlite3.connect(database)) as connection:
            assert [page.body for page in read_request_pages(connection, failed_id)] == [first]
            with pytest.raises(JQuantsAcquisitionError, match="incomplete"):
                read_complete_request(connection, failed_id)
            assert connection.execute("SELECT COUNT(*) FROM jquants_daily_bars").fetchone() == (0,)
            assert not range_covered(connection, "jquants_daily_bars", target, target)

        wire.reply = lambda url: (
            200,
            {},
            second if "pagination_key" in parse_qs(urlsplit(url).query) else first,
        )
        assert maintenance.ensure_daily_bars(target, target) == 2
    finally:
        maintenance.close()

    assert len(wire.calls) == 4
    assert wire.waits == []
    with closing(sqlite3.connect(database)) as connection:
        (resumed_id,) = connection.execute(
            "SELECT DISTINCT request_id FROM jquants_source_pages WHERE request_id != ?",
            (failed_id,),
        ).fetchone()
        assert [page.body for page in read_complete_request(connection, resumed_id).pages] == [
            first,
            second,
        ]
        assert [page.body for page in read_request_pages(connection, failed_id)] == [first]
        assert connection.execute(
            "SELECT ticker, close FROM jquants_daily_bars ORDER BY ticker"
        ).fetchall() == [("1301", 100.0), ("7203", 200.0)]
        assert range_covered(connection, "jquants_daily_bars", target, target)


@pytest.mark.parametrize("status", [401, 403, 404])
def test_permanent_status_does_not_retry_rate_limit_wording(wire, status):
    wire.reply = lambda url: (
        status,
        {"Retry-After": "17"},
        b'{"message":"429 Too Many Requests synthetic-test-key"}',
    )
    client = JQuantsHTTP("synthetic-test-key", clock=lambda: RECEIVED_AT)
    try:
        with pytest.raises(JQuantsAcquisitionError, match=f"HTTP status {status}") as caught:
            list(client.pages("master", {}))
    finally:
        client.close()
    assert len(wire.calls) == 1
    assert wire.waits == []
    assert "synthetic-test-key" not in str(caught.value)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"429 Too Many Requests", "not JSON"),
        (b'{"data":{}}', "lacks a data array"),
        (b'{"data":[123]}', "non-record row"),
    ],
)
def test_invalid_http_200_response_does_not_restart_get(wire, body, message):
    wire.reply = lambda url: (200, {}, body)
    client = JQuantsHTTP("synthetic-test-key", clock=lambda: RECEIVED_AT)
    try:
        with pytest.raises(JQuantsAcquisitionError, match=message):
            list(client.pages("master", {}))
    finally:
        client.close()
    assert len(wire.calls) == 1
    assert wire.waits == []


def test_second_get_exhausts_own_retry_budget_without_replaying_first(wire):
    def reply(url):
        if "pagination_key" not in parse_qs(urlsplit(url).query):
            return 200, {}, payload([], cursor="next")
        return 429, {}, b"{}"

    wire.reply = reply
    client = JQuantsHTTP("synthetic-test-key", clock=lambda: RECEIVED_AT)
    try:
        pages = client.pages("master", {})
        assert next(pages).body == payload([], cursor="next")
        with pytest.raises(JQuantsRateLimitError, match="HTTP 429") as caught:
            next(pages)
    finally:
        client.close()
    assert len(wire.calls) == 7
    assert wire.waits == [30, 60, 120, 300, 600]
    assert sum("pagination_key" not in parse_qs(urlsplit(url).query) for _, url in wire.calls) == 1
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("command", ["backfill-history", "backfill-master", "bootstrap-cache"])
def test_rate_limit_exhaustion_stops_next_endpoint_or_day_in_same_pass(
    wire, tmp_path, command, capsys
):
    def reply(url):
        assert len(wire.calls) <= 6, "shared quota must not receive another GET budget"
        return 429, {}, b'{"message":"synthetic-test-key"}'

    wire.reply = reply
    database = tmp_path / "market.sqlite"
    open_connection(database).close()
    client = JQuantsHTTP("synthetic-test-key", clock=lambda: RECEIVED_AT)
    maintenance = JQuantsRefresh(database, None, client=client)
    edinet = Mock()
    jpx = Mock()
    edinet.bootstrap_cache.return_value = {"ok": 1}
    jpx.bootstrap_cache.return_value = {"ok": 1}
    providers = ProviderBundle(jquants_refresh=maintenance, edinet_refresh=edinet, jpx_refresh=jpx)
    target = date(2026, 10, 16)
    try:
        if command == "backfill-history":
            code = backfill_history_command(
                start=target,
                end=target + timedelta(days=1),
                providers=providers,
                sqlite_path=database,
            )
        elif command == "backfill-master":
            code = backfill_master_command(
                asof_dates=[target, target + timedelta(days=1)],
                providers=providers,
                sqlite_path=database,
            )
        else:
            code = bootstrap_cache_command(
                asof_date=target, providers=providers, sqlite_path=database
            )
    finally:
        maintenance.close()

    assert code == 1
    assert len(wire.calls) == 6
    assert len({url for _, url in wire.calls}) == 1
    assert wire.waits == [30, 60, 120, 300, 600]
    error = capsys.readouterr().err
    assert "JQuantsRateLimitError" in error
    assert "synthetic-test-key" not in error
    if command == "bootstrap-cache":
        edinet.bootstrap_cache.assert_called_once()
        jpx.bootstrap_cache.assert_called_once_with(target)
    else:
        edinet.bootstrap_cache.assert_not_called()
        jpx.bootstrap_cache.assert_not_called()


@pytest.mark.parametrize("status", [429, 503])
def test_final_status_survives_an_unreadable_error_body(wire, status):
    class UnreadableBody(io.BytesIO):
        def read(self, *args, **kwargs):
            raise ReadTimeoutError(None, None, "synthetic-test-key in body timeout details")

    wire.reply = lambda url: (status, {}, UnreadableBody())
    client = JQuantsHTTP("synthetic-test-key", clock=lambda: RECEIVED_AT)
    try:
        with pytest.raises(JQuantsAcquisitionError) as caught:
            list(client.pages("master", {}))
    finally:
        client.close()
    assert isinstance(caught.value, JQuantsRateLimitError) == (status == 429)
    assert str(status) in str(caught.value)
    assert "synthetic-test-key" not in str(caught.value)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    assert len(wire.calls) == 6
    assert wire.waits == [30, 60, 120, 300, 600]


@pytest.mark.parametrize("failure", ["server", "schema"])
def test_non_quota_failure_keeps_next_master_date_recoverable(wire, tmp_path, failure):
    first = date(2026, 10, 15)
    second = date(2026, 10, 16)
    recovered = payload(make_master_records(second))

    def reply(url):
        requested = parse_qs(urlsplit(url).query)["date"]
        if requested == [first.isoformat()]:
            if failure == "server":
                return 503, {}, b'{"message":"429 Too Many Requests"}'
            return 200, {}, b'{"data":{}}'
        assert requested == [second.isoformat()]
        return 200, {}, recovered

    wire.reply = reply
    database = tmp_path / "market.sqlite"
    open_connection(database).close()
    client = JQuantsHTTP("synthetic-test-key", clock=lambda: RECEIVED_AT)
    maintenance = JQuantsRefresh(database, None, client=client)
    try:
        assert (
            backfill_master_command(
                asof_dates=[first, second],
                providers=ProviderBundle(jquants_refresh=maintenance),
                sqlite_path=database,
            )
            == 1
        )
    finally:
        maintenance.close()

    assert len(wire.calls) == (7 if failure == "server" else 2)
    assert parse_qs(urlsplit(wire.calls[-1][1]).query)["date"] == [second.isoformat()]
    assert wire.waits == ([30, 60, 120, 300, 600] if failure == "server" else [])
    assert read_eq_master_exact(database, first) is None
    assert read_eq_master_exact(database, second) is not None
