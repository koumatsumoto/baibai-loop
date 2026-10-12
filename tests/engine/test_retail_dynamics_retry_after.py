from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import requests

from baibai_engine.market.retail_dynamics import collection


class Clock:
    def __init__(self):
        self.seconds = 100.0
        self.baseline = datetime(2026, 10, 11, 12, tzinfo=UTC)

    def monotonic(self):
        return self.seconds

    def sleep(self, seconds):
        self.seconds += seconds

    def utc(self):
        return self.baseline + timedelta(seconds=self.seconds - 100)


@pytest.fixture
def clock(monkeypatch):
    value = Clock()

    class DateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return value.utc()

    monkeypatch.setattr(collection.time, "monotonic", value.monotonic)
    monkeypatch.setattr(collection.time, "sleep", value.sleep)
    monkeypatch.setattr(collection, "datetime", DateTime)
    return value


class IncompleteResponse(requests.Response):
    @property
    def content(self):
        raise collection._TimeLimit


class Session(requests.Session):
    def __init__(self, clock, *, status=429, header=None, failure=None, incomplete_body=False):
        super().__init__()
        self.clock = clock
        self.status = status
        self.header = header
        self.failure = failure
        self.incomplete_body = incomplete_body
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(kwargs)
        self.clock.seconds += 0.25
        if self.failure is not None:
            raise self.failure
        response = IncompleteResponse() if self.incomplete_body else requests.Response()
        response.status_code = self.status
        response._content = b"private-secret-body"
        response._content_consumed = True
        if self.header is not None:
            response.headers["Retry-After"] = self.header
        return response


def collect(client, *, on_response=None, on_outcome=None):
    return collection.collect_pair(
        "7203",
        source="monex",
        before_request=lambda: None,
        on_outcome=on_outcome if on_outcome is not None else lambda result: None,
        on_response=on_response,
        session=client,
    )


@pytest.mark.parametrize(("header", "seconds"), [("30", 30), (" 00030 ", 30), ("0", 0)])
def test_confirmed_delta_seconds_uses_fully_received_time_without_retry(clock, header, seconds):
    client = Session(clock, header=header)
    completed = []
    result = collect(client, on_outcome=completed.append)
    assert result.retry_after_not_before_utc == result.last_response_finished_at_utc + timedelta(
        seconds=seconds
    )
    assert result.retry_after_unrepresentable is False
    assert result.outcomes[0].http_status == 429
    assert result.outcomes[0].status == result.stop_reason == "rate_limited"
    assert result.requests == len(client.calls) == 1
    assert completed == [result]
    assert client.calls[0]["allow_redirects"] is False
    assert client.calls[0]["timeout"] <= 30
    assert client.trust_env
    assert client.verify is True
    assert client.adapters["https://"].max_retries.total == 0
    assert "private-secret" not in json.dumps(result.summary())
    assert (
        result.summary()["retry_after_not_before_utc"]
        == result.retry_after_not_before_utc.isoformat()
    )


def test_checkpoint_latency_does_not_extend_retry_after(clock):
    client = Session(clock, header="30")
    received = []

    def response(finished, count, size):
        received.append(finished)
        clock.seconds += 10

    result = collect(client, on_response=response)
    assert result.retry_after_not_before_utc == received[0] + timedelta(seconds=30)
    assert result.last_response_finished_at_utc == received[0]
    assert result.response_content_bytes == len(b"private-secret-body")
    assert clock.utc() == received[0] + timedelta(seconds=10)


@pytest.mark.parametrize(
    ("header", "deadline"),
    [
        ("Sun, 11 Oct 2026 12:01:00 GMT", datetime(2026, 10, 11, 12, 1, tzinfo=UTC)),
        ("Sun Oct 18 12:00:00 2026", datetime(2026, 10, 18, 12, tzinfo=UTC)),
        ("Thu Nov  5 12:00:00 2026", datetime(2026, 11, 5, 12, tzinfo=UTC)),
        ("Sunday, 18-Oct-26 12:00:00 GMT", datetime(2026, 10, 18, 12, tzinfo=UTC)),
        ("Sunday, 18-Oct-69 12:00:00 GMT", datetime(2069, 10, 18, 12, tzinfo=UTC)),
        ("Sunday, 01-Oct-76 12:00:00 GMT", datetime(2076, 10, 1, 12, tzinfo=UTC)),
        ("Sunday, 18-Oct-76 12:00:00 GMT", None),
        ("Sun, 11 Oct 2026 11:59:00 GMT", None),
    ],
)
def test_http_date_normalizes_to_utc_and_past_clamps_to_receipt(clock, header, deadline):
    result = collect(Session(clock, header=header))
    assert result.retry_after_not_before_utc == (deadline or result.last_response_finished_at_utc)
    assert result.retry_after_not_before_utc.tzinfo is UTC
    assert result.retry_after_unrepresentable is False


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "private-secret-header",
        "-1",
        "+1",
        "1.5",
        "１",
        "NaN",
        "Sun, 11 Oct 2026 12:01:00",
        "Sun, 11 Oct 2026 13:01:00 +0100",
        "Fri, 31 Dec 9999 23:59:59 -0100",
        "Sun Oct 8 12:00:00 2026",
        "Sunday, 18-Oct-69 12:00:00",
    ],
)
def test_missing_or_malformed_retry_after_is_safe_unknown_for_caller_fallback(clock, header):
    result = collect(Session(clock, header=header))
    assert result.retry_after_not_before_utc is None
    assert result.retry_after_unrepresentable is False
    assert result.outcomes[0].http_status == 429
    assert result.stop_reason == "rate_limited"
    assert "private-secret" not in json.dumps(result.summary())


