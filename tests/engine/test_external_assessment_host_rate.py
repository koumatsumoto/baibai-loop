from __future__ import annotations

from datetime import timedelta

import pytest
import requests
from tests.engine.test_external_assessment_sources import monex_html, official_html, rakuten_html

from baibai_engine.data_collection.equity_web import http as source_http
import baibai_jobs.acquisition.external_assessments.collection as collection

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


def test_rakuten_outer_and_iframe_share_one_source_clock(clock):
    limiter = source_http.HostRateLimiter()
    for url, expected in ((MONEX, 100.5), (OFFICIAL, 100.5), (IFRAME, 101), (OFFICIAL, 101.5)):
        limiter.wait(url, deadline=200)
        assert clock.seconds == expected


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


def test_rakuten_two_http_starts_are_half_a_second_apart(clock):
    client = Session(clock, [(200, official_html(), {}), (200, rakuten_html(), {})])
    result = pair(client, source_http.HostRateLimiter(), source="rakuten")
    assert result.requests == 2
    assert [at for at, _, _ in client.calls] == [100.5, 101.0]
    assert result.outcomes[0].status == "observed"


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
    minkabu_binding = collection.collector_binding(source="minkabu")
    monkeypatch.setattr(collection.minkabu, "PARSER_VERSION", "changed-minkabu")
    assert collection.collector_binding(source="monex") == monex_binding
    assert collection.collector_binding(source="minkabu") != minkabu_binding
    monkeypatch.setitem(source_http.SOURCE_HOSTS, "minkabu", ("changed.example",))
    assert collection.collector_binding(source="monex") == monex_binding
    monkeypatch.setattr(source_http, "SOURCE_REQUEST_INTERVAL_SECONDS", 0.75)
    assert collection.collector_binding(source="monex") != monex_binding


@pytest.mark.parametrize(
    "url",
    [
        "https://jp.kabumap.com/servlets/kabumap/Action",
        "https://minkabu.jp/stock/7203/analyst_consensus",
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


@pytest.mark.parametrize("linked", [False, True])
def test_minkabu_404_requires_explicit_profile_evidence_and_retains_only_received_200(
    clock, linked
):
    from tests.engine.test_minkabu import no_coverage_html

    from baibai_engine.machine_analysis.observations.external_assessments.normalization.decode import (
        decode_attempt,
    )

    originals = []
    client = Session(clock, [(404, "not found", {}), (200, no_coverage_html(linked=linked), {})])
    result = pair(
        client,
        source_http.HostRateLimiter(),
        ticker="130A",
        source="minkabu",
        on_received_html=originals.append,
    )
    assert result.requests == 2
    assert [at for at, _, _ in client.calls] == [100.5, 101.0]
    assert result.outcomes[0].status == "observed"
    assert result.observations[0].availability == "not_covered"
    assert [url for _, url, _ in client.calls] == [
        "https://minkabu.jp/stock/130A/analyst_consensus",
        "https://minkabu.jp/stock/130A",
    ]
    assert all(
        kwargs["headers"] == source_http.MINKABU_PUBLIC_HEADERS for _, _, kwargs in client.calls
    )
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


def test_minkabu_identifies_actual_client_without_changing_other_sources(clock):
    from tests.engine.test_minkabu import html

    client = Session(clock, [(200, html(), {}), (200, monex_html(), {})])
    limiter = source_http.HostRateLimiter()
    assert pair(client, limiter, source="minkabu").outcomes[0].status == "observed"
    assert client.calls[0][2]["headers"] == {
        "User-Agent": "baibai-loop-public-research/1.0",
        "Accept": "text/html",
    }
    assert pair(client, limiter, source="monex").outcomes[0].status == "observed"
    assert "headers" not in client.calls[1][2]


def test_minkabu_header_policy_is_bound_to_fixed_plan(monkeypatch):
    before = collection.collector_binding()
    monkeypatch.setitem(source_http.MINKABU_PUBLIC_HEADERS, "Accept", "application/xhtml+xml")
    assert collection.collector_binding() != before
