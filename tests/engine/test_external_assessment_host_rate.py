from __future__ import annotations

from datetime import timedelta

import pytest
import requests
from tests.engine.test_external_assessment_sources import monex_html, official_html, rakuten_html

from baibai_engine.collection.equity_web import http as source_http
from baibai_engine.external_assessment.acquisition import collection

MONEX = "https://scouter.monex.co.jp/report/theoryDps/7203"
OFFICIAL = "https://www.rakuten-sec.co.jp/web/market/search/forecast.html?ric=7203.T"
IFRAME = "https://rakuten-sec.ifis.co.jp/index.php?action=tp4&sa=report_est&bcode=7203.T"


class Clock:
    def __init__(self):
        self.seconds = 100.0

    def monotonic(self):
        return self.seconds

    def sleep(self, seconds):
        self.seconds += seconds


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(collection.time, "monotonic", value.monotonic)
    monkeypatch.setattr(collection.time, "sleep", value.sleep)
    return value


class Session(requests.Session):
    def __init__(self, clock, replies=None):
        super().__init__()
        self.clock = clock
        self.replies = iter(replies) if replies is not None else None
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((self.clock.seconds, url, kwargs))
        status, html, headers = (
            next(self.replies)
            if self.replies is not None
            else (200, monex_html(url.rsplit("/", 1)[-1]), {})
        )
        response = requests.Response()
        response.status_code = status
        response._content = html.encode()
        response._content_consumed = True
        response.headers.update(headers)
        return response


def pair(client, limiter, *, ticker="7203", source="monex", **kwargs):
    return collection.collect_pair(
        ticker,
        source=source,
        session=client,
        rate_limiter=limiter,
        **kwargs,
    )


def test_cold_bucket_has_no_credit_and_refills_two_per_second(clock):
    limiter = source_http.HostRateLimiter()
    sent = []
    for _ in range(4):
        limiter.wait(MONEX, deadline=200)
        sent.append(clock.seconds)
    assert sent == [100.5, 101.0, 101.5, 102.0]
    assert all(index + 1 <= 2 * (at - 100) for index, at in enumerate(sent))


def test_idle_credit_is_capped_and_actual_sends_obey_every_window(clock):
    limiter = source_http.HostRateLimiter()
    clock.seconds += 10
    sent = []
    for _ in range(20):
        limiter.wait(MONEX, deadline=200)
        sent.append(clock.seconds)
    assert sent[:4] == [110] * 4
    assert sent[4] == 110.5
    for first in range(len(sent)):
        for last in range(first, len(sent)):
            assert last - first + 1 <= 4 + 2 * (sent[last] - sent[first])


def test_three_fixed_hosts_have_independent_buckets(clock):
    limiter = source_http.HostRateLimiter()
    for url in (MONEX, OFFICIAL, IFRAME):
        limiter.wait(url, deadline=200)
        assert clock.seconds == 100.5
    limiter.wait(MONEX, deadline=200)
    assert clock.seconds == 101


@pytest.mark.parametrize(
    "url",
    [
        "http://scouter.monex.co.jp/report/theoryDps/7203",
        "https://private-secret.example/path",
        "https://private-secret@scouter.monex.co.jp/report/theoryDps/7203",
        "https://user:private-secret@scouter.monex.co.jp/report/theoryDps/7203",
        "https://scouter.monex.co.jp:443/report/theoryDps/7203",
        MONEX.encode(),
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


def test_rakuten_two_hosts_use_shared_limiter_without_old_within_pair_delay(clock):
    client = Session(clock, [(200, official_html(), {}), (200, rakuten_html(), {})])
    result = pair(client, source_http.HostRateLimiter(), source="rakuten")
    assert result.requests == 2
    assert [at for at, _, _ in client.calls] == [100.5, 100.5]
    assert result.outcomes[0].status == "observed"


def test_new_cohort_or_process_cold_start_never_recreates_unused_credit(clock):
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


def test_v9_binding_includes_fixed_rate_policy(monkeypatch):
    assert collection.PAIR_ACQUISITION_POLICY_VERSION == "external-assessment-pair-acquisition-v9"
    assert source_http.HOST_REQUEST_RATE_PER_SECOND == 2
    assert source_http.HOST_REQUEST_BURST_CREDITS == 4
    binding = collection.collector_binding()
    assert binding == "7b9688aa1e92d642aeaf71f1e197626ee26388e5fc7e8e1ab1e3ed506ff6fb7d"
    monkeypatch.setattr(source_http, "HOST_REQUEST_RATE_PER_SECOND", 3)
    assert collection.collector_binding() != binding


@pytest.mark.parametrize(
    "url",
    [
        "https://jp.kabumap.com/servlets/kabumap/Action",
        "https://minkabu.jp/stock/7203/analyst_consensus",
    ],
)
def test_additional_sources_share_two_requests_per_second_and_four_credit_burst(clock, url):
    bucket = source_http.HostRateLimiter()
    bucket.wait(url, deadline=120)
    first = clock.seconds
    bucket.wait(url, deadline=120)
    assert clock.seconds - first == pytest.approx(0.5)
    clock.seconds += 10
    bucket.wait(url, deadline=120)
    after_idle = clock.seconds
    for _ in range(3):
        bucket.wait(url, deadline=120)
        assert clock.seconds == after_idle
    bucket.wait(url, deadline=120)
    assert clock.seconds - after_idle == pytest.approx(0.5)


def test_token_wait_timeout_is_unattempted_with_no_http(clock):
    client = Session(clock, replies=[])
    result = pair(client, source_http.HostRateLimiter(), time_limit_seconds=0.1)
    assert result.requests == 0
    assert result.outcomes[0].status == "not_attempted"
    assert result.stop_reason == "wall_clock_limit"
    assert client.calls == []


def test_minkabu_404_requires_explicit_profile_evidence_and_retains_only_received_200(clock):
    from tests.engine.test_minkabu import no_coverage_html

    from baibai_engine.external_assessment.normalization.decode import decode_attempt

    originals = []
    client = Session(clock, [(404, "not found", {}), (200, no_coverage_html(), {})])
    result = pair(
        client,
        source_http.HostRateLimiter(),
        ticker="130A",
        source="minkabu",
        on_received_html=originals.append,
    )
    assert result.requests == 2
    assert result.outcomes[0].status == "observed"
    assert result.observations[0].availability == "not_covered"
    assert [url for _, url, _ in client.calls] == [
        "https://minkabu.jp/stock/130A/analyst_consensus",
        "https://minkabu.jp/stock/130A",
    ]
    assert len(originals) == 1
    assert decode_attempt(originals) == result.observations[0]
    assert originals[0].requested_url.endswith("/130A")


@pytest.mark.parametrize("status", [403, 429, 500])
def test_minkabu_never_uses_profile_to_bypass_denial_or_http_failure(clock, status):
    client = Session(clock, [(status, "denied", {})])
    result = pair(client, source_http.HostRateLimiter(), source="minkabu")
    assert result.requests == 1
    assert result.observations == ()
    assert result.outcomes[0].http_status == status


def test_minkabu_404_with_unconfirmed_profile_is_failure_not_normal_null(clock):
    from tests.engine.test_minkabu import no_coverage_html

    client = Session(
        clock, [(404, "not found", {}), (200, no_coverage_html().replace("対象外", "不明"), {})]
    )
    result = pair(client, source_http.HostRateLimiter(), ticker="130A", source="minkabu")
    assert result.requests == 2
    assert result.observations == ()
    assert result.outcomes[0].status == "parse_failure"
    assert result.outcomes[0].reason == "unconfirmed_no_analyst_coverage"
