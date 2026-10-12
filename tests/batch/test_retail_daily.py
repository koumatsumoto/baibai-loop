from __future__ import annotations

import base64
import hashlib
import json
import sys
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
import requests
from tests.engine.test_retail_dynamics_daily_verification import ASOF, build_master_lake
from tests.engine.test_retail_dynamics_sources import official_html, rakuten_html
from tests.helpers.r2_store import MemoryR2Store

from baibai_batch.jobs import retail_daily as daily
from baibai_batch.jobs.retail_cohorts import (
    make_plan,
    mark_inflight,
    new_journal,
    record_outcome,
    summarize,
)
from baibai_batch.storage import retail_staging as staging
from baibai_batch.storage.lake_publish import LakeCASConflict, LakePublishError, RemoteObject
from baibai_engine.batch_api import (
    RetailCollectionOutcome,
    RetailCollectionResult,
    RetailModelValuationObservation,
    RetailReceivedHTML,
    retail_collect_pair,
    retail_collector_binding,
)
from baibai_engine.market.lake.keys import current_l1_pointer_key
from baibai_engine.market.lake.objects import LakeObjectError

RECEIVED = datetime(2026, 10, 11, 0, 0, 1, 123456, tzinfo=UTC)


class FakeObjectStore:
    def __init__(self):
        self.objects = {}
        self.events = []
        self.puts = []
        self.serial = 0
        self.put_fault = None
        self.read_fault = None

    def seed(self, key, payload, content_type="application/json"):
        self.serial += 1
        metadata = {"sha256": hashlib.sha256(payload).hexdigest()}
        self.objects[key] = (
            payload,
            RemoteObject(str(self.serial), len(payload), metadata, content_type),
        )

    def head(self, key):
        self.events.append(("HEAD", key))
        item = self.objects.get(key)
        return item[1] if item else None

    def get_bytes(self, key):
        self.events.append(("GET", key))
        body = self.objects[key][0]
        if self.read_fault == "changed":
            self.read_fault = None
            self.seed(key, body)
        elif self.read_fault == "deleted":
            self.read_fault = None
            del self.objects[key]
        elif self.read_fault == "truncated":
            self.read_fault = None
            return body[:-1]
        return body

    def put_file(
        self, key, path, *, sha256, content_md5, content_type, if_match=None, if_none_match=False
    ):
        body = path.read_bytes()
        assert path.stat().st_mode & 0o777 == 0o600
        assert sha256 == hashlib.sha256(body).hexdigest()
        assert (
            content_md5
            == base64.b64encode(hashlib.md5(body, usedforsecurity=False).digest()).decode()
        )
        assert content_type in {"application/json", "application/gzip"}
        assert (if_match is not None) != if_none_match
        self.events.append(("PUT", key))
        self.puts.append((key, if_match, if_none_match, body))
        fault = None
        if self.put_fault and self.put_fault[0] == len(self.puts):
            fault = self.put_fault[1]
        existing = self.objects.get(key)
        if (
            fault == "cas"
            or (if_none_match and existing is not None)
            or (if_match is not None and (existing is None or if_match != existing[1].etag))
        ):
            raise LakeCASConflict("synthetic stale conditional write")
        if fault == "before":
            raise LakePublishError("synthetic unavailable write")
        self.seed(key, body, content_type)
        if fault == "after":
            raise LakePublishError("synthetic lost write response")
        remote = self.objects[key][1]
        if fault == "bad_receipt":
            return replace(remote, metadata={"sha256": "wrong"})
        return remote


def plan(*, tickers=("7203",), sources=("monex",), cohort_size=50, **kwargs):
    return make_plan(
        run_id="synthetic-20261011",
        tickers=tickers,
        sources=sources,
        cohort_size=cohort_size,
        universe_binding=json.dumps({"master_asof": "2026-10-11", "sha256": "synthetic-master"}),
        collector_binding=retail_collector_binding(),
        **kwargs,
    )


def observation(ticker="7203", *, missing=False):
    return RetailModelValuationObservation.model_validate(
        {
            "ticker": ticker,
            "fetched_at_utc": RECEIVED,
            "distributor": "monex",
            "source_url": "https://synthetic.invalid/source",
            "parser_version": "synthetic-1",
            "pbr": None if missing else 0,
        }
    )


