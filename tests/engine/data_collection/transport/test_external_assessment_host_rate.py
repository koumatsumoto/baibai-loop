from __future__ import annotations

from datetime import timedelta

import pytest
from tests.helpers.external_assessment_http import HostRateClock as Clock
from tests.helpers.external_assessment_http import HostRateSession as Session

import baibai_engine.data_collection.transport.public_pages as source_http
import baibai_jobs.acquisition.external_assessments.collection as collection

MONEX = "https://scouter.monex.co.jp/report/theoryDps/7203"
OFFICIAL = "https://www.rakuten-sec.co.jp/web/market/search/forecast.html?ric=7203.T"
IFRAME = "https://rakuten-sec.ifis.co.jp/index.php?action=tp4&sa=report_est&bcode=7203.T"


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(collection.time, "monotonic", value.monotonic)
    monkeypatch.setattr(collection.time, "sleep", value.sleep)
    return value


def pair(client, limiter, *, ticker="7203", source="monex", **kwargs):
    return collection.collect_pair(
        ticker,
        source=source,
        session=client,
        rate_limiter=limiter,
        **kwargs,
    )


def test_cold_start_and_each_request_are_spaced_half_a_second(clock):
    limiter = source_http.HostRateLimiter()
    sent = []
    for _ in range(4):
        limiter.wait(MONEX, deadline=200)
        sent.append(clock.seconds)
    assert sent == [100.5, 101.0, 101.5, 102.0]
    assert all(index + 1 <= 2 * (at - 100) for index, at in enumerate(sent))


def test_idle_time_never_creates_a_burst(clock):
    limiter = source_http.HostRateLimiter()
    clock.seconds += 10
    sent = []
    for _ in range(8):
        limiter.wait(MONEX, deadline=200)
        sent.append(clock.seconds)
    assert sent == [110 + index * 0.5 for index in range(8)]


@pytest.mark.parametrize(
    "url",
    [
        "http://scouter.monex.co.jp/report/theoryDps/7203",
        "https://private-secret.example/path",
        "https://private-secret@scouter.monex.co.jp/report/theoryDps/7203",
        "https://user:private-secret@scouter.monex.co.jp/report/theoryDps/7203",
        "https://scouter.monex.co.jp:443/report/theoryDps/7203",
        MONEX.encode(),
        OFFICIAL,
        IFRAME,
        "https://minkabu.jp/stock/7203/analyst_consensus",
    ],
)
def test_unknown_non_https_or_authenticated_host_is_rejected_safely(clock, url):
    limiter = source_http.HostRateLimiter()
    with pytest.raises(ValueError, match="fixed public HTTPS host") as caught:
        limiter.wait(url, deadline=200)
    assert "private-secret" not in str(caught.value)
    assert clock.seconds == 100


def test_pairs_share_one_host_bucket_and_default_two_second_delay_is_removed(clock):
    client = Session(clock)
    limiter = source_http.HostRateLimiter()
    for ticker in ("7203", "9432", "6758"):
        result = pair(client, limiter, ticker=ticker)
        assert result.outcomes[0].status == "observed"
    assert [at for at, _, _ in client.calls] == [100.5, 101, 101.5]
    assert all(kwargs["allow_redirects"] is False for _, _, kwargs in client.calls)


def test_new_cohort_or_process_waits_half_a_second_before_its_first_start(clock):
    first = source_http.HostRateLimiter()
    clock.seconds += 10
    first.wait(MONEX, deadline=200)
    assert clock.seconds == 110
    second = source_http.HostRateLimiter()
    second.wait(MONEX, deadline=200)
    assert clock.seconds == 110.5


def test_confirmed_429_deadline_is_returned_without_consuming_retry_http(clock):
    client = Session(clock, [(429, "synthetic denial", {"Retry-After": "60"})])
    result = pair(client, source_http.HostRateLimiter())
    assert result.stop_reason == "rate_limited"
    assert result.retry_after_not_before_utc == result.last_response_finished_at_utc + timedelta(
        seconds=60
    )
    assert result.requests == len(client.calls) == 1
    assert clock.seconds == 100.5


def test_float_rounding_cannot_inflate_remaining_above_attempt_limit(clock):
    clock.seconds = 100.3
    assert clock.seconds + 120 - clock.seconds > 120
    result = pair(Session(clock), source_http.HostRateLimiter())
    assert result.outcomes[0].status == "observed"
    assert result.requests == 1


def test_source_binding_includes_strict_spacing_and_only_its_parser(monkeypatch):
    assert collection.PAIR_ACQUISITION_POLICY_VERSION == "external-assessment-pair-acquisition-v14"
    assert source_http.SOURCE_REQUEST_INTERVAL_SECONDS == 0.5
    monex_binding = collection.collector_binding(source="monex")
    kabumap_binding = collection.collector_binding(source="kabumap")
    monkeypatch.setattr(collection.kabumap, "PARSER_VERSION", "changed-kabumap")
    assert collection.collector_binding(source="monex") == monex_binding
    assert collection.collector_binding(source="kabumap") != kabumap_binding
    monkeypatch.setitem(source_http.SOURCE_HOSTS, "kabumap", ("changed.example",))
    assert collection.collector_binding(source="monex") == monex_binding
    monkeypatch.setattr(source_http, "SOURCE_REQUEST_INTERVAL_SECONDS", 0.75)
    assert collection.collector_binding(source="monex") != monex_binding


@pytest.mark.parametrize(
    "url",
    [
        "https://jp.kabumap.com/servlets/kabumap/Action",
    ],
)
def test_additional_sources_keep_strict_spacing_after_idle(clock, url):
    limiter = source_http.HostRateLimiter()
    limiter.wait(url, deadline=120)
    assert clock.seconds == 100.5
    clock.seconds += 10
    limiter.wait(url, deadline=120)
    after_idle = clock.seconds
    for index in range(1, 5):
        limiter.wait(url, deadline=120)
        assert clock.seconds == after_idle + 0.5 * index


def test_token_wait_timeout_is_unattempted_with_no_http(clock):
    client = Session(clock, replies=[])
    result = pair(client, source_http.HostRateLimiter(), time_limit_seconds=0.1)
    assert result.requests == 0
    assert result.outcomes[0].status == "not_attempted"
    assert result.stop_reason == "wall_clock_limit"
    assert client.calls == []
