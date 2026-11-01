from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
import requests
from tests.engine.test_external_assessment_sources import (
    monex_html,
    official_html,
    rakuten_html,
    rakuten_no_estimates_html,
)
from tests.engine.test_kabumap import html as kabumap_html
from tests.jobs.test_external_assessment_daily import FakeObjectStore, plan

import baibai_engine.data_collection.transport.public_pages as source_http
import baibai_jobs.acquisition.external_assessments.daily as daily
import baibai_jobs.acquisition.external_assessments.staging as staging
from baibai_jobs.acquisition.external_assessments.collection import (
    collect_pair as external_assessment_collect_pair,
)

BASE = datetime(2026, 10, 11, 0, tzinfo=UTC)


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        elapsed = 0.0
        sleeps = []

        def now(self):
            return BASE + timedelta(seconds=self.elapsed)

        def sleep(self, seconds):
            assert 0 < seconds <= 30
            self.sleeps.append(seconds)
            self.elapsed += seconds

    value = Clock()

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return value.now().astimezone(tz or UTC)

    monkeypatch.setattr(daily, "datetime", FrozenDatetime)
    monkeypatch.setattr(source_http, "datetime", FrozenDatetime)
    monkeypatch.setattr(daily.time, "monotonic", lambda: value.elapsed)
    monkeypatch.setattr(daily.time, "sleep", value.sleep)
    monkeypatch.setattr(daily.secrets, "randbelow", lambda upper: 0)
    return value


def wire_collector(monkeypatch, store, p, clock, responses):
    class Session(requests.Session):
        def __init__(self):
            super().__init__()
            self.responses = iter(responses)
            self.calls = []
            self.limiters = []
            self.source = None
            self.ticker = None

        def get(self, url, **kwargs):
            self.calls.append((url, clock.now()))
            assert not any(event[0] == "PUT" for event in store.events)
            response = next(self.responses)
            clock.elapsed += 0.1
            if isinstance(response, Exception):
                raise response
            status, header, body = response
            result = requests.Response()
            result.status_code = status
            result.url = url
            result.encoding = "utf-8"
            result.headers["Content-Type"] = "text/html; charset=utf-8"
            result._content = body.encode()
            if header is not None:
                result.headers["Retry-After"] = header
            return result

    session = Session()

    def collect(ticker, *, source, **kwargs):
        session.ticker, session.source = ticker, source
        session.limiters.append(kwargs.get("rate_limiter"))
        return external_assessment_collect_pair(
            ticker, source=source, **{**kwargs, "session": session}
        )

    monkeypatch.setattr(daily, "external_assessment_collect_pair", collect)
    return session


def test_retry_is_process_local_and_one_stage_commit_preserves_actual_receipt(monkeypatch, clock):
    p, store = plan(), FakeObjectStore()
    client = wire_collector(
        monkeypatch, store, p, clock, [(429, "8", "denied"), (200, None, monex_html())]
    )
    report = daily.acquire_cohort(store, p, 0)
    assert report["provider_requests"] == 2
    assert client.calls[1][1] >= client.calls[0][1] + timedelta(seconds=8)
    saved = staging.read_stage(store, p, 0)[0]
    assert saved.result.entries[0].state == "success"
    assert saved.result.observations[0].fetched_at_utc == clock.now()
    assert not hasattr(saved, "retry_states")
    assert len(store.puts) == 2  # one stage, one bundle


def test_expired_cycle_keeps_unattempted_pairs_without_http_or_writes(monkeypatch, clock):
    p, store = plan(), FakeObjectStore()
    client = wire_collector(monkeypatch, store, p, clock, [])
    report = daily.acquire_cohort(store, p, 0, collect_before=BASE)
    assert report["provider_requests"] == 0
    assert report["stop_reason"] == "cycle_finished"
    assert report["state_counts"]["unattempted"] == 1
    assert client.calls == []
    assert store.puts == []


def test_retry_cannot_sleep_into_next_cycle(monkeypatch, clock):
    p, store = plan(), FakeObjectStore()
    client = wire_collector(monkeypatch, store, p, clock, [(429, "8", "denied")])
    report = daily.acquire_cohort(store, p, 0, collect_before=BASE + timedelta(seconds=5))
    assert report["provider_requests"] == 1
    assert len(client.calls) == 1
    assert clock.sleeps == [source_http.SOURCE_REQUEST_INTERVAL_SECONDS]
    assert report["stop_reason"] == "cycle_finished"
    saved = staging.read_stage(store, p, 0)[0]
    assert saved.result.entries[0].state == "failure"
    assert saved.result.entries[0].http_status == 429


def test_host_spacing_cannot_start_request_after_cycle_deadline(monkeypatch, clock):
    p, store = plan(), FakeObjectStore()
    client = wire_collector(monkeypatch, store, p, clock, [])
    report = daily.acquire_cohort(store, p, 0, collect_before=BASE + timedelta(seconds=0.1))
    assert report["provider_requests"] == 0
    assert client.calls == []
    assert store.puts == []