@pytest.mark.parametrize("header", ["999999999999999", "9" * 5000])
def test_valid_unrepresentable_delay_is_confirmed_failure_never_short_fallback(clock, header):
    completed = []
    result = collect(Session(clock, header=header), on_outcome=completed.append)
    assert result.retry_after_not_before_utc is None
    assert result.retry_after_unrepresentable is True
    assert result.outcomes[0].http_status == 429
    assert result.stop_reason == "rate_limited"
    assert completed == [result]
    assert result.summary()["retry_after_unrepresentable"] is True


def test_very_long_zero_delta_is_not_mistaken_for_overflow(clock):
    result = collect(Session(clock, header="0" * 5000))
    assert result.retry_after_not_before_utc == result.last_response_finished_at_utc
    assert result.retry_after_unrepresentable is False


def test_representable_long_delay_is_not_truncated_to_attempt_or_job_budget(clock):
    result = collect(Session(clock, header="86400"))
    assert result.retry_after_not_before_utc == result.last_response_finished_at_utc + timedelta(
        days=1
    )
    assert result.retry_after_unrepresentable is False
    assert result.requests == 1


@pytest.mark.parametrize("status", [403, 500])
def test_other_http_failures_never_adopt_retry_after(clock, status):
    result = collect(Session(clock, status=status, header="999999999999999"))
    assert result.outcomes[0].http_status == status
    assert result.retry_after_not_before_utc is None
    assert result.retry_after_unrepresentable is False
    assert (result.stop_reason == "access_denied") is (status == 403)


def test_transport_timeout_is_not_a_confirmed_429(clock):
    completed = []
    result = collect(Session(clock, failure=requests.Timeout()), on_outcome=completed.append)
    assert result.outcomes[0].status == "request_timeout"
    assert result.outcomes[0].http_status is None
    assert result.retry_after_not_before_utc is None
    assert result.retry_after_unrepresentable is False
    assert result.last_response_finished_at_utc is None
    assert completed == [result]


@pytest.mark.parametrize("phase", ["body", "response_checkpoint", "outcome_checkpoint", "deadline"])
@pytest.mark.parametrize("header", ["30", "999999999999999"])
def test_interrupted_429_attempt_keeps_marker_without_retry_deadline(clock, phase, header):
    completed = []
    client = Session(clock, header=header, incomplete_body=phase == "body")

    def response(*args):
        if phase == "response_checkpoint":
            raise collection._TimeLimit
        if phase == "deadline":
            clock.seconds += 120

    def outcome(result):
        if phase == "outcome_checkpoint":
            raise collection._TimeLimit
        completed.append(result)

    result = collect(client, on_response=response, on_outcome=outcome)
    assert result.stop_reason == "uncertain_inflight"
    assert result.retry_after_not_before_utc is None
    assert result.retry_after_unrepresentable is False
    assert result.requests == 1
    assert completed == []
    if phase == "body":
        assert result.last_response_finished_at_utc is None
        assert result.response_content_bytes == 0


def test_retry_policy_version_changes_old_plan_binding(monkeypatch):
    assert collection.PAIR_ACQUISITION_POLICY_VERSION == "retail-pair-acquisition-v4"
    new = collection.collector_binding()
    monkeypatch.setattr(collection, "PAIR_ACQUISITION_POLICY_VERSION", "retail-pair-acquisition-v1")
    assert collection.collector_binding() != new


def test_rfc850_century_window_moves_with_receipt_and_overflow_fails_closed():
    received = datetime(2090, 10, 11, 12, tzinfo=UTC)
    assert collection._retry_after("Sunday, 18-Oct-26 12:00:00 GMT", received) == (
        datetime(2126, 10, 18, 12, tzinfo=UTC),
        False,
    )
    received = datetime(9990, 10, 11, 12, tzinfo=UTC)
    assert collection._retry_after("Sunday, 18-Oct-26 12:00:00 GMT", received) == (None, True)


@pytest.mark.parametrize(
    "header",
    [
        "Mon, 11 Oct 2027 12:00:60 GMT",
        "Monday, 11-Oct-27 12:00:60 GMT",
        "Mon Oct 11 12:00:60 2027",
    ],
)
def test_calendar_valid_http_leap_second_fails_closed_without_short_fallback(clock, header):
    result = collect(Session(clock, header=header))
    assert result.outcomes[0].http_status == 429
    assert result.retry_after_not_before_utc is None
    assert result.retry_after_unrepresentable is True


@pytest.mark.parametrize(
    "header", ["Tue, 30 Feb 2027 12:00:60 GMT", "Mon, 11 Oct 2027 12:00:61 GMT"]
)
def test_invalid_calendar_or_second_is_malformed_not_leap_second(clock, header):
    result = collect(Session(clock, header=header))
    assert result.retry_after_not_before_utc is None
    assert result.retry_after_unrepresentable is False
