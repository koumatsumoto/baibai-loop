"""Offline checks for raw J-Quants page reception before DataFrame conversion."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.exceptions import ReadTimeoutError
from urllib3.response import HTTPResponse

from baibai_engine.collection import jquants_http
from baibai_engine.collection.jquants_http import (
    JQuantsAcquisitionError,
    JQuantsHTTP,
    page_records,
)

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
                body=io.BytesIO(body),
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
    monkeypatch.setattr(jquants_http.time, "sleep", value.waits.append)

    def no_socket(*args, **kwargs):
        pytest.fail("offline transport must never connect")

    monkeypatch.setattr("socket.socket.connect", no_socket)
    return value


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
    monkeypatch.setattr(jquants_http.time, "time", lambda: base.timestamp())
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


def test_read_timeout_has_finite_get_retry_budget(wire):
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
        with pytest.raises(JQuantsAcquisitionError, match="RetryError"):
            next(pages)
    finally:
        client.close()
    assert len(wire.calls) == 7
    assert wire.waits == [30, 60, 120, 300, 600]
    assert sum("pagination_key" not in parse_qs(urlsplit(url).query) for _, url in wire.calls) == 1