def checkpoint(store, p, index=0):
    return staging.read_checkpoints(store, p)[index][0]


def seed_checkpoint(store, p, journal, *, reservations=0, confirmed=0):
    value = staging.Checkpoint(journal, reservations, confirmed)
    store.seed(staging.checkpoint_key(p, journal.cohort_index), staging.checkpoint_bytes(p, value))


def completed_pair(p, journal, ticker, status):
    active = mark_inflight(p, journal, "monex", ticker)
    if status == "parse_failure":
        return record_outcome(
            p, active, RetailCollectionOutcome("monex", ticker, status, "missing_theory_table", 200)
        )
    return record_outcome(
        p,
        active,
        RetailCollectionOutcome("monex", ticker, "observed", http_status=200),
        observation(ticker, missing=status == "normal_null"),
    )


@pytest.mark.parametrize(
    ("status", "exit_code"),
    [("observed", 0), ("parse_failure", 0), ("access_denied", 1), ("incomplete", 1)],
)
def test_acquire_cli_uses_own_durable_outcome_without_a_second_checkpoint_scan(
    monkeypatch, capsys, status, exit_code
):
    p = plan(tickers=("7203", "9432"), cohort_size=1)
    store = FakeObjectStore()
    staging.save_new_plan(store, p)
    seed_checkpoint(store, p, new_journal(p, 1))
    transports = []

    def collect(ticker, *, source, before_request, on_outcome, **kwargs):
        before_request()
        transports.append(ticker)
        result = RetailCollectionResult(
            (observation(ticker),) if status == "observed" else (),
            (
                RetailCollectionOutcome(
                    source,
                    ticker,
                    status,
                    {"parse_failure": "missing_theory_table", "incomplete": "wall_clock_limit"}.get(
                        status
                    ),
                    403 if status == "access_denied" else 200,
                ),
            ),
            1,
            "uncertain_inflight" if status == "incomplete" else None,
        )
        if status != "incomplete":
            on_outcome(result)
        return result

    monkeypatch.setattr(daily, "Boto3R2Store", lambda **kwargs: store)
    monkeypatch.setattr(daily, "retail_collect_pair", collect)
    store.events.clear()
    args = ["acquire", "--run-id", p.run_id, "--cohort", "0"]
    assert daily.main(args) == exit_code
    report = json.loads(capsys.readouterr().out)
    assert report["cohort_complete"] is (exit_code == 0)
    # Other cohorts remain pending even when this one is complete.
    assert report["state_counts"]["unattempted"] >= 1
    other_key = staging.checkpoint_key(p, 1)
    assert store.events.count(("HEAD", other_key)) == 2
    assert store.events.count(("GET", other_key)) == 1
    own_key = staging.checkpoint_key(p, 0)
    assert store.events.count(("HEAD", own_key)) == 1
    assert store.events.count(("GET", own_key)) == 0
    assert transports == ["7203"]
    if exit_code == 0:
        previous = store.objects[own_key]
        puts_before = len(store.puts)
        assert daily.main(args) == 0
        assert transports == ["7203"]
        assert len(store.puts) == puts_before
        assert store.objects[own_key] == previous


def test_cohort_timing_separates_collection_from_checkpoint_and_raw_storage(monkeypatch):
    p = plan()
    store = FakeObjectStore()
    clock = [0.0]
    monkeypatch.setattr(daily.time, "monotonic", lambda: clock[0])

    def timed(operation, seconds):
        def run(*args, **kwargs):
            clock[0] += seconds
            return operation(*args, **kwargs)

        return run

    monkeypatch.setattr(store, "head", timed(store.head, 0.25))
    monkeypatch.setattr(store, "get_bytes", timed(store.get_bytes, 0.5))
    monkeypatch.setattr(store, "put_file", timed(store.put_file, 1.0))

    def collect(ticker, *, source, before_request, on_outcome, on_received_html, **kwargs):
        before_request()
        clock[0] += 3.0  # HTTP, pacing and parsing, without checkpoint storage.
        on_received_html(
            RetailReceivedHTML("https://synthetic.invalid/source", RECEIVED, "utf-8", b"public")
        )
        result = RetailCollectionResult(
            (observation(ticker),),
            (RetailCollectionOutcome(source, ticker, "observed", http_status=200),),
            1,
        )
        on_outcome(result)
        return result

    monkeypatch.setattr(daily, "retail_collect_pair", collect)
    report = daily.acquire_cohort(store, p, 0)
    assert report["cohort_complete"] is True
    assert report["collection_elapsed_seconds"] == 3.0
    # Two initial HEADs, two durable checkpoint PUTs, and raw HEAD/PUT pairs.
    assert report["storage_elapsed_seconds"] == 5.0
    assert report["elapsed_seconds"] == 8.0
    monkeypatch.setattr(
        daily, "retail_collect_pair", lambda *args, **kwargs: pytest.fail("already saved")
    )
    resumed = daily.acquire_cohort(store, p, 0)
    assert resumed["collection_elapsed_seconds"] == 0.0
    assert resumed["storage_elapsed_seconds"] == resumed["elapsed_seconds"] == 1.25


