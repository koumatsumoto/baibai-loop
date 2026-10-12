"""Exercise the actual GET boundary: HTTP and body 429 share six attempts."""

from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime
from io import BytesIO
from zipfile import ZipFile

import pytest
import requests
from tests.helpers.http_doubles import FakeSession, json_response, response

from baibai_engine.market.providers import edinet
from baibai_engine.market.providers.edinet import EDINETProvider, EDINETProviderError
from baibai_engine.market.providers.edinet_facts import EDINETFactsProvider


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

    monkeypatch.setattr(edinet, "datetime", Clock)
    monkeypatch.setattr(edinet.time, "sleep", sleep)
    return waits


def success(kind):
    if kind == "list":
        return json_response({"metadata": {"resultset": {"count": 0}}, "results": []})
    data = BytesIO()
    with ZipFile(data, "w") as archive:
        archive.writestr("document.csv", "unchanged source bytes")
    return response(data.getvalue())


def request(kind, path, session):
    provider = (EDINETFactsProvider if kind == "xbrl" else EDINETProvider)(
        "test-key", path, session=session
    )
    if kind == "list":
        return provider.list_documents(date(2026, 10, 9))
    if kind == "xbrl":
        return provider.download_xbrl_zip("S100TEST")
    return provider.download_csv_zip("S100TEST")


def rate_limit(body=False, retry_after=None):
    result = response(b'{"StatusCode":429}' if body else b"", status_code=200 if body else 429)
    if retry_after is not None:
        result.headers["Retry-After"] = retry_after
    return result


@pytest.mark.parametrize("kind", ["csv", "xbrl", "list"])
def test_mixed_http_and_body_rate_limits_share_one_budget(kind, tmp_path, clock):
    session = FakeSession(*(rate_limit(body=bool(i % 2)) for i in range(6)))
    with pytest.raises(edinet.EDINETRateLimitError):
        request(kind, tmp_path, session)
    assert len(session.calls) == 6
    assert clock == [3, 6, 12, 24, 48]
    assert not list(tmp_path.rglob("*.zip"))


@pytest.mark.parametrize("kind", ["csv", "xbrl", "list"])
def test_transient_failures_do_not_reset_the_rate_limit_budget(kind, tmp_path, clock):
    session = FakeSession(
        requests.ConnectionError("temporary connection failure"),
        response(status_code=503),
        rate_limit(body=True),
        rate_limit(),
        rate_limit(body=True),
        rate_limit(),
    )
    with pytest.raises(edinet.EDINETRateLimitError):
        request(kind, tmp_path, session)
    assert len(session.calls) == 6
    assert clock == [3, 6, 12, 24, 48]


@pytest.mark.parametrize("kind", ["csv", "xbrl", "list"])
def test_sixth_attempt_can_succeed_and_zip_cache_avoids_new_requests(kind, tmp_path, clock):
    expected = success(kind)
    session = FakeSession(*(rate_limit(body=bool(i % 2)) for i in range(5)), expected)
    result = request(kind, tmp_path, session)
    assert result == ([] if kind == "list" else expected.content)
    assert len(session.calls) == 6
    assert clock == [3, 6, 12, 24, 48]
    if kind != "list":
        assert request(kind, tmp_path, session) == expected.content
        assert len(session.calls) == 6
        assert [p.read_bytes() for p in tmp_path.rglob("*.zip")] == [expected.content]


@pytest.mark.parametrize("kind", ["csv", "xbrl", "list"])
@pytest.mark.parametrize("body", [False, True])
def test_retry_after_seconds_and_date_use_the_current_response_time(kind, body, tmp_path, clock):
    deadline = format_datetime(datetime(2026, 10, 12, 3, 0, 20, tzinfo=UTC), usegmt=True)
    session = FakeSession(rate_limit(body, "7"), rate_limit(body, deadline), success(kind))
    request(kind, tmp_path, session)
    assert len(session.calls) == 3
    assert clock == [7, 13]


@pytest.mark.parametrize(
    ("header", "wait"), [("invalid", 3), ("-1", 3), ("0", 0), ("Mon, 12 Oct 2026 02:59:59 GMT", 0)]
)
def test_missing_or_invalid_retry_after_falls_back_and_past_deadline_does_not_wait(
    header, wait, tmp_path, clock
):
    session = FakeSession(rate_limit(retry_after=header), success("csv"))
    request("csv", tmp_path, session)
    assert clock == [wait]


@pytest.mark.parametrize("kind", ["csv", "xbrl", "list"])
@pytest.mark.parametrize("status", [400, 401, 403])
def test_auth_or_refusal_is_not_reclassified_by_a_body_429(kind, status, tmp_path, clock):
    session = FakeSession(response(b'{"StatusCode":429}', status_code=status))
    with pytest.raises(EDINETProviderError) as caught:
        request(kind, tmp_path, session)
    assert not isinstance(caught.value, edinet.EDINETRateLimitError)
    assert len(session.calls) == 1
    assert clock == []


@pytest.mark.parametrize("kind", ["csv", "xbrl"])
@pytest.mark.parametrize("content", [b"invalid zip", b'{"StatusCode":401}', b'{"StatusCode":403}'])
def test_invalid_zip_or_body_auth_error_is_not_retried(kind, content, tmp_path, clock):
    session = FakeSession(response(content))
    with pytest.raises(EDINETProviderError):
        request(kind, tmp_path, session)
    assert len(session.calls) == 1
    assert clock == []
    assert not list(tmp_path.rglob("*.zip"))


@pytest.mark.parametrize("payload", [[], {"results": []}])
def test_listing_schema_error_is_not_retried(payload, tmp_path, clock):
    session = FakeSession(json_response(payload))
    with pytest.raises(EDINETProviderError):
        request("list", tmp_path, session)
    assert len(session.calls) == 1
    assert clock == []


def test_listing_json_parse_error_is_not_retried(tmp_path, clock):
    session = FakeSession(response(b"invalid JSON"))
    with pytest.raises(ValueError, match="no JSON body"):
        request("list", tmp_path, session)
    assert len(session.calls) == 1
    assert clock == []
