"""Exercise the installed SDK and urllib3 retry loop without opening a socket."""

from __future__ import annotations

import io
import json
from collections import Counter
from datetime import UTC, date, datetime
from email.utils import format_datetime
from urllib.parse import parse_qs, urlsplit

import jquantsapi
import pytest
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.exceptions import ReadTimeoutError
from urllib3.response import HTTPResponse

from baibai_engine.market.jquants_decode import JQuantsProviderError
from baibai_engine.market.providers.jquants import JQuantsProvider

BASE = datetime(2026, 10, 12, 12, tzinfo=UTC).timestamp()


@pytest.fixture
def wire(monkeypatch):
    class Transport:
        elapsed = 0.0
        calls = []
        waits = []
        timeouts = []
        reply = None

        def sleep(self, seconds):
            self.waits.append(seconds)
            self.elapsed += seconds

        def send(self, pool, connection, method, url, **kwargs):
            self.calls.append((method, url))
            assert method == "GET"
            assert kwargs["headers"]["x-api-key"] == "synthetic-key"
            self.timeouts.append(kwargs["timeout"])
            status, headers, payload = self.reply(url)
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            return HTTPResponse(
                body=io.BytesIO(body),
                status=status,
                headers={"Content-Type": "application/json", **headers},
                preload_content=False,
            )

    transport = Transport()
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setattr(jquantsapi.ClientV2, "_load_config", lambda self: {})
    monkeypatch.setattr(jquantsapi.ClientV2, "MAX_WORKERS", 1)  # deterministic range order
    monkeypatch.setattr(
        HTTPSConnectionPool,
        "_make_request",
        lambda pool, connection, method, url, **kwargs: transport.send(
            pool, connection, method, url, **kwargs
        ),
    )
    monkeypatch.setattr("time.sleep", transport.sleep)
    monkeypatch.setattr("time.time", lambda: BASE + transport.elapsed)

    def no_socket(*args, **kwargs):
        pytest.fail("offline transport must never connect")

    monkeypatch.setattr("socket.socket.connect", no_socket)
    transport.provider = JQuantsProvider("synthetic-key")
    try:
        yield transport
    finally:
        transport.provider._get_client()._request_session().close()


def calendar(wire):
    return wire.provider._load_or_fetch(
        "get_mkt_calendar", from_yyyymmdd="20260801", to_yyyymmdd="20260802"
    )


def success(day="2026-08-01", *, next_page=None):
    payload = {"data": [{"Date": day, "HolDiv": "1"}]}
    if next_page is not None:
        payload["pagination_key"] = next_page
    return 200, {}, payload


@pytest.mark.parametrize("failures", [1, 5, 6])
def test_same_http_has_initial_plus_five_retries_and_no_outer_restart(wire, failures):
    def reply(url):
        return (429, {}, {"message": "rate limit"}) if len(wire.calls) <= failures else success()

    wire.reply = reply
    if failures == 6:
        with pytest.raises(JQuantsProviderError, match="RetryError"):
            calendar(wire)
    else:
        assert len(calendar(wire)) == 1
    assert len(wire.calls) == min(failures + 1, 6)
    assert wire.waits == [30, 60, 120, 300, 600][: min(failures, 5)]
    assert len(set(wire.calls)) == 1


@pytest.mark.parametrize(
    ("header", "delay"),
    [
        ("17", 17),
        ("0", 0),
        (format_datetime(datetime.fromtimestamp(BASE + 60, UTC), usegmt=True), 60),
        (format_datetime(datetime.fromtimestamp(BASE - 60, UTC), usegmt=True), 0),
        ("invalid", 30),
    ],
)
def test_retry_after_seconds_or_http_date_precedes_existing_backoff(wire, header, delay):
    wire.reply = lambda url: (
        (429, {"Retry-After": header}, {"message": "rate limit"})
        if len(wire.calls) == 1
        else success()
    )
    assert len(calendar(wire)) == 1
    assert len(wire.calls) == 2
    assert wire.waits == ([] if delay == 0 else [delay])


def test_retry_after_does_not_increase_attempt_limit_or_wait_after_last_failure(wire):
    wire.reply = lambda url: (429, {"Retry-After": "2"}, {"message": "rate limit"})
    with pytest.raises(JQuantsProviderError, match="RetryError"):
        calendar(wire)
    assert len(wire.calls) == 6
    assert wire.waits == [2] * 5


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_existing_transient_get_statuses_still_retry(wire, status):
    wire.reply = lambda url: (status, {}, {}) if len(wire.calls) == 1 else success()
    assert len(calendar(wire)) == 1
    assert len(wire.calls) == 2
    assert wire.waits == [30]