def test_private_put_requires_generation_and_verifies_hashes_and_private_mode():
    store = FakeObjectStore()
    first = staging.put_private(store, "private/key", b'{"count":1}', previous_etag=None)
    second = staging.put_private(store, "private/key", b'{"count":2}', previous_etag=first.etag)
    assert [(match, create) for _, match, create, _ in store.puts] == [
        (None, True),
        (first.etag, False),
    ]
    assert second.etag != first.etag
    assert staging.read_private(store, "private/key") == second
    assert store.events[-3:] == [
        ("HEAD", "private/key"),
        ("GET", "private/key"),
        ("HEAD", "private/key"),
    ]
    with pytest.raises(LakeCASConflict, match="stale conditional"):
        staging.put_private(store, "private/key", b"different", previous_etag=first.etag)
    assert store.objects["private/key"][0] == second.payload


@pytest.mark.parametrize(
    "fault", ["changed", "deleted", "truncated", "wrong_sha", "empty_etag", "oversize"]
)
def test_private_read_refuses_racing_or_invalid_objects(fault):
    store = FakeObjectStore()
    store.seed("private/key", b'{"count":1}')
    if fault in {"wrong_sha", "empty_etag", "oversize"}:
        body, remote = store.objects["private/key"]
        if fault == "wrong_sha":
            remote = replace(remote, metadata={"sha256": "wrong"})
        elif fault == "empty_etag":
            remote = replace(remote, etag="")
        else:
            remote = replace(remote, size=staging.MAX_PRIVATE_OBJECT_BYTES + 1)
        store.objects["private/key"] = body, remote
    else:
        store.read_fault = fault
    with pytest.raises(LakePublishError, match="private retail object"):
        staging.read_private(store, "private/key")


@pytest.mark.parametrize("fault", ["cas", "before", "after", "bad_receipt"])
def test_inflight_checkpoint_failure_cannot_reach_transport(monkeypatch, fault):
    p = plan()
    store = FakeObjectStore()
    store.put_fault = 1, fault
    transports = []

    def collect(ticker, *, source, before_request, **kwargs):
        before_request()
        transports.append((source, ticker))
        raise AssertionError("checkpoint failure must abort before transport")

    monkeypatch.setattr(daily, "retail_collect_pair", collect)
    with pytest.raises(LakePublishError, match=r"synthetic|receipt mismatch"):
        daily.acquire_cohort(store, p, 0)
    assert transports == []
    if fault in {"after", "bad_receipt"}:
        saved = checkpoint(store, p)
        assert saved.requests_started == 1
        assert saved.confirmed_http_requests == 0
        assert saved.journal.halt_reason == "inflight_uncertain"
        with pytest.raises(ValueError, match="daily run is halted"):
            daily.acquire_cohort(store, p, 0)
        assert transports == []


@pytest.mark.parametrize("fault", ["cas", "after"])
def test_outcome_checkpoint_failure_stops_before_next_pair(monkeypatch, fault):
    p = plan(tickers=("7203", "9432"))
    store = FakeObjectStore()
    store.put_fault = 2, fault
    transports = []

    def collect(ticker, *, source, before_request, on_outcome, on_response, **kwargs):
        before_request()
        transports.append(ticker)
        on_response(RECEIVED, 1, 12)
        result = RetailCollectionResult(
            (observation(ticker),),
            (RetailCollectionOutcome(source, ticker, "observed", http_status=200),),
            1,
        )
        on_outcome(result)
        return result

    monkeypatch.setattr(daily, "retail_collect_pair", collect)
    with pytest.raises(LakePublishError, match="synthetic"):
        daily.acquire_cohort(store, p, 0)
    assert transports == ["7203"]
    saved = checkpoint(store, p)
    if fault == "cas":
        assert saved.journal.entries[0].state == "uncertain"
    else:
        assert saved.journal.entries[0].state == "success"
        assert saved.journal.observations == (observation(),)
    assert saved.journal.entries[1].state == "unattempted"


