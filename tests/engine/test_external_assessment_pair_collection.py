from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
import requests
from tests.engine.test_external_assessment_sources import (
    monex_html,
    official_html,
    rakuten_html,
    rakuten_no_estimates_html,
)

from baibai_engine import job_api
from baibai_engine.data_collection.equity_web import http as source_http
from baibai_engine.data_collection.external_assessment import collection
from baibai_engine.machine_analysis.external_assessment.normalization import monex, rakuten
from baibai_engine.machine_analysis.external_assessment.normalization.decode import decode_attempt


class Clock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(collection.time, "monotonic", value.monotonic)
    monkeypatch.setattr(collection.time, "sleep", value.sleep)
    return value


class Session(requests.Session):
    def __init__(self, replies, *, clock=None, events=None):
        super().__init__()
        self.replies = iter(replies)
        self.calls = []
        self.clock = clock
        self.events = events
        self.finished = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs, self.clock.now if self.clock is not None else None))
        if self.events is not None:
            assert self.events[-1] == "before"
            self.events.append("request")
        reply = next(self.replies)
        if isinstance(reply, BaseException):
            raise reply
        status, html = reply
        if self.clock is not None:
            self.clock.now += 0.25
            self.finished.append(self.clock.now)
        response = requests.Response()
        response.status_code = status
        response._content = html.encode()
        response._content_consumed = True
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        return response


def pair(client, *, source="monex", ticker="7203", response=None, **kwargs):
    return collection.collect_pair(
        ticker,
        source=source,
        on_response=response,
        session=client,
        **kwargs,
    )


def test_fresh_pair_and_resumed_collector_wait_after_previous_response(clock):
    first = Session([(200, monex_html())], clock=clock)
    pair(first)
    second = Session([(200, monex_html("9432"))], clock=clock)
    pair(second, ticker="9432")
    assert second.calls[0][2] - first.finished[-1] >= 2


def test_response_callback_delay_preserves_receipt_and_jst_date(clock, monkeypatch):
    original_datetime = datetime
    baseline = original_datetime(2026, 10, 10, 14, 59, 56, 750000, tzinfo=UTC)

    class DateTime(original_datetime):
        @classmethod
        def now(cls, tz=None):
            return baseline + timedelta(seconds=clock.now - 100)

    monkeypatch.setattr(source_http, "datetime", DateTime)
    client = Session([(200, monex_html())], clock=clock)
    finishes = []

    def response(received, count, size):
        finishes.append(received)
        clock.now += 2

    result = pair(client, response=response)
    assert result.observations[0].fetched_at_utc == finishes[-1]
    assert result.observations[0].snapshot_date.isoformat() == "2026-10-10"
    assert result.last_response_finished_at_utc == finishes[-1]


def test_pair_keeps_the_observation_already_validated_by_its_source(clock, monkeypatch):
    parsed = []
    parse = monex.parse_html

    def capture(*args, **kwargs):
        observation = parse(*args, **kwargs)
        parsed.append(observation)
        return observation

    monkeypatch.setattr(monex, "parse_html", capture)
    result = pair(Session([(200, monex_html())], clock=clock))
    assert result.observations[0] is parsed[0]
    assert result.observations[0].fetched_at_utc == result.last_response_finished_at_utc


def test_initial_pacing_deadline_is_zero_requests(clock):
    client = Session([], clock=clock)
    result = pair(client, time_limit_seconds=1)
    assert result.requests == 0
    assert not client.calls
    assert result.outcomes[0].status == "not_attempted"
    assert result.stop_reason == "wall_clock_limit"
    assert result.response_content_bytes == 0


@pytest.mark.parametrize(("status", "reason"), [(403, "access_denied"), (429, "rate_limited")])
def test_access_limits_request_whole_run_halt_with_confirmed_status(clock, status, reason):
    client = Session([(status, "synthetic denied page")], clock=clock)
    result = pair(client)
    assert result.stop_reason == reason
    assert result.outcomes[0].http_status == status
    assert result.requests == 1
    assert not result.observations


def test_iframe_access_limit_confirms_two_requests_and_stops(clock):
    client = Session([(200, official_html()), (403, "synthetic denial")], clock=clock)
    result = pair(client, source="rakuten")
    assert result.requests == 2
    assert result.stop_reason == "access_denied"


@pytest.mark.parametrize("value", ["--", "1,234円"])
def test_verified_null_and_numeric_results_remain_typed(clock, value):
    client = Session([(200, monex_html(value=value))], clock=clock)
    result = pair(client)
    assert result.outcomes[0].status == "observed"
    assert len(result.observations) == 1
    assert (result.observations[0].consensus_target is None) is (value == "--")


