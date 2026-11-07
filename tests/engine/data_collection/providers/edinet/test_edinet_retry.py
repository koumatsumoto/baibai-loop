"""Exercise the actual GET boundary: HTTP and body 429 share six attempts."""

from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime
from io import BytesIO
from zipfile import ZipFile

import pytest
import requests
from tests.helpers.http_doubles import FakeSession, json_response, response

from baibai_engine.data_collection.providers.edinet import http
from baibai_engine.data_collection.providers.edinet.models import (
    EdinetAcquisitionError,
    EdinetRateLimitError,
)


@pytest.fixture
def clock(monkeypatch):
    current = datetime(2026, 10, 12, 3, 0, tzinfo=UTC)
    waits = []

    class Clock:
        @staticmethod
        def now(_tz):
            return current

    def sleep(seconds):
        nonlocal current
        waits.append(seconds)
        current += timedelta(seconds=seconds)

    monkeypatch.setattr(http, "datetime", Clock)
    monkeypatch.setattr(http.time, "sleep", sleep)
    return waits


def success(kind):
    if kind == "list":
        return json_response({"metadata": {"resultset": {"count": 0}}, "results": []})
    data = BytesIO()
    with ZipFile(data, "w") as archive:
        archive.writestr("document.csv", "unchanged source bytes")
    return response(data.getvalue())


def request(kind, session):
    receiver = http.EDINETHTTP("test-key", session=session)
    if kind == "list":
        return receiver.fetch_list(date(2026, 10, 9))
    return receiver.fetch_archive("S100TEST", 1 if kind == "xbrl" else 5)


def rate_limit(body=False, retry_after=None):
    result = response(b'{"StatusCode":429}' if body else b"", status_code=200 if body else 429)
    if retry_after is not None:
        result.headers["Retry-After"] = retry_after
    return result


@pytest.mark.parametrize("kind", ["csv", "xbrl", "list"])
def test_mixed_http_and_body_rate_limits_share_one_budget(kind, clock):
    session = FakeSession(*(rate_limit(body=bool(i % 2)) for i in range(6)))
    with pytest.raises(EdinetRateLimitError):
        request(kind, session)
    assert len(session.calls) == 6
    assert clock == [3, 6, 12, 24, 48]


@pytest.mark.parametrize("kind", ["csv", "xbrl", "list"])
def test_transient_failures_do_not_reset_the_rate_limit_budget(kind, clock):
    session = FakeSession(
        requests.ConnectionError("temporary connection failure"),
        response(status_code=503),
        rate_limit(body=True),
        rate_limit(),
        rate_limit(body=True),
        rate_limit(),
    )
    with pytest.raises(EdinetRateLimitError):
        request(kind, session)
    assert len(session.calls) == 6
    assert clock == [3, 6, 12, 24, 48]


@pytest.mark.parametrize("kind", ["csv", "xbrl", "list"])
def test_sixth_attempt_returns_exact_body_and_receipt(kind, clock):
    expected = success(kind)
    session = FakeSession(*(rate_limit(body=bool(i % 2)) for i in range(5)), expected)
    result = request(kind, session)
    assert result.body == expected.content
    assert result.received_at == datetime(2026, 10, 12, 3, 1, 33, tzinfo=UTC)
    assert len(session.calls) == 6
    assert clock == [3, 6, 12, 24, 48]


@pytest.mark.parametrize("kind", ["csv", "xbrl", "list"])
@pytest.mark.parametrize("body", [False, True])
def test_retry_after_seconds_and_date_use_the_current_response_time(kind, body, clock):
    deadline = format_datetime(datetime(2026, 10, 12, 3, 0, 20, tzinfo=UTC), usegmt=True)
    session = FakeSession(rate_limit(body, "7"), rate_limit(body, deadline), success(kind))
    request(kind, session)
    assert len(session.calls) == 3
    assert clock == [7, 13]


@pytest.mark.parametrize(
    ("header", "wait"), [("invalid", 3), ("-1", 3), ("0", 0), ("Mon, 12 Oct 2026 02:59:59 GMT", 0)]
)
def test_missing_or_invalid_retry_after_falls_back_and_past_deadline_does_not_wait(
    header, wait, clock
):
    session = FakeSession(rate_limit(retry_after=header), success("csv"))
    request("csv", session)
    assert clock == [wait]


@pytest.mark.parametrize("kind", ["csv", "xbrl", "list"])
@pytest.mark.parametrize("status", [400, 401, 403])
def test_auth_or_refusal_is_not_reclassified_by_a_body_429(kind, status, clock):
    session = FakeSession(response(b'{"StatusCode":429}', status_code=status))
    with pytest.raises(EdinetAcquisitionError) as caught:
        request(kind, session)
    assert not isinstance(caught.value, EdinetRateLimitError)
    assert len(session.calls) == 1
    assert clock == []


@pytest.mark.parametrize("kind", ["csv", "xbrl"])
@pytest.mark.parametrize("content", [b"invalid zip", b'{"StatusCode":401}', b'{"StatusCode":403}'])
def test_non_zip_success_body_is_returned_without_validation_or_retry(kind, content, clock):
    session = FakeSession(response(content))
    assert request(kind, session).body == content
    assert len(session.calls) == 1
    assert clock == []


@pytest.mark.parametrize("payload", [[], {"results": []}])
def test_listing_schema_is_not_interpreted_by_transport(payload, clock):
    session = FakeSession(json_response(payload))
    assert request("list", session).body == json_response(payload).content
    assert len(session.calls) == 1
    assert clock == []


def test_invalid_listing_json_is_returned_without_retry(clock):
    session = FakeSession(response(b"invalid JSON"))
    assert request("list", session).body == b"invalid JSON"
    assert len(session.calls) == 1
    assert clock == []


@pytest.mark.parametrize("kind", ["csv", "xbrl", "list"])
def test_each_retry_response_is_closed_before_wait_and_success_is_closed(kind, clock, monkeypatch):
    limited = rate_limit()
    unavailable = response(status_code=503)
    accepted = success(kind)
    session = FakeSession(limited, unavailable, accepted)
    sleep = http.time.sleep

    def checked_sleep(seconds):
        assert limited.close_count == 1
        if len(session.calls) == 2:
            assert unavailable.close_count == 1
        sleep(seconds)

    monkeypatch.setattr(http.time, "sleep", checked_sleep)
    request(kind, session)
    assert [item.close_count for item in (limited, unavailable, accepted)] == [1, 1, 1]


@pytest.mark.parametrize("status", [403, 429, 503])
def test_terminal_http_failures_close_every_response(status, clock):
    replies = [response(status_code=status) for _ in range(1 if status == 403 else 6)]
    with pytest.raises(EdinetAcquisitionError):
        request("list", FakeSession(*replies))
    assert all(item.close_count == 1 for item in replies)


def test_unparsed_success_body_still_closes_http_response(clock):
    invalid = response(b"invalid JSON")
    assert request("list", FakeSession(invalid)).body == invalid.content
    assert invalid.close_count == 1