def test_confirmed_success_normal_null_and_parse_failure_resume_without_http(monkeypatch):
    p = plan(tickers=("130A", "7203", "9432"))
    store = FakeObjectStore()
    journal = new_journal(p, 0)
    for ticker, status in zip(p.tickers, ("success", "normal_null", "parse_failure"), strict=True):
        journal = completed_pair(p, journal, ticker, status)
    seed_checkpoint(store, p, journal, reservations=3, confirmed=3)
    original_payload = store.objects[staging.checkpoint_key(p, 0)][0]

    def unexpected_http(*args, **kwargs):
        raise AssertionError("confirmed pairs must not be acquired again")

    monkeypatch.setattr(daily, "retail_collect_pair", unexpected_http)
    report = daily.acquire_cohort(store, p, 0)
    assert report["state_counts"] == {
        "failure": 1,
        "normal_null": 1,
        "success": 1,
        "unattempted": 0,
        "uncertain": 0,
    }
    assert report["confirmed_http_requests"] == 3
    assert store.puts == []
    assert store.objects[staging.checkpoint_key(p, 0)][0] == original_payload
    assert checkpoint(store, p).journal.observations == journal.observations
    assert all(
        item.fetched_at_utc == RECEIVED for item in checkpoint(store, p).journal.observations
    )


@pytest.mark.parametrize("blocked", ["uncertain", "access_denied", "rate_limited"])
def test_halt_in_another_cohort_blocks_all_new_http(monkeypatch, blocked):
    p = plan(tickers=("7203", "9432"), cohort_size=1)
    store = FakeObjectStore()
    journal = mark_inflight(p, new_journal(p, 0), "monex", "7203")
    if blocked != "uncertain":
        status = 403 if blocked == "access_denied" else 429
        journal = record_outcome(
            p, journal, RetailCollectionOutcome("monex", "7203", blocked, http_status=status)
        )
    seed_checkpoint(store, p, journal, reservations=1, confirmed=int(blocked != "uncertain"))

    def unexpected_http(*args, **kwargs):
        raise AssertionError("global daily halt must precede collection")

    monkeypatch.setattr(daily, "retail_collect_pair", unexpected_http)
    with pytest.raises(ValueError, match="daily run is halted"):
        daily.acquire_cohort(store, p, 1)
    assert store.puts == []
    assert staging.checkpoint_key(p, 1) not in store.objects


def test_soft_budget_keeps_explicit_normal_unattempted_checkpoint(monkeypatch):
    p = plan()
    store = FakeObjectStore()

    def unexpected_http(*args, **kwargs):
        raise AssertionError("soft stop must precede collection")

    monkeypatch.setattr(daily, "retail_collect_pair", unexpected_http)
    report = daily.acquire_cohort(store, p, 0, soft_budget_seconds=1)
    assert report["stop_reason"] == "soft_budget"
    saved = checkpoint(store, p)
    assert saved.requests_started == saved.confirmed_http_requests == 0
    assert saved.journal.halt_reason is None
    assert saved.journal.entries[0].state == "unattempted"
    assert len(store.puts) == 1


def test_soft_stop_after_success_keeps_receipt_and_resumes_only_pending_pair(monkeypatch):
    p = plan(tickers=("7203", "9432"))
    store = FakeObjectStore()
    clock = [0.0]
    monkeypatch.setattr(daily.time, "monotonic", lambda: clock[0])
    transports = []

    def collect(ticker, *, source, before_request, on_outcome, on_response, **kwargs):
        before_request()
        transports.append(ticker)
        on_response(RECEIVED, 1, 12)
        result = RetailCollectionResult(
            (observation(ticker),),
            (RetailCollectionOutcome(source, ticker, "observed", http_status=200),),
            1,
        )
        on_outcome(result)
        clock[0] += 110
        return result

    monkeypatch.setattr(daily, "retail_collect_pair", collect)
    report = daily.acquire_cohort(store, p, 0, soft_budget_seconds=1000)
    assert report["stop_reason"] == "soft_budget"
    assert transports == ["7203"]
    first = checkpoint(store, p)
    assert first.journal.entries[0].state == "success"
    assert first.journal.entries[1].state == "unattempted"
    assert first.journal.halt_reason is None
    original = first.journal.observations[0]
    second = daily.acquire_cohort(store, p, 0)
    assert transports == ["7203", "9432"]
    assert second["success_completeness"] is True
    assert checkpoint(store, p).journal.observations[0] == original
    assert checkpoint(store, p).confirmed_http_requests == 2