def test_transient_get_read_timeout_is_finite(wire):
    def reply(url):
        raise ReadTimeoutError(None, url, "synthetic timeout")

    wire.reply = reply
    with pytest.raises(JQuantsProviderError, match="ConnectionError"):
        calendar(wire)
    assert len(wire.calls) == 6
    assert wire.waits == [30, 60, 120, 300, 600]


@pytest.mark.parametrize("status", [401, 403, 404])
def test_auth_and_permanent_http_failures_are_not_retried_even_with_rate_limit_text(wire, status):
    wire.reply = lambda url: (
        status,
        {"Retry-After": "17"},
        {"message": "429 Too Many Requests synthetic-key"},
    )
    with pytest.raises(JQuantsProviderError, match="HTTPError") as caught:
        calendar(wire)
    assert len(wire.calls) == 1
    assert wire.waits == []
    assert "synthetic-key" not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("body", [b"429 Too Many Requests", {"data": [123]}])
def test_http200_json_or_schema_failure_is_not_retried(wire, body):
    wire.reply = lambda url: (200, {}, body)
    with pytest.raises(JQuantsProviderError, match=r"JSONDecodeError|TypeError"):
        calendar(wire)
    assert len(wire.calls) == 1
    assert wire.waits == []


def test_later_page_failure_does_not_replay_successful_first_page(wire):
    def reply(url):
        if "pagination_key" not in parse_qs(urlsplit(url).query):
            return success(next_page="second")
        return 429, {"Retry-After": "0"}, {"message": "rate limit"}

    wire.reply = reply
    with pytest.raises(JQuantsProviderError, match="RetryError"):
        calendar(wire)
    counts = Counter(
        "second" if "pagination_key" in parse_qs(urlsplit(url).query) else "first"
        for _, url in wire.calls
    )
    assert counts == {"first": 1, "second": 6}
    assert wire.waits == []


def test_successful_pagination_keeps_sdk_payload_and_separate_page_retry_budgets(wire):
    counts = Counter()

    def reply(url):
        page = "second" if "pagination_key" in parse_qs(urlsplit(url).query) else "first"
        counts[page] += 1
        if counts[page] == 1:
            return 429, {"Retry-After": "0"}, {"message": "rate limit"}
        return success(
            "2026-08-02" if page == "second" else "2026-08-01",
            next_page=None if page == "second" else "second",
        )

    wire.reply = reply
    records = calendar(wire)
    assert len(records) == 2
    assert all(row["HolDiv"] == "1" for row in records)
    assert counts == {"first": 2, "second": 2}
    assert wire.waits == []


def test_sdk_range_days_are_distinct_requests_not_one_shared_retry_budget(wire):
    counts = Counter()

    def reply(url):
        day = parse_qs(urlsplit(url).query)["date"][0]
        counts[day] += 1
        if counts[day] <= 5:
            return 429, {"Retry-After": "0"}, {"message": "rate limit"}
        fields = dict.fromkeys(jquantsapi.constants.EQ_BARS_DAILY_COLUMNS_V2)
        fields.update(Code="13010", Date=day, C=100.0)
        return 200, {}, {"data": [fields]}

    wire.reply = reply
    records = wire.provider._load_or_fetch(
        "get_eq_bars_daily_range", start_dt=date(2026, 8, 1), end_dt=date(2026, 8, 2)
    )
    assert len(records) == 2
    assert list(counts.values()) == [6, 6]
    assert len(wire.calls) == 12
    assert wire.waits == []


def test_read_client_retains_normal_tls_proxy_pool_timeout_and_does_not_patch_sdk_globally(wire):
    wire.reply = lambda url: success()
    calendar(wire)
    client = wire.provider._get_client()
    session = client._request_session()
    assert session.trust_env
    assert session.verify is True
    assert all(timeout.connect_timeout == timeout.read_timeout == 30 for timeout in wire.timeouts)
    assert session.adapters["https://"]._pool_maxsize == client.MAX_WORKERS + 10
    assert session.adapters["https://"].max_retries.allowed_methods == frozenset({"GET"})
    with jquantsapi.ClientV2(api_key="other-synthetic-key")._request_session() as other_session:
        assert other_session.adapters["https://"].max_retries.total == 3