@pytest.mark.parametrize("ticker", ["1301", "130A", "7203", "6091"])
def test_identity_unknown_is_fixed_failure_without_null_observation(clock, ticker):
    client = Session(
        [(200, official_html(ticker)), (200, rakuten_no_estimates_html())],
        clock=clock,
    )
    result = pair(client, source="rakuten", ticker=ticker)
    assert result.outcomes[0].status == "parse_failure"
    assert result.outcomes[0].reason == "no_estimates_identity_unconfirmed"
    assert result.outcomes[0].http_status == 200
    assert result.outcomes[0].attempt_id is not None
    assert not result.observations
    assert result.stop_reason is None
    assert result.requests == 2
    assert [call[0] for call in client.calls] == [
        rakuten.public_url(ticker),
        rakuten.IFRAME_BASE + rakuten.IFRAME_PARAMETER + ticker + ".T",
    ]


def test_wall_clock_interruption_after_transport_reports_timeout(clock):
    client = Session([source_http.TimeLimit()], clock=clock)
    result = pair(client)
    assert result.requests == 1
    assert result.stop_reason == "wall_clock_limit"
    assert result.response_content_bytes == 0


def test_interruption_after_403_body_before_classification_reports_timeout(clock):
    client = Session([(403, "synthetic denial")], clock=clock)

    def response(*args):
        raise source_http.TimeLimit

    result = pair(client, response=response)
    assert result.requests == 1
    assert result.stop_reason == "wall_clock_limit"
    assert result.last_response_finished_at_utc is not None


def test_normal_request_timeout_is_confirmed_without_retry(clock):
    client = Session([requests.Timeout("private-secret")], clock=clock)
    result = pair(client)
    assert result.requests == 1
    assert result.outcomes[0].status == "request_timeout"
    assert result.stop_reason is None
    assert "private-secret" not in str(result.summary())


@pytest.mark.parametrize("setting", ["trust_env", "verify", "retry"])
def test_pair_refuses_proxy_tls_bypass_and_retry_configuration(clock, setting):
    client = Session([])
    if setting == "retry":
        client.mount("https://", requests.adapters.HTTPAdapter(max_retries=1))
    else:
        setattr(client, setting, False)
    with pytest.raises(ValueError, match="ordinary proxy/TLS and no retries"):
        pair(client)
    assert not client.calls


@pytest.mark.parametrize(
    "kwargs",
    [
        {"time_limit_seconds": 121},
        {"time_limit_seconds": float("nan")},
        {"request_timeout_seconds": 31},
        {"request_timeout_seconds": 0},
        {"source": "other"},
        {"ticker": "invalid"},
    ],
)
def test_pair_invalid_limits_or_identity_never_request(clock, kwargs):
    client = Session([], clock=clock)
    with pytest.raises(ValueError, match=r"ticker|source|limit|timeout"):
        pair(client, **kwargs)
    assert not client.calls


@pytest.mark.parametrize("parser", [monex, rakuten])
def test_facade_and_policy_binding_are_deterministic_and_parser_sensitive(monkeypatch, parser):
    assert job_api.external_assessment_collect_pair is collection.collect_pair
    assert job_api.ExternalAssessmentCollectionResult is collection.CollectionResult
    assert job_api.external_assessment_parse_failure_reasons is collection.PARSE_FAILURE_REASONS
    binding = job_api.external_assessment_collector_binding()
    assert len(binding) == 64
    assert binding == job_api.external_assessment_collector_binding()
    monkeypatch.setattr(parser, "PARSER_VERSION", "synthetic-next-parser")
    assert binding != job_api.external_assessment_collector_binding()


def test_two_http_receipt_times_and_content_accounting_without_durable_callbacks(clock):
    pages = [official_html(), rakuten_html()]
    client = Session([(200, html) for html in pages], clock=clock)
    received = []
    result = pair(client, source="rakuten", on_received_html=received.append)
    assert result.requests == 2
    assert result.response_content_bytes == sum(len(html.encode()) for html in pages)
    assert result.observations[0].fetched_at_utc == received[-1].received_at_utc
    assert result.last_response_finished_at_utc == received[-1].received_at_utc
    assert [item.body for item in received] == [html.encode() for html in pages]
    assert received[0].role == "page"
    assert received[0].parent_receipt_id is None
    assert received[1].role == "iframe"
    assert received[1].parent_receipt_id == received[0].receipt_id
    assert received[0].attempt_id == received[1].attempt_id == result.outcomes[0].attempt_id
    assert decode_attempt(received) == result.observations[0]
    assert all(
        call[1]["allow_redirects"] is False and call[1]["timeout"] <= 30 for call in client.calls
    )


def test_real_timer_interrupts_hanging_body_and_returns_controlled_timeout(monkeypatch):
    monkeypatch.setattr(source_http, "MIN_REQUEST_INTERVAL_SECONDS", 0)

    class Hanging(Session):
        def get(self, url, **kwargs):
            time.sleep(5)
            raise AssertionError("finite timer should interrupt")

    start = time.monotonic()
    result = pair(Hanging([]), time_limit_seconds=0.05)
    assert time.monotonic() - start < 1
    assert result.requests == 1
    assert result.outcomes[0].status == "request_timeout"
    assert result.stop_reason == "wall_clock_limit"
