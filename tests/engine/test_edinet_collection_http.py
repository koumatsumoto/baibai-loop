"""Synthetic HTTP checks for Collection's EDINET byte receiver."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime

import pytest
import requests
from tests.helpers.http_doubles import FakeResponse, response

from baibai_engine.collection.edinet import http
from baibai_engine.collection.edinet.models import EdinetAcquisitionError, EdinetRateLimitError
from baibai_engine.collection.read_http import AcquisitionDeadlineExceeded
from baibai_engine.market.edinet_refresh import EDINETRefresh

KEY = "secret+/key"
DAY = date(2026, 10, 14)
NOW = datetime(2026, 10, 14, 3, 0, tzinfo=UTC)


class Session:
    def __init__(self, *answers: FakeResponse | Exception) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, int, bool]] = []
        self.close_count = 0

    def get(self, url: str, *, timeout: int, allow_redirects: bool) -> FakeResponse:
        self.calls.append((url, timeout, allow_redirects))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def close(self) -> None:
        self.close_count += 1


@pytest.fixture
def clock(monkeypatch):
    current = NOW
    waits: list[float] = []

    class Clock:
        @staticmethod
        def now(_tz):
            return current

    def sleep(seconds: float) -> None:
        nonlocal current
        waits.append(seconds)
        current += timedelta(seconds=seconds)

    monkeypatch.setattr(http, "datetime", Clock)
    monkeypatch.setattr(http.time, "sleep", sleep)
    return waits


def limited(*, body: bool = False, retry_after: str | None = None) -> FakeResponse:
    reply = response(b'{"StatusCode":429}' if body else b"", status_code=200 if body else 429)
    if retry_after is not None:
        reply.headers["Retry-After"] = retry_after
    return reply


def test_job_cutoff_stops_body_429_before_waiting_or_retrying(clock, monkeypatch):
    monkeypatch.setattr("time.time", lambda: NOW.timestamp())
    monkeypatch.setenv("BAIBAI_ACQUISITION_DEADLINE", str(NOW.timestamp() + 10))
    session = Session(limited(body=True, retry_after="20"))
    with pytest.raises(AcquisitionDeadlineExceeded):
        http.EDINETHTTP(KEY, session=session).fetch_list(DAY)
    assert len(session.calls) == 1
    assert clock == []


def test_list_and_archive_requests_keep_exact_query_and_raw_body(clock):
    replies = [response(b"not JSON"), response(b"XBRL bytes"), response(b"CSV bytes")]
    session = Session(*replies)
    receiver = http.EDINETHTTP(KEY, session=session)

    listing = receiver.fetch_list(DAY)
    xbrl = receiver.fetch_archive("S100TEST", 1)
    csv = receiver.fetch_archive("S100TEST", 5)

    assert [(item.body, item.received_at) for item in (listing, xbrl, csv)] == [
        (b"not JSON", NOW),
        (b"XBRL bytes", NOW),
        (b"CSV bytes", NOW),
    ]
    list_url = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
    archive_url = "https://api.edinet-fsa.go.jp/api/v2/documents/S100TEST"
    assert session.calls == [
        (
            f"{list_url}?date=2026-10-14&type=2&Subscription-Key=secret%2B%2Fkey",
            30,
            False,
        ),
        (
            f"{archive_url}?type=1&Subscription-Key=secret%2B%2Fkey",
            60,
            False,
        ),
        (
            f"{archive_url}?type=5&Subscription-Key=secret%2B%2Fkey",
            60,
            False,
        ),
    ]
    assert [item.close_count for item in replies] == [1, 1, 1]
    assert session.close_count == 0
    assert clock == []


@pytest.mark.parametrize("doc_id", ["../../secret", "s100test", "S12", "S100TEST?"])
def test_archive_rejects_non_normalized_document_id_before_request(doc_id):
    session = Session()
    with pytest.raises(ValueError, match="normalized document id"):
        http.EDINETHTTP(KEY, session=session).fetch_archive(doc_id, 5)
    assert session.calls == []


@pytest.mark.parametrize("source_type", [0, 2, 6, True, False])
def test_archive_rejects_non_archive_type_before_request(source_type):
    session = Session()
    with pytest.raises(ValueError, match=r"XBRL.*CSV"):
        http.EDINETHTTP(KEY, session=session).fetch_archive("S100TEST", source_type)
    assert session.calls == []


def test_api_key_is_required():
    with pytest.raises(ValueError, match="API key is required"):
        http.EDINETHTTP("")


def test_http_and_body_429_share_six_attempts_and_close_before_wait(clock, monkeypatch):
    replies = [limited(body=bool(index % 2)) for index in range(6)]
    session = Session(*replies)
    sleep = http.time.sleep

    def checked_sleep(seconds: float) -> None:
        assert replies[len(session.calls) - 1].close_count == 1
        sleep(seconds)

    monkeypatch.setattr(http.time, "sleep", checked_sleep)
    with pytest.raises(EdinetRateLimitError, match="after retries") as caught:
        http.EDINETHTTP(KEY, session=session).fetch_list(DAY)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert len(session.calls) == 6
    assert [item.close_count for item in replies] == [1] * 6
    assert clock == [3.0, 6.0, 12.0, 24.0, 48.0]


@pytest.mark.parametrize("failure", [503, "timeout"])
def test_server_or_transport_failure_has_finite_six_attempt_budget(failure, clock):
    answers = [
        response(status_code=503)
        if failure == 503
        else requests.Timeout("https://api.edinet-fsa.go.jp/?Subscription-Key=" + KEY)
        for _ in range(6)
    ]
    session = Session(*answers)
    with pytest.raises(
        EdinetAcquisitionError, match=r"EDINET.*after retries|transport failed"
    ) as caught:
        http.EDINETHTTP(KEY, session=session).fetch_list(DAY)
    assert not isinstance(caught.value, EdinetRateLimitError)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert KEY not in str(caught.value)
    assert len(session.calls) == 6
    assert clock == [3.0, 6.0, 12.0, 24.0, 48.0]
    if failure == 503:
        assert all(item.close_count == 1 for item in answers)


def test_sixth_attempt_can_succeed_after_mixed_failures(clock):
    replies = [response(status_code=503), limited(body=True), limited(), response(status_code=503)]
    accepted = response(b"final")
    session = Session(requests.Timeout("temporary"), *replies, accepted)
    result = http.EDINETHTTP(KEY, session=session).fetch_archive("S100TEST", 1)
    assert result.body == b"final"
    assert result.received_at == NOW + timedelta(seconds=93)
    assert len(session.calls) == 6
    assert [item.close_count for item in replies] == [1] * 4
    assert accepted.close_count == 1
    assert clock == [3.0, 6.0, 12.0, 24.0, 48.0]


@pytest.mark.parametrize("body", [False, True])
def test_retry_after_seconds_and_http_date_use_response_time(body, clock):
    deadline = format_datetime(NOW + timedelta(seconds=20), usegmt=True)
    first = limited(body=body, retry_after="7")
    second = limited(body=body, retry_after=deadline)
    accepted = response(b"ok")
    session = Session(first, second, accepted)
    result = http.EDINETHTTP(KEY, session=session).fetch_list(DAY)
    assert result.body == b"ok"
    assert result.received_at == NOW + timedelta(seconds=20)
    assert clock == [7.0, 13.0]
    assert [item.close_count for item in (first, second, accepted)] == [1, 1, 1]


@pytest.mark.parametrize(
    ("header", "expected_wait"),
    [
        ("invalid", 3.0),
        ("-1", 3.0),
        ("Wed, 14 Oct 2026 02:59:59 GMT", 0.0),
        ("0", 0.0),
        ("Wed, 14 Oct 2026 03:00:00", 3.0),
        ("²", 3.0),
        ("9" * 400, 3.0),
    ],
)
def test_invalid_or_past_retry_after_header(header, expected_wait, clock):
    session = Session(limited(retry_after=header), response(b"ok"))
    http.EDINETHTTP(KEY, session=session).fetch_list(DAY)
    assert clock == [expected_wait]


def test_server_error_uses_backoff_even_with_retry_after(clock):
    unavailable = response(status_code=503)
    unavailable.headers["Retry-After"] = "99"
    session = Session(unavailable, response(b"ok"))
    http.EDINETHTTP(KEY, session=session).fetch_list(DAY)
    assert clock == [3.0]
    assert unavailable.close_count == 1


@pytest.mark.parametrize("status", [401, 403, 404, 302, 307])
def test_auth_missing_and_redirect_fail_immediately_without_secret(status, clock):
    reply = response(b'{"StatusCode":429}', status_code=status)
    session = Session(reply)
    with pytest.raises(EdinetAcquisitionError, match=f"status {status}") as caught:
        http.EDINETHTTP(KEY, session=session).fetch_list(DAY)
    assert len(session.calls) == 1
    assert session.calls[0][2] is False
    assert reply.close_count == 1
    assert clock == []
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert KEY not in str(caught.value)


def test_body_read_transport_error_is_closed_and_does_not_leak_secret(clock):
    class BrokenResponse:
        status_code = 200
        headers: dict[str, str] = {}

        def __init__(self) -> None:
            self.close_count = 0

        @property
        def content(self) -> bytes:
            raise requests.ConnectionError("https://api.edinet-fsa.go.jp/?Subscription-Key=" + KEY)

        def close(self) -> None:
            self.close_count += 1

    replies = [BrokenResponse() for _ in range(6)]
    session = Session(*replies)
    with pytest.raises(EdinetAcquisitionError, match="transport failed") as caught:
        http.EDINETHTTP(KEY, session=session).fetch_list(DAY)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert KEY not in str(caught.value)
    assert [item.close_count for item in replies] == [1] * 6
    assert clock == [3.0, 6.0, 12.0, 24.0, 48.0]


def test_close_only_owned_session(monkeypatch):
    owned = Session()
    injected = Session()
    monkeypatch.setattr(http.requests, "Session", lambda: owned)
    http.EDINETHTTP(KEY).close()
    http.EDINETHTTP(KEY, session=injected).close()
    assert owned.close_count == 1
    assert injected.close_count == 0


def test_provider_creates_http_only_when_acquiring_and_closes_owned_session(tmp_path, monkeypatch):
    created: list[Session] = []

    def new_session() -> Session:
        session = Session(response(b'{"metadata":{"resultset":{"count":0}},"results":[]}'))
        created.append(session)
        return session

    monkeypatch.setattr(http.requests, "Session", new_session)
    provider = EDINETRefresh(KEY, sqlite_path=tmp_path / "market.sqlite")
    assert created == []
    assert provider.list_documents(DAY) == []
    assert len(created) == 1
    provider.close()
    provider.close()
    assert created[0].close_count == 1


def test_refresh_rebuilds_saved_list_without_creating_http_session(tmp_path, monkeypatch):
    def unexpected_session() -> None:
        pytest.fail("read-only EDINET path constructed an HTTP session")

    monkeypatch.setattr(http.requests, "Session", unexpected_session)
    sqlite_path = tmp_path / "market.sqlite"
    from baibai_engine.collection.edinet.receipts import retain_list_response
    from baibai_engine.storage.observations.schema import open_connection

    connection = open_connection(sqlite_path)
    try:
        retain_list_response(
            connection,
            doc_date=DAY,
            received_at=datetime(2026, 10, 14, 12, tzinfo=UTC),
            body=b'{"metadata":{"resultset":{"count":1}},"results":[{"seqNumber":1,"docID":"S100TEST"}]}',
        )
    finally:
        connection.close()
    provider = EDINETRefresh(KEY, sqlite_path=sqlite_path)
    documents = provider.list_documents(DAY)
    assert len(documents) == 1
    assert {key: documents[0][key] for key in ("seqNumber", "docID", "doc_date")} == {
        "seqNumber": 1,
        "docID": "S100TEST",
        "doc_date": DAY.isoformat(),
    }
    provider.close()


def test_provider_does_not_close_injected_session_after_acquisition(tmp_path):
    session = Session(response(b'{"metadata":{"resultset":{"count":0}},"results":[]}'))
    provider = EDINETRefresh(KEY, session=session, sqlite_path=tmp_path / "market.sqlite")
    assert provider.list_documents(DAY) == []
    provider.close()
    assert session.close_count == 0