@pytest.mark.parametrize(("source", "limit"), [("monex", 6), ("rakuten", 12), ("kabumap", 6)])
def test_initial_plus_five_retry_stops_and_saved_failure_is_not_retried(
    monkeypatch, clock, source, limit
):
    p, store = plan(sources=(source,)), FakeObjectStore()
    replies = [(429, "0", "denied")] * 6
    if source == "rakuten":
        replies = [reply for denied in replies for reply in [(200, None, official_html()), denied]]
    client = wire_collector(monkeypatch, store, p, clock, replies)
    report = daily.acquire_cohort(store, p, 0)
    assert len(client.calls) == report["provider_requests"] == limit
    assert report["stop_reason"] == "rate_limited_retry_exhausted"
    assert all(
        (later[1] - earlier[1]).total_seconds() >= 0.5
        for earlier, later in zip(client.calls, client.calls[1:], strict=False)
    )
    saved = staging.read_stage(store, p, 0)[0]
    assert saved.result.entries[0].reason == "rate_limited"
    before = list(store.puts)
    assert daily.acquire_cohort(store, p, 0)["provider_requests"] == 0
    assert store.puts == before
    assert len(client.calls) == limit


def test_rakuten_retry_restarts_outer_page_and_archives_every_received_200(monkeypatch, clock):
    p, store = plan(sources=("rakuten",)), FakeObjectStore()
    client = wire_collector(
        monkeypatch,
        store,
        p,
        clock,
        [
            (200, None, official_html()),
            (429, "0", "denied"),
            (200, None, official_html()),
            (200, None, rakuten_html()),
        ],
    )
    report = daily.acquire_cohort(store, p, 0)
    assert report["provider_requests"] == len(client.calls) == 4
    assert client.calls[0][0] == client.calls[2][0]
    saved = staging.read_stage(store, p, 0)[0]
    assert saved.result.entries[0].state == "success"
    from baibai_jobs.integration.external_assessment_raw import read_raw_bundle

    manifest, bodies = read_raw_bundle(store, saved.raw_bundle_keys[0])
    assert len(manifest["receipts"]) == len(bodies) == 3
    assert bodies[0].role == bodies[1].role == "page"
    assert bodies[0].attempt_id != bodies[1].attempt_id
    assert bodies[2].role == "iframe"
    assert bodies[2].attempt_id == bodies[1].attempt_id == saved.result.entries[0].attempt_id
    assert bodies[2].parent_receipt_id == bodies[1].receipt_id


@pytest.mark.parametrize(
    ("header", "stop"),
    [("18000", "retry_soft_budget"), ("9" * 5000, "retry_after_unrepresentable")],
)
def test_retry_deadline_outside_budget_records_failure_and_resume_only_missing(
    monkeypatch, clock, header, stop
):
    p, store = plan(tickers=("6758", "7203")), FakeObjectStore()
    client = wire_collector(monkeypatch, store, p, clock, [(429, header, "denied")])
    report = daily.acquire_cohort(store, p, 0)
    assert report["stop_reason"] == stop
    assert report["cohort_complete"] is False
    partial = staging.read_stage(store, p, 0)[0]
    assert [entry.state for entry in partial.result.entries] == ["failure", "unattempted"]
    assert len(client.calls) == 1
    store.events.clear()
    next_client = wire_collector(monkeypatch, store, p, clock, [(200, None, monex_html("7203"))])
    if header == "18000":
        waiting = daily.acquire_cohort(store, p, 0)
        assert waiting["provider_requests"] == 0
        assert waiting["stop_reason"] == "retry_not_before"
        assert next_client.calls == []
        assert staging.read_stage(store, p, 0)[0] == partial
        # A new work label must respect the same source quota, not reset it.
        other = replace(p, run_id="next-cycle-same-source")
        assert daily.acquire_cohort(store, other, 0)["provider_requests"] == 0
        clock.elapsed += 18001
    assert daily.acquire_cohort(store, p, 0)["cohort_complete"] is True
    assert len(next_client.calls) == 1
    assert "7203" in next_client.calls[0][0]
    assert staging.read_stage(store, p, 0)[0].result.entries[0] == partial.result.entries[0]


def test_http_date_retry_deadline_is_not_replaced_by_short_fallback(monkeypatch, clock):
    p, store = plan(), FakeObjectStore()
    client = wire_collector(
        monkeypatch,
        store,
        p,
        clock,
        [(429, "Sun, 11 Oct 2026 00:00:12 GMT", "denied"), (200, None, monex_html())],
    )
    daily.acquire_cohort(store, p, 0)
    assert client.calls[1][1] >= BASE + timedelta(seconds=12)