def test_interrupted_request_retains_marker_and_blocks_resume(monkeypatch):
    p = plan(tickers=("7203", "9432"))
    store = FakeObjectStore()
    transports = []

    def collect(ticker, *, source, before_request, **kwargs):
        before_request()
        transports.append(ticker)
        return RetailCollectionResult(
            (),
            (RetailCollectionOutcome(source, ticker, "incomplete", "wall_clock_limit"),),
            1,
            "uncertain_inflight",
        )

    monkeypatch.setattr(daily, "retail_collect_pair", collect)
    report = daily.acquire_cohort(store, p, 0)
    assert report["stop_reason"] == "uncertain_inflight"
    assert transports == ["7203"]
    saved = checkpoint(store, p)
    assert saved.requests_started == 1
    assert saved.confirmed_http_requests == 0
    assert saved.journal.entries[0].state == "uncertain"
    assert saved.journal.entries[1].state == "unattempted"
    with pytest.raises(ValueError, match="daily run is halted"):
        daily.acquire_cohort(store, p, 0)
    assert transports == ["7203"]


def test_missing_checkpoints_keep_accounting_incomplete_and_wrong_cohort_fails_pre_http(
    monkeypatch,
):
    p = plan(tickers=("7203", "9432"), cohort_size=1)
    store = FakeObjectStore()
    seed_checkpoint(store, p, new_journal(p, 0))
    journals = [value[0].journal for value in staging.read_checkpoints(store, p).values()]
    assert summarize(p, journals)["whole_universe_accounting"] is False
    with pytest.raises(ValueError, match="missing cohort"):
        summarize(p, journals, require_all=True)
    wrong = staging.checkpoint_bytes(p, staging.Checkpoint(new_journal(p, 1)))
    store.seed(staging.checkpoint_key(p, 0), wrong)

    def unexpected_http(*args, **kwargs):
        raise AssertionError("cohort identity failure must precede collection")

    monkeypatch.setattr(daily, "retail_collect_pair", unexpected_http)
    with pytest.raises(ValueError, match="cohort identity mismatch"):
        daily.acquire_cohort(store, p, 1)


def test_wrong_collector_and_journal_bindings_fail_before_http(monkeypatch):
    p = plan()
    store = FakeObjectStore()
    wrong = replace(p, collector_binding="different-code")
    attempts = []
    monkeypatch.setattr(daily, "retail_collect_pair", lambda *args, **kwargs: attempts.append(args))
    with pytest.raises(ValueError, match="collector binding changed"):
        daily.acquire_cohort(store, wrong, 0)
    assert not any(operation == "PUT" for operation, _ in store.events)
    store.seed(
        staging.checkpoint_key(p, 0),
        staging.checkpoint_bytes(wrong, staging.Checkpoint(new_journal(wrong, 0))),
    )
    with pytest.raises(ValueError, match="journal binding differs"):
        daily.acquire_cohort(store, p, 0)
    assert attempts == []


def test_saved_plan_master_assertion_is_checked_without_refreezing(monkeypatch):
    p = plan()
    store = FakeObjectStore()
    staging.save_new_plan(store, p)

    def unexpected_master(*args, **kwargs):
        raise AssertionError("existing plan must retain its frozen universe")

    monkeypatch.setattr(daily, "retail_frozen_lake_universe", unexpected_master)
    assert (
        daily.initialize(store, run_id=p.run_id, master_asof=date(2026, 10, 11), mirror=None) == p
    )
    with pytest.raises(ValueError, match="master assertion changed"):
        daily.initialize(store, run_id=p.run_id, master_asof=date(2026, 10, 12), mirror=None)


