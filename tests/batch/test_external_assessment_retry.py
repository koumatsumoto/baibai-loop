from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
import requests
from tests.batch.test_external_assessment_daily import FakeObjectStore, observation, plan
from tests.engine.test_external_assessment_sources import monex_html, official_html, rakuten_html

from baibai_batch.jobs import external_assessment_daily as daily
from baibai_batch.jobs.external_assessment_cohorts import mark_inflight, new_journal, record_outcome
from baibai_batch.storage import external_assessment_staging as staging
from baibai_engine.batch_api import (
    ExternalAssessmentCollectionOutcome,
    ExternalAssessmentHostRateLimiter,
    external_assessment_collect_pair,
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

    engine = sys.modules[external_assessment_collect_pair.__module__]
    monkeypatch.setattr(daily, "datetime", FrozenDatetime)
    monkeypatch.setattr(engine, "datetime", FrozenDatetime)
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
            index = next(c.index for c in p.cohorts if self.ticker in c.tickers)
            saved = staging.read_checkpoints(store, p)[index][0]
            entry = next(
                item
                for item in saved.journal.entries
                if (item.source, item.ticker) == (self.source, self.ticker)
            )
            assert entry.state == "uncertain"
            assert entry.reason == "inflight"
            retry = saved.retry_for(self.source, self.ticker)
            self.calls.append((url, clock.now(), retry.retries_used if retry else 0))
            response = next(self.responses)
            clock.elapsed += 0.1
            if isinstance(response, Exception):
                raise response
            status, header, body = response
            result = requests.Response()
            result.status_code = status
            result.url = url
            result.encoding = "utf-8"
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


def saved(store, p, index=0):
    return staging.read_checkpoints(store, p)[index][0]


def known_429(p, *, index=0, retries_used=0, deadline=BASE + timedelta(seconds=10), overflow=False):
    ticker = p.cohorts[index].tickers[0]
    source = p.sources[0]
    journal = mark_inflight(p, new_journal(p, index), source, ticker)
    journal = record_outcome(
        p, journal, ExternalAssessmentCollectionOutcome(source, ticker, "rate_limited", None, 429)
    )
    used = retries_used + 1
    return staging.Checkpoint(
        journal,
        requests_started=used,
        confirmed_http_requests=used,
        last_response_finished_at_utc=BASE,
        retry_states=(staging.PairRetryState(source, ticker, retries_used, deadline, overflow),),
    )


def seed_checkpoint(store, p, checkpoint):
    store.seed(
        staging.checkpoint_key(p, checkpoint.journal.cohort_index),
        staging.checkpoint_bytes(p, checkpoint),
    )


def test_known_429_retries_after_deadline_with_counter_reserved_before_http(monkeypatch, clock):
    p, store = plan(), FakeObjectStore()
    session = wire_collector(
        monkeypatch,
        store,
        p,
        clock,
        [(429, "7", "synthetic rate limit"), (200, None, monex_html())],
    )
    report = daily.acquire_cohort(store, p, 0)
    assert len(session.calls) == 2
    assert session.calls[1][1] >= session.calls[0][1] + timedelta(seconds=7.1)
    assert [item[2] for item in session.calls] == [0, 1]
    checkpoint = saved(store, p)
    assert checkpoint.retry_for("monex", "7203").retries_used == 1
    assert checkpoint.retry_for("monex", "7203").next_retry_utc is None
    assert checkpoint.journal.entries[0].state == "success"
    assert report["request_reservations"] == report["confirmed_http_requests"] == 2
    assert report["stop_reason"] is None
    assert session.limiters[0] is session.limiters[1]
    assert isinstance(session.limiters[0], ExternalAssessmentHostRateLimiter)


def test_daily_shares_one_host_bucket_across_pairs_and_cold_restart(monkeypatch, clock):
    tickers = tuple(str(1230 + index) for index in range(6))
    p, store = plan(tickers=tickers), FakeObjectStore()
    session = wire_collector(
        monkeypatch,
        store,
        p,
        clock,
        [(200, None, monex_html(ticker)) for ticker in tickers],
    )
    daily.acquire_cohort(store, p, 0)
    assert len(session.calls) == 6
    assert session.calls[0][1] >= BASE + timedelta(seconds=0.5)
    assert len({id(limiter) for limiter in session.limiters}) == 1
    for start, first in enumerate(session.calls):
        for end, last in enumerate(session.calls[start:], start):
            elapsed = (last[1] - first[1]).total_seconds()
            assert end - start + 1 <= 4 + 2 * elapsed
    other = plan(tickers=("7203",))
    other_store = FakeObjectStore()
    other_session = wire_collector(
        monkeypatch, other_store, other, clock, [(200, None, monex_html())]
    )
    restarted = clock.now()
    daily.acquire_cohort(other_store, other, 0)
    assert other_session.calls[0][1] >= restarted + timedelta(seconds=0.5)
    assert other_session.limiters[0] is not session.limiters[0]


@pytest.mark.parametrize("source", ["monex", "rakuten"])
def test_initial_plus_five_retry_is_exhausted_once_and_restart_cannot_reset_it(
    monkeypatch, clock, source
):
    p, store = plan(sources=(source,)), FakeObjectStore()
    replies = [(429, None, "synthetic rate limit")] * 6
    if source == "rakuten":
        replies = [
            item
            for _ in range(6)
            for item in ((200, None, official_html()), (429, None, "synthetic rate limit"))
        ]
    session = wire_collector(monkeypatch, store, p, clock, replies)
    report = daily.acquire_cohort(store, p, 0)
    expected = 6 if source == "monex" else 12
    assert len(session.calls) == expected
    assert [item[2] for item in session.calls] == (
        list(range(6)) if source == "monex" else [used for used in range(6) for _ in range(2)]
    )
    step = 1 if source == "monex" else 2
    for attempt, delay in enumerate((5, 10, 20, 40, 80)):
        previous = session.calls[attempt * step + step - 1][1]
        following = session.calls[(attempt + 1) * step][1]
        assert following >= previous + timedelta(seconds=delay + 0.1)
    checkpoint = saved(store, p)
    retry = checkpoint.retry_for(source, "7203")
    assert retry.retries_used == 5
    assert retry.next_retry_utc is None
    assert checkpoint.journal.entries[0].reason == "rate_limited"
    assert report["stop_reason"] == "rate_limited_retry_exhausted"
    assert report["request_reservations"] == report["confirmed_http_requests"] == expected
    puts = len(store.puts)
    with pytest.raises(ValueError, match="halted"):
        daily.acquire_cohort(store, p, 0)
    assert len(session.calls) == expected
    assert len(store.puts) == puts


def test_rakuten_retry_restarts_outer_page_and_saves_only_confirmed_null(monkeypatch, clock):
    p, store = plan(sources=("rakuten",)), FakeObjectStore()
    session = wire_collector(
        monkeypatch,
        store,
        p,
        clock,
        [
            (200, None, official_html()),
            (429, "8", "synthetic rate limit"),
            (200, None, official_html()),
            (200, None, rakuten_html(value="--")),
        ],
    )
    report = daily.acquire_cohort(store, p, 0)
    assert len(session.calls) == 4
    assert session.calls[0][0] == session.calls[2][0]
    assert session.calls[1][0] == session.calls[3][0]
    assert session.calls[2][1] >= session.calls[1][1] + timedelta(seconds=8.1)
    assert [item[2] for item in session.calls] == [0, 0, 1, 1]
    checkpoint = saved(store, p)
    assert checkpoint.journal.entries[0].state == "normal_null"
    assert len(checkpoint.journal.observations) == 1
    assert checkpoint.journal.observations[0].fetched_at_utc > session.calls[2][1]
    assert report["confirmed_http_requests"] == report["request_reservations"] == 4


def test_long_retry_after_budget_defers_without_consuming_retry_and_resumes_exact_deadline(
    monkeypatch, clock
):
    p, store = plan(), FakeObjectStore()
    session = wire_collector(
        monkeypatch,
        store,
        p,
        clock,
        [
            (429, "3600", "synthetic rate limit"),
            (200, None, monex_html()),
        ],
    )
    report = daily.acquire_cohort(store, p, 0, soft_budget_seconds=1000)
    checkpoint = saved(store, p)
    retry = checkpoint.retry_for("monex", "7203")
    assert report["stop_reason"] == "retry_pending_soft_budget"
    assert retry.retries_used == 0
    assert retry.next_retry_utc == BASE + timedelta(seconds=3600.6)
    assert len(session.calls) == 1
    assert clock.sleeps == [0.5]
    payload = store.objects[staging.checkpoint_key(p, 0)][0]
    again = daily.acquire_cohort(store, p, 0, soft_budget_seconds=1000)
    assert again["stop_reason"] == "retry_pending_soft_budget"
    assert store.objects[staging.checkpoint_key(p, 0)][0] == payload
    assert len(session.calls) == 1
    clock.elapsed = 3600
    report = daily.acquire_cohort(store, p, 0, soft_budget_seconds=1000)
    assert session.calls[1][1] >= retry.next_retry_utc
    assert saved(store, p).retry_for("monex", "7203").retries_used == 1
    assert report["stop_reason"] is None


def test_http_date_retry_deadline_is_not_replaced_by_short_fallback(monkeypatch, clock):
    p, store = plan(), FakeObjectStore()
    session = wire_collector(
        monkeypatch,
        store,
        p,
        clock,
        [
            (429, "Sun, 11 Oct 2026 00:01:00 GMT", "synthetic rate limit"),
            (200, None, monex_html()),
        ],
    )
    daily.acquire_cohort(store, p, 0)
    assert session.calls[1][1] >= BASE + timedelta(minutes=1)
    assert 30 in clock.sleeps


def test_unrepresentable_retry_after_is_confirmed_failure_with_no_fallback_retry(
    monkeypatch, clock
):
    p, store = plan(), FakeObjectStore()
    session = wire_collector(
        monkeypatch,
        store,
        p,
        clock,
        [
            (429, "9" * 100, "synthetic rate limit"),
        ],
    )
    report = daily.acquire_cohort(store, p, 0)
    assert len(session.calls) == 1
    retry = saved(store, p).retry_for("monex", "7203")
    assert retry.retry_after_unrepresentable
    assert retry.next_retry_utc is None
    assert retry.retries_used == 0
    assert report["stop_reason"] == "retry_after_unrepresentable"
    with pytest.raises(ValueError, match="halted"):
        daily.acquire_cohort(store, p, 0)
    assert len(session.calls) == 1


@pytest.mark.parametrize("fault", ["cas", "after"])
def test_retry_reservation_cas_or_lost_put_response_stops_before_next_http(
    monkeypatch, clock, fault
):
    p, store = plan(), FakeObjectStore()
    store.put_fault = (3, fault)
    session = wire_collector(
        monkeypatch,
        store,
        p,
        clock,
        [
            (429, "0", "synthetic rate limit"),
            (200, None, monex_html()),
        ],
    )
    with pytest.raises(RuntimeError, match="pair_checkpoint_callback_failed"):
        daily.acquire_cohort(store, p, 0)
    assert len(session.calls) == 1
    checkpoint = saved(store, p)
    retry = checkpoint.retry_for("monex", "7203")
    if fault == "after":
        assert retry.retries_used == 1
        assert retry.next_retry_utc is None
        assert checkpoint.journal.entries[0].state == "uncertain"
        with pytest.raises(ValueError, match="uncertain"):
            daily.acquire_cohort(store, p, 0)
    else:
        assert retry.retries_used == 0
        assert retry.next_retry_utc is not None
        assert checkpoint.journal.entries[0].state == "failure"
    assert len(session.calls) == 1


def test_request_timeout_is_confirmed_failure_and_never_retried(monkeypatch, clock):
    p, store = plan(), FakeObjectStore()
    session = wire_collector(monkeypatch, store, p, clock, [requests.Timeout("synthetic timeout")])
    report = daily.acquire_cohort(store, p, 0)
    assert len(session.calls) == 1
    assert report["stop_reason"] is None
    checkpoint = saved(store, p)
    assert checkpoint.journal.entries[0].state == "failure"
    assert checkpoint.journal.entries[0].reason == "request_timeout"
    assert checkpoint.retry_states == ()
    daily.acquire_cohort(store, p, 0)
    assert len(session.calls) == 1


def test_attempt_deadline_with_unknown_outcome_stays_uncertain_without_retry(monkeypatch, clock):
    p, store = plan(), FakeObjectStore()
    session = wire_collector(monkeypatch, store, p, clock, [(200, None, monex_html())])
    original = session.get

    def slow_get(*args, **kwargs):
        result = original(*args, **kwargs)
        clock.elapsed += 121
        return result

    monkeypatch.setattr(session, "get", slow_get)
    report = daily.acquire_cohort(store, p, 0)
    assert len(session.calls) == 1
    assert report["stop_reason"] == "uncertain_inflight"
    checkpoint = saved(store, p)
    assert checkpoint.journal.entries[0].state == "uncertain"
    assert checkpoint.retry_states == ()
    with pytest.raises(ValueError, match="uncertain"):
        daily.acquire_cohort(store, p, 0)
    assert len(session.calls) == 1


def test_pending_retry_plan_selects_its_cohort_and_redispatch_can_reach_remaining_work(
    monkeypatch, clock, tmp_path
):
    p, store = plan(tickers=("6758", "7203", "9432"), cohort_size=1), FakeObjectStore()
    staging.save_new_plan(store, p)
    first = mark_inflight(p, new_journal(p, 0), "monex", "6758")
    first = record_outcome(
        p,
        first,
        ExternalAssessmentCollectionOutcome("monex", "6758", "observed", None, 200),
        observation("6758"),
    )
    seed_checkpoint(
        store, p, staging.Checkpoint(first, requests_started=1, confirmed_http_requests=1)
    )
    seed_checkpoint(store, p, known_429(p, index=1, deadline=BASE + timedelta(seconds=8)))
    session = wire_collector(
        monkeypatch,
        store,
        p,
        clock,
        [
            (200, None, monex_html("7203")),
            (200, None, monex_html("9432")),
        ],
    )
    monkeypatch.setattr(daily, "Boto3R2Store", lambda **kwargs: store)
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    args = ["plan", "--run-id", p.run_id, "--master-asof", "2026-10-11"]
    store.events.clear()
    assert daily.main(args) == 0
    checkpoint_events = [event for event in store.events if "/cohort-" in event[1]]
    assert sum(event[0] == "HEAD" for event in checkpoint_events) == 5
    assert sum(event[0] == "GET" for event in checkpoint_events) == 2
    assert sum(event[0] == "HEAD" for event in store.events) == 8
    assert sum(event[0] == "GET" for event in store.events) == 3
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert values["cohorts"] == "[1]"
    assert values["retry_only"] == "true"
    assert daily.acquire_cohort(store, p, 0)["stop_reason"] is None
    with pytest.raises(ValueError, match="another cohort"):
        daily.acquire_cohort(store, p, 2)
    assert session.calls == []
    assert daily.acquire_cohort(store, p, 1)["stop_reason"] is None
    output.write_text("")
    assert daily.main(args) == 0
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert values["cohorts"] == "[2]"
    assert values["retry_only"] == "false"
    assert daily.acquire_cohort(store, p, 2)["stop_reason"] is None
    assert len(session.calls) == 2
    assert saved(store, p, 0).journal.observations == (observation("6758"),)


def test_multiple_pending_retry_pairs_are_rejected_before_any_http(monkeypatch, clock):
    p, store = plan(tickers=("6758", "7203"), cohort_size=1), FakeObjectStore()
    for index in (0, 1):
        seed_checkpoint(store, p, known_429(p, index=index))
    session = wire_collector(monkeypatch, store, p, clock, [])
    with pytest.raises(ValueError, match="multiple pending"):
        daily.acquire_cohort(store, p, 0)
    assert session.calls == []


def test_pending_retry_precedes_unattempted_pair_in_same_cohort(monkeypatch, clock):
    p, store = plan(tickers=("6758", "7203")), FakeObjectStore()
    journal = mark_inflight(p, new_journal(p, 0), "monex", "7203")
    journal = record_outcome(
        p, journal, ExternalAssessmentCollectionOutcome("monex", "7203", "rate_limited", None, 429)
    )
    deadline = BASE + timedelta(seconds=30)
    seed_checkpoint(
        store,
        p,
        staging.Checkpoint(
            journal,
            requests_started=1,
            confirmed_http_requests=1,
            last_response_finished_at_utc=BASE,
            retry_states=(staging.PairRetryState("monex", "7203", 0, deadline),),
        ),
    )
    session = wire_collector(
        monkeypatch,
        store,
        p,
        clock,
        [(200, None, monex_html("7203")), (200, None, monex_html("6758"))],
    )
    assert daily.acquire_cohort(store, p, 0)["stop_reason"] is None
    assert session.calls[0][1] >= deadline
    assert "7203" in session.calls[0][0]
    assert "6758" in session.calls[1][0]


def test_resumed_four_retries_consumes_only_last_slot_and_then_halts(monkeypatch, clock):
    p, store = plan(), FakeObjectStore()
    seed_checkpoint(store, p, known_429(p, retries_used=4, deadline=BASE))
    session = wire_collector(monkeypatch, store, p, clock, [(429, "0", "synthetic")])
    report = daily.acquire_cohort(store, p, 0)
    assert [item[2] for item in session.calls] == [5]
    checkpoint = saved(store, p)
    assert checkpoint.requests_started == 6
    assert checkpoint.retry_for("monex", "7203").retries_used == 5
    assert report["stop_reason"] == "rate_limited_retry_exhausted"
    with pytest.raises(ValueError, match="halted"):
        daily.acquire_cohort(store, p, 0)
    assert len(session.calls) == 1


@pytest.mark.parametrize("fault", ["cas", "after"])
def test_known_429_checkpoint_failure_cannot_start_retry(monkeypatch, clock, fault):
    p, store = plan(), FakeObjectStore()
    store.put_fault = (2, fault)
    session = wire_collector(monkeypatch, store, p, clock, [(429, "0", "synthetic")])
    with pytest.raises(RuntimeError, match="pair_checkpoint_callback_failed"):
        daily.acquire_cohort(store, p, 0)
    assert len(session.calls) == 1
    checkpoint = saved(store, p)
    if fault == "cas":
        assert checkpoint.journal.entries[0].state == "uncertain"
        assert checkpoint.retry_states == ()
        with pytest.raises(ValueError, match="uncertain"):
            daily.acquire_cohort(store, p, 0)
    else:
        assert checkpoint.journal.entries[0].reason == "rate_limited"
        assert checkpoint.retry_for("monex", "7203").retries_used == 0
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "fault",
    [
        "old_format",
        "duplicate",
        "extra_field",
        "outside_pair",
        "unknown_source",
        "bool_count",
        "negative_count",
        "too_many",
        "naive_deadline",
        "offset_deadline",
        "bad_deadline_type",
        "bad_overflow_type",
        "deadline_and_overflow",
        "missing_pending_deadline",
        "exhausted_deadline",
        "excess_http",
        "retry_for_unattempted",
    ],
)
def test_retry_checkpoint_strict_shape_time_quota_and_state_alignment(fault):
    p = plan()
    data = json.loads(staging.checkpoint_bytes(p, known_429(p)))
    retry = data["retry_states"][0]
    if fault == "old_format":
        data["format"] = "retail-private-checkpoint-v1"
    elif fault == "duplicate":
        data["retry_states"].append(dict(retry))
    elif fault == "extra_field":
        retry["raw_header"] = "synthetic forbidden"
    elif fault == "outside_pair":
        retry["ticker"] = "9432"
    elif fault == "unknown_source":
        retry["source"] = "unknown"
    elif fault in {"bool_count", "negative_count", "too_many"}:
        retry["retries_used"] = {"bool_count": True, "negative_count": -1, "too_many": 6}[fault]
    elif fault == "naive_deadline":
        retry["next_retry_utc"] = "2026-10-11T00:00:10"
    elif fault == "offset_deadline":
        retry["next_retry_utc"] = "2026-10-11T00:00:10+01:00"
    elif fault == "bad_deadline_type":
        retry["next_retry_utc"] = 1
    elif fault == "bad_overflow_type":
        retry["retry_after_unrepresentable"] = 1
    elif fault == "deadline_and_overflow":
        retry["retry_after_unrepresentable"] = True
    elif fault == "missing_pending_deadline":
        retry["next_retry_utc"] = None
    elif fault == "exhausted_deadline":
        retry["retries_used"] = 5
    elif fault == "excess_http":
        data["requests_started"] = 2
    else:
        data["journal"] = json.loads(
            staging.checkpoint_bytes(p, staging.Checkpoint(new_journal(p, 0)))
        )["journal"]
        retry["retries_used"], retry["next_retry_utc"] = 1, None
    with pytest.raises(ValueError, match=r"checkpoint|retry|UTC|budget"):
        staging.load_checkpoint(p, json.dumps(data).encode())


def test_old_binding_and_duplicate_json_cannot_enable_retry(monkeypatch, clock):
    p, store = plan(), FakeObjectStore()
    old = replace(p, collector_binding="synthetic-v1-binding")
    seed_checkpoint(store, old, known_429(old))
    session = wire_collector(monkeypatch, store, old, clock, [])
    with pytest.raises(ValueError, match="collector binding changed"):
        daily.acquire_cohort(store, old, 0)
    payload = staging.checkpoint_bytes(p, known_429(p)).decode()
    duplicate = payload.replace('"format":', '"format":"discarded duplicate","format":', 1)
    with pytest.raises(ValueError, match="duplicate"):
        staging.load_checkpoint(p, duplicate.encode())
    assert session.calls == []
