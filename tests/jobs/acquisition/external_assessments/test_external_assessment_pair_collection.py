from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
import requests
from tests.helpers.external_assessment_html import (
    monex_html,
)
from tests.helpers.external_assessment_http import PairSession as Session

import baibai_engine.data_collection.transport.public_pages as source_http
import baibai_jobs.acquisition.external_assessments.collection as collection
from baibai_engine.machine_analysis.observations.external_assessments.normalization import (
    kabumap,
    monex,
)


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


@pytest.mark.parametrize(
    ("status", "reason"),
    [(401, "access_denied"), (403, "access_denied"), (429, "rate_limited")],
)
def test_access_limits_request_whole_run_halt_with_confirmed_status(clock, status, reason):
    client = Session([(status, "synthetic denied page")], clock=clock)
    result = pair(client)
    assert result.stop_reason == reason
    assert result.outcomes[0].http_status == status
    assert result.requests == 1
    assert not result.observations


@pytest.mark.parametrize("value", ["--", "1,234円"])
def test_verified_null_and_numeric_results_remain_typed(clock, value):
    client = Session([(200, monex_html(value=value))], clock=clock)
    result = pair(client)
    assert result.outcomes[0].status == "observed"
    assert len(result.observations) == 1
    assert (result.observations[0].consensus_target is None) is (value == "--")


def test_wall_clock_interruption_after_transport_reports_timeout(clock):
    client = Session([source_http.TimeLimit()], clock=clock)
    result = pair(client)
    assert result.requests == 1
    assert result.stop_reason == "wall_clock_limit"
    assert result.response_content_bytes == 0


@pytest.mark.parametrize("error_type", [source_http.TimeLimit, KeyboardInterrupt, SystemExit])
def test_projection_interruption_is_not_parse_failure(clock, monkeypatch, error_type):
    client = Session([(200, monex_html())], clock=clock)
    received = []

    def interrupt(*args, **kwargs):
        raise error_type

    monkeypatch.setattr(collection, "parse_source_html", interrupt)
    if error_type is source_http.TimeLimit:
        result = pair(client, on_received_html=received.append)
        assert result.outcomes[0].status == "request_timeout"
        assert result.stop_reason == "wall_clock_limit"
        assert not result.observations
    else:
        with pytest.raises(error_type):
            pair(client, on_received_html=received.append)
    assert len(received) == len(client.calls) == 1


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
@pytest.mark.parametrize("origin", ["transport", "receipt_callback"])
def test_nonprojection_errors_are_not_converted_to_parse_failure(
    clock, monkeypatch, error_type, origin
):
    client = Session([(200, monex_html())], clock=clock)

    def fail(*args, **kwargs):
        raise error_type("private nonprojection failure")

    if origin == "transport":
        monkeypatch.setattr(client, "get", fail)
    with pytest.raises(error_type, match="private nonprojection failure"):
        pair(client, on_received_html=fail if origin == "receipt_callback" else None)


def test_unreceived_transport_failure_has_no_original_and_no_projection(clock, monkeypatch):
    client = Session([requests.ConnectionError("private transport detail")], clock=clock)
    received = []

    def unexpected(*args, **kwargs):
        raise AssertionError("an unreceived response must not be projected")

    monkeypatch.setattr(collection, "parse_source_html", unexpected)
    result = pair(client, on_received_html=received.append)
    assert result.outcomes[0].status == "transport_failure"
    assert result.response_content_bytes == 0
    assert result.requests == 1
    assert received == []
    assert "private transport detail" not in str(result.summary())


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


@pytest.mark.parametrize("parser", [monex, kabumap])
def test_policy_binding_is_deterministic_and_parser_sensitive(monkeypatch, parser):
    binding = collection.collector_binding()
    assert len(binding) == 64
    assert binding == collection.collector_binding()
    monkeypatch.setattr(parser, "PARSER_VERSION", "synthetic-next-parser")
    assert binding != collection.collector_binding()


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