def test_initial_plan_uses_only_exact_master_year_and_resume_keeps_its_release(
    tmp_path, monkeypatch
):
    store, release, _ = build_master_lake(tmp_path, monkeypatch)
    cache = tmp_path / "private-plan-cache"
    p = daily.initialize(store, run_id="new-master-only", master_asof=ASOF, mirror=cache)
    assert p.tickers == tuple(sorted((*map(str, range(1000, 3500)), "130A")))
    assert json.loads(p.universe_binding)["release_manifest_sha256"] == release.manifest_sha256
    assert cache.stat().st_mode & 0o777 == 0o700
    parquet_reads = [key for key in store.get_keys if key.endswith(".parquet")]
    assert parquet_reads
    assert all("jquants.master_snapshots/" in key for key in parquet_reads)
    assert all("/year=2026/" in key for key in parquet_reads)
    assert not list(cache.rglob("*.sqlite"))
    assert not list(cache.rglob("*.db"))

    # A later run may use the same verified cache, without transferring Parquet again.
    store.get_keys.clear()
    second = daily.initialize(store, run_id="other-batch", master_asof=ASOF, mirror=cache)
    assert second.tickers == p.tickers
    assert second.universe_binding == p.universe_binding
    assert not any(key.endswith(".parquet") for key in store.get_keys)

    # Even an unreadable current release cannot silently refreeze a saved batch.
    store.store_object(current_l1_pointer_key(), b"unreadable synthetic later pointer")
    store.get_keys.clear()
    store.head_keys.clear()
    resumed = daily.initialize(store, run_id=p.run_id, master_asof=ASOF, mirror=None)
    assert resumed == p
    assert current_l1_pointer_key() not in store.head_keys
    assert all(key == staging.prefix(p.run_id) + "/plan.json" for key in store.get_keys)


def test_new_plan_requires_cache_and_existing_release_before_master_or_plan_writes(tmp_path):
    store = MemoryR2Store()
    with pytest.raises(ValueError, match="master cache directory"):
        daily.initialize(store, run_id="missing-cache", master_asof=ASOF, mirror=None)
    with pytest.raises(ValueError, match="existing L1 release"):
        daily.initialize(
            store, run_id="missing-release", master_asof=ASOF, mirror=tmp_path / "cache"
        )
    assert store.get_keys == []
    assert store.put_keys == []


def test_old_saved_collector_binding_can_load_without_release_reads(monkeypatch):
    p = replace(plan(), collector_binding="different-code")
    store = FakeObjectStore()
    staging.save_new_plan(store, p)
    store.events.clear()
    assert daily.initialize(store, run_id=p.run_id, master_asof=ASOF, mirror=None) == p
    assert all(key == staging.prefix(p.run_id) + "/plan.json" for _, key in store.events)


def test_corrupt_digest_pinned_master_cannot_publish_a_new_plan(tmp_path, monkeypatch):
    store, _, _ = build_master_lake(tmp_path, monkeypatch)
    key = next(
        key
        for key in store.values
        if key.endswith(".parquet") and "jquants.master_snapshots/" in key and "/year=2026/" in key
    )
    original = store.values[key].body
    store.store_object(key, bytes([original[0] ^ 1]) + original[1:])
    with pytest.raises(LakeObjectError, match="digest differs"):
        daily.initialize(
            store, run_id="corrupt-master", master_asof=ASOF, mirror=tmp_path / "cache"
        )
    assert store.put_keys == []
    assert [key for key in store.get_keys if key.endswith(".parquet")] == [key]