def test_timeout_has_one_attempt_and_controlled_wall_clock_stop_saves_partial(monkeypatch, clock):
    p, store = plan(tickers=("6758", "7203")), FakeObjectStore()

    client = wire_collector(monkeypatch, store, p, clock, [source_http.TimeLimit()])
    report = daily.acquire_cohort(store, p, 0)
    assert report["stop_reason"] == "wall_clock_limit"
    assert len(client.calls) == 1
    saved = staging.read_stage(store, p, 0)[0]
    assert [entry.state for entry in saved.result.entries] == ["failure", "unattempted"]
    assert saved.result.entries[0].reason == "request_timeout"
    assert saved.result.entries[0].attempt_id is not None
    assert saved.raw_bundle_keys == ()


def test_403_stops_current_cohort_after_one_request(monkeypatch, clock):
    p, store = plan(tickers=("6758", "7203")), FakeObjectStore()
    client = wire_collector(monkeypatch, store, p, clock, [(403, None, "denied")])
    report = daily.acquire_cohort(store, p, 0)
    assert report["stop_reason"] == "access_denied"
    assert report["cohort_complete"] is False
    assert len(client.calls) == 1


def test_host_bucket_is_shared_across_pairs_and_resets_without_restart_credit(monkeypatch, clock):
    p, store = plan(tickers=("6758", "7203")), FakeObjectStore()
    client = wire_collector(
        monkeypatch,
        store,
        p,
        clock,
        [(200, None, monex_html("6758")), (200, None, monex_html("7203"))],
    )
    daily.acquire_cohort(store, p, 0)
    assert client.limiters[0] is client.limiters[1]
    assert (client.calls[1][1] - client.calls[0][1]).total_seconds() >= 0.5


def test_rotated_hosts_preserve_saved_order_and_partial_receipts_on_resume(monkeypatch, clock):
    p = plan(tickers=("130A", "7203", "8301"), sources=("kabumap", "monex", "rakuten"))
    store = FakeObjectStore()
    original_plan = staging.plan_bytes(p)
    client = wire_collector(
        monkeypatch,
        store,
        p,
        clock,
        [
            (200, None, kabumap_html("130A")),
            (200, None, monex_html("130A", value="--")),
            (200, None, official_html("130A")),
            (200, None, rakuten_html("130A", value="0")),
            (200, None, kabumap_html("7203")),
            (200, None, monex_html("7203").replace("theory_summary_inner", "missing")),
            (200, None, official_html("7203")),
            (200, None, rakuten_no_estimates_html()),
        ],
    )
    collect = daily.external_assessment_collect_pair
    seen = []

    def limited(ticker, *, source, **kwargs):
        seen.append((source, ticker))
        result = collect(ticker, source=source, **kwargs)
        if (source, ticker) == ("rakuten", "7203"):
            clock.elapsed = 101
        return result

    monkeypatch.setattr(daily, "external_assessment_collect_pair", limited)
    report = daily.acquire_cohort(store, p, 0, soft_budget_seconds=1000)
    assert report["stop_reason"] == "soft_budget"
    assert report["state_counts"] == {
        "success": 3,
        "normal_null": 1,
        "failure": 2,
        "unattempted": 3,
    }
    assert seen == [(source, ticker) for ticker in ("130A", "7203") for source in p.sources]
    assert len(client.calls) == report["provider_requests"] == 8
    assert all(limiter is client.limiters[0] for limiter in client.limiters)
    saved = staging.read_stage(store, p, 0)[0]
    assert (
        tuple((entry.source, entry.ticker) for entry in saved.result.entries) == p.cohorts[0].pairs
    )
    assert staging.plan_bytes(p) == original_plan
    received = {
        (entry.source, entry.ticker): entry
        for entry in saved.result.entries
        if entry.state != "unattempted"
    }
    assert received["rakuten", "130A"].observation.rating == 0
    assert received["monex", "130A"].observation.pbr is None
    assert received["monex", "7203"].reason == "missing_theory_table"
    assert received["rakuten", "7203"].reason == "no_estimates_identity_unconfirmed"

    store.events.clear()
    resumed = wire_collector(
        monkeypatch,
        store,
        p,
        clock,
        [
            (200, None, kabumap_html("8301")),
            (200, None, monex_html("8301")),
            (200, None, official_html("8301")),
            (200, None, rakuten_html("8301")),
        ],
    )
    report = daily.acquire_cohort(store, p, 0)
    assert report["cohort_complete"] is True
    assert len(resumed.calls) == report["provider_requests"] == 4
    complete = staging.read_stage(store, p, 0)[0]
    assert all(
        received[key] == entry
        for entry in complete.result.entries
        if (key := (entry.source, entry.ticker)) in received
    )
    before = store.objects.copy()
    report = daily.acquire_cohort(store, p, 0)
    assert report["provider_requests"] == 0
    assert len(resumed.calls) == 4
    assert store.objects == before