def test_real_pair_collector_persists_two_rakuten_reservations_and_exact_receipt(monkeypatch):
    p = plan(sources=("rakuten",))
    store = FakeObjectStore()
    module = sys.modules[retail_collect_pair.__module__]
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        module.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )

    class FakeSession(requests.Session):
        def __init__(self):
            super().__init__()
            self.calls = []
            self.replies = iter((official_html(), rakuten_html(value="--")))

        def get(self, url, **kwargs):
            durable = checkpoint(store, p)
            self.calls.append((url, kwargs, durable))
            assert durable.journal.entries[0].state == "uncertain"
            assert durable.requests_started == len(self.calls)
            response = requests.Response()
            response.status_code = 200
            response._content = next(self.replies).encode()
            response._content_consumed = True
            response.headers["Content-Type"] = "text/html; charset=utf-8"
            return response

    session = FakeSession()

    def real_collect(ticker, **kwargs):
        return retail_collect_pair(ticker, **{**kwargs, "session": session})

    monkeypatch.setattr(daily, "retail_collect_pair", real_collect)
    report = daily.acquire_cohort(store, p, 0)
    saved = checkpoint(store, p)
    assert len(session.calls) == 2
    assert all(call[1]["allow_redirects"] is False for call in session.calls)
    assert report["request_reservations"] == report["confirmed_http_requests"] == 2
    assert saved.requests_started == saved.confirmed_http_requests == 2
    assert saved.response_content_bytes > 0
    assert saved.last_response_finished_at_utc is not None
    assert saved.journal.entries[0].state == "normal_null"
    assert len(saved.journal.observations) == 1
    first, second, final = [
        staging.load_checkpoint(p, item[3])
        for item in store.puts
        if item[0] == staging.checkpoint_key(p, 0)
    ]
    assert first.requests_started == 1
    assert second.requests_started == 2
    assert first.journal.entries[0] == second.journal.entries[0]
    original = saved.journal.observations[0]
    assert original == final.journal.observations[0]
    assert original.fetched_at_utc.utcoffset() == UTC.utcoffset(None)
    before = len(session.calls)
    daily.acquire_cohort(store, p, 0)
    assert len(session.calls) == before
    assert checkpoint(store, p).journal.observations == (original,)


@pytest.mark.parametrize("fault", ["cas", "after"])
def test_real_collector_aborts_before_transport_on_checkpoint_error(monkeypatch, fault):
    p = plan()
    store = FakeObjectStore()
    store.put_fault = 1, fault
    module = sys.modules[retail_collect_pair.__module__]
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        module.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )

    class FakeSession(requests.Session):
        def __init__(self):
            super().__init__()
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append(url)
            raise AssertionError("durable checkpoint error must precede transport")

    session = FakeSession()
    monkeypatch.setattr(
        daily,
        "retail_collect_pair",
        lambda ticker, **kwargs: retail_collect_pair(ticker, **{**kwargs, "session": session}),
    )
    with pytest.raises(RuntimeError, match="pair_checkpoint_callback_failed"):
        daily.acquire_cohort(store, p, 0)
    assert session.calls == []
    if fault == "after":
        assert checkpoint(store, p).journal.halt_reason == "inflight_uncertain"


def test_published_terminal_suppresses_refetch_when_checkpoint_payloads_are_absent(monkeypatch):
    p = plan()
    store = FakeObjectStore()
    terminal = {
        "format": "retail-published-batch-v1",
        "plan_sha256": p.plan_sha256,
        "release_id": "synthetic-published",
        "release_manifest_sha256": "a" * 64,
        "expected_pairs": 1,
        "observations": 1,
        "readback": "verified",
    }
    store.seed(staging.prefix(p.run_id) + "/published.json", json.dumps(terminal).encode())

    def forbidden_transport(*args, **kwargs):
        raise AssertionError("a published batch must never refetch its receipts")

    monkeypatch.setattr(daily, "retail_collect_pair", forbidden_transport)
    report = daily.acquire_cohort(store, p, 0)
    assert report["status"] == "already_published"
    assert report["readback"] == "previously_verified"
    assert report["confirmed_http_requests"] == report["request_reservations"] == 0
    assert staging.read_checkpoints(store, p) == {}
    assert store.puts == []
    terminal["plan_sha256"] = "b" * 64
    store.seed(staging.prefix(p.run_id) + "/published.json", json.dumps(terminal).encode())
    with pytest.raises(ValueError, match="invalid retail published batch"):
        daily.acquire_cohort(store, p, 0)


def test_old_binding_plan_with_pending_acquisition_is_rejected(monkeypatch):
    p = replace(plan(), collector_binding="previous-collector")
    store = FakeObjectStore()
    staging.save_new_plan(store, p)
    store.events.clear()
    monkeypatch.setattr(daily, "Boto3R2Store", lambda **kwargs: store)
    assert daily.main(["plan", "--run-id", p.run_id, "--master-asof", ASOF.isoformat()]) == 1
    assert not any(operation == "PUT" for operation, _ in store.events)
