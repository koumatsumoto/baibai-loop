from __future__ import annotations

import base64
import hashlib
import json
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from tests.engine.test_external_assessment_daily_verification import ASOF, build_master_lake
from tests.engine.test_external_assessment_sources import monex_html
from tests.helpers.r2_store import MemoryR2Store
from tests.jobs.external_assessment_response_fixtures import response

import baibai_jobs.acquisition.external_assessments.daily as daily
import baibai_jobs.acquisition.external_assessments.staging as staging
import baibai_jobs.integration.external_assessment_raw as external_assessment_raw
from baibai_engine.machine_analysis.observations.external_assessments.models import (
    ModelValuationObservation as ExternalAssessmentModelValuationObservation,
)
from baibai_engine.machine_analysis.observations.external_assessments.normalization import monex
from baibai_engine.storage.objects.r2 import (
    LakeCASConflict,
    LakePublishError,
    RemoteObject,
)
from baibai_engine.storage.snapshots.keys import current_release_pointer_key
from baibai_engine.storage.snapshots.objects import LakeObjectError
from baibai_jobs.acquisition.external_assessments.cohorts import (
    make_plan,
    new_result,
    record_outcome,
)
from baibai_jobs.acquisition.external_assessments.collection import (
    CollectionOutcome as ExternalAssessmentCollectionOutcome,
)
from baibai_jobs.acquisition.external_assessments.collection import (
    CollectionResult as ExternalAssessmentCollectionResult,
)
from baibai_jobs.acquisition.external_assessments.collection import (
    collector_binding as external_assessment_collector_binding,
)

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
        collector_binding=external_assessment_collector_binding(
            source=sources[0] if len(sources) == 1 else None
        ),
        **kwargs,
    )


def observation(ticker="7203", *, missing=False):
    return ExternalAssessmentModelValuationObservation.model_validate(
        {
            "ticker": ticker,
            "fetched_at_utc": RECEIVED,
            "distributor": "monex",
            "source_url": "https://synthetic.invalid/source",
            "parser_version": "synthetic-1",
            "pbr": None if missing else 0,
        }
    )


def stage(store, p, index=0):
    return staging.read_stage(store, p, index)[0]


def seed_stage(store, p, result, *, http_requests=0):
    value = staging.CohortStage(result, http_requests)
    store.seed(staging.stage_key(p, result.cohort_index), staging.stage_bytes(p, value))


def completed_pair(p, journal, ticker, status):
    active = journal
    attempt_id = str(uuid.uuid4())
    if status == "parse_failure":
        return record_outcome(
            p,
            active,
            ExternalAssessmentCollectionOutcome(
                "monex", ticker, status, "missing_theory_table", 200, attempt_id
            ),
        )
    return record_outcome(
        p,
        active,
        ExternalAssessmentCollectionOutcome(
            "monex", ticker, "observed", http_status=200, attempt_id=attempt_id
        ),
        observation(ticker, missing=status == "normal_null"),
    )


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
    with pytest.raises(LakePublishError, match="private external assessment object"):
        staging.read_private(store, "private/key")


def test_saved_plan_master_assertion_is_checked_without_refreezing(monkeypatch):
    p = plan()
    store = FakeObjectStore()
    staging.save_new_plan(store, p)

    def unexpected_master(*args, **kwargs):
        raise AssertionError("existing plan must retain its frozen universe")

    monkeypatch.setattr(daily, "external_assessment_frozen_lake_universe", unexpected_master)
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
    store.store_object(current_release_pointer_key(), b"unreadable synthetic later pointer")
    store.get_keys.clear()
    store.head_keys.clear()
    resumed = daily.initialize(store, run_id=p.run_id, master_asof=ASOF, mirror=None)
    assert resumed == p
    assert current_release_pointer_key() not in store.head_keys
    assert all(key == staging.prefix(p.run_id) + "/plan.json" for key in store.get_keys)


def test_new_plan_requires_cache_and_existing_release_before_master_or_plan_writes(tmp_path):
    store = MemoryR2Store()
    with pytest.raises(ValueError, match="master cache directory"):
        daily.initialize(store, run_id="missing-cache", master_asof=ASOF, mirror=None)
    with pytest.raises(ValueError, match="existing lake release"):
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


def test_published_terminal_suppresses_refetch_when_checkpoint_payloads_are_absent(monkeypatch):
    p = plan()
    store = FakeObjectStore()
    terminal = {
        "format": "external-assessment-published-batch-v1",
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

    monkeypatch.setattr(daily, "external_assessment_collect_pair", forbidden_transport)
    report = daily.acquire_cohort(store, p, 0)
    assert report["status"] == "already_published"
    assert report["readback"] == "previously_verified"
    assert report["http_requests"] == report["provider_requests"] == 0
    assert staging.read_stages(store, p) == {}
    assert store.puts == []
    terminal["plan_sha256"] = "b" * 64
    store.seed(staging.prefix(p.run_id) + "/published.json", json.dumps(terminal).encode())
    with pytest.raises(ValueError, match="invalid external assessment published batch"):
        daily.acquire_cohort(store, p, 0)


def test_old_binding_plan_with_pending_acquisition_is_rejected(monkeypatch):
    import baibai_jobs.acquisition.external_assessments.sources as sources

    p = replace(plan(), run_id="monex-old-binding", collector_binding="previous-collector")
    store = FakeObjectStore()
    staging.save_new_plan(store, p)
    store.events.clear()
    monkeypatch.setattr(sources, "Boto3R2Store", lambda **kwargs: store)
    assert (
        sources.main(
            [
                "acquire",
                "--source",
                "monex",
                "--run-id",
                p.run_id,
                "--master-asof",
                ASOF.isoformat(),
                "--directory",
                "/tmp/source-old-binding",
            ]
        )
        == 1
    )
    assert not any(operation == "PUT" for operation, _ in store.events)


def wire_success(monkeypatch, store, p, *, after=None, raw=True):
    calls = []

    def collect(ticker, *, source, on_received_html, **kwargs):
        # Nothing is committed between HTTP calls in an ordinary cohort.
        calls.append(ticker)
        attempt_id = str(uuid.uuid4())
        if raw:
            on_received_html(
                response(source, ticker, b"public", attempt_id=attempt_id, received_at_utc=RECEIVED)
            )
        if after:
            after(ticker)
        return ExternalAssessmentCollectionResult(
            (observation(ticker),),
            (
                ExternalAssessmentCollectionOutcome(
                    source, ticker, "observed", http_status=200, attempt_id=attempt_id
                ),
            ),
            1,
            response_content_bytes=6 if raw else 0,
        )

    monkeypatch.setattr(daily, "external_assessment_collect_pair", collect)
    return calls


def test_normal_fifty_pair_cohort_uploads_one_raw_bundle_then_stage(monkeypatch):
    p = plan(tickers=tuple(str(1000 + i) for i in range(50)))
    store = FakeObjectStore()
    calls = wire_success(monkeypatch, store, p, raw=True)
    report = daily.acquire_cohort(store, p, 0)
    assert len(calls) == report["provider_requests"] == 50
    assert report["cohort_complete"] is True
    assert len(store.puts) == 2
    assert store.puts[1][0] == staging.stage_key(p, 0)
    assert all(event[0] != "GET" for event in store.events)
    saved = stage(store, p)
    assert saved.http_requests == 50
    assert saved.raw_bundle_keys == (store.puts[0][0],)
    before = list(store.puts)
    report = daily.acquire_cohort(store, p, 0)
    assert report["provider_requests"] == 0
    assert len(calls) == 50
    assert store.puts == before


def test_acquisition_reads_only_its_own_cohort(monkeypatch):
    p = plan(tickers=("6758", "7203"), cohort_size=1)
    store = FakeObjectStore()
    seed_stage(store, p, new_result(p, 1))
    calls = wire_success(monkeypatch, store, p)
    store.events.clear()
    assert daily.acquire_cohort(store, p, 0)["cohort_complete"] is True
    assert calls == ["6758"]
    assert not any(key == staging.stage_key(p, 1) for _, key in store.events)
    assert store.events.count(("HEAD", staging.stage_key(p, 0))) == 1
    assert store.events.count(("PUT", staging.stage_key(p, 0))) == 1


def test_controlled_partial_commit_preserves_receipt_and_failure_then_resumes_missing(monkeypatch):
    p = plan(tickers=("6758", "7203", "9432"))
    store = FakeObjectStore()
    timer = [0.0]
    monkeypatch.setattr(daily.time, "monotonic", lambda: timer[0])
    seen = []

    def collect(ticker, *, source, **kwargs):
        seen.append(ticker)
        attempt_id = str(uuid.uuid4())
        kwargs["on_received_html"](
            response(
                source,
                ticker,
                b"invalid" if ticker == "7203" else b"public",
                attempt_id=attempt_id,
                received_at_utc=RECEIVED,
            )
        )
        if ticker == "7203":
            timer[0] = 101
            return ExternalAssessmentCollectionResult(
                (),
                (
                    ExternalAssessmentCollectionOutcome(
                        source,
                        ticker,
                        "parse_failure",
                        "missing_theory_table",
                        200,
                        attempt_id,
                    ),
                ),
                1,
            )
        return ExternalAssessmentCollectionResult(
            (observation(ticker),),
            (
                ExternalAssessmentCollectionOutcome(
                    source, ticker, "observed", http_status=200, attempt_id=attempt_id
                ),
            ),
            1,
        )

    monkeypatch.setattr(daily, "external_assessment_collect_pair", collect)
    first = daily.acquire_cohort(store, p, 0, soft_budget_seconds=1000)
    assert first["stop_reason"] == "soft_budget"
    assert first["cohort_complete"] is False
    partial = stage(store, p)
    assert [entry.state for entry in partial.result.entries] == [
        "success",
        "failure",
        "unattempted",
    ]
    assert len(partial.raw_bundle_keys) == 1
    assert len(store.puts) == 2
    timer[0] = 0
    daily.acquire_cohort(store, p, 0)
    complete = stage(store, p)
    assert seen == ["6758", "7203", "9432"]
    assert complete.result.entries[:2] == partial.result.entries[:2]
    assert complete.raw_bundle_keys[:1] == partial.raw_bundle_keys
    assert len(complete.raw_bundle_keys) == 2
    assert complete.result.observations[0].fetched_at_utc == RECEIVED
    assert len(store.puts) == 4


def test_empty_soft_stop_does_not_write_a_stage(monkeypatch):
    store, p = FakeObjectStore(), plan()
    monkeypatch.setattr(
        daily, "external_assessment_collect_pair", lambda *a, **k: pytest.fail("budget")
    )
    report = daily.acquire_cohort(store, p, 0, soft_budget_seconds=1)
    assert report["cohort_complete"] is False
    assert report["stop_reason"] == "soft_budget"
    assert store.puts == []


def test_hard_kill_before_commit_allows_reget_without_durable_reservation(monkeypatch):
    p = plan(tickers=("6758", "7203"))
    store = FakeObjectStore()
    seen = []

    def collect(ticker, *, source, **kwargs):
        seen.append(ticker)
        assert store.puts == []
        if ticker == "7203":
            raise KeyboardInterrupt
        attempt_id = str(uuid.uuid4())
        kwargs["on_received_html"](
            response(source, ticker, b"public", attempt_id=attempt_id, received_at_utc=RECEIVED)
        )
        return ExternalAssessmentCollectionResult(
            (observation(ticker),),
            (
                ExternalAssessmentCollectionOutcome(
                    source, ticker, "observed", http_status=200, attempt_id=attempt_id
                ),
            ),
            1,
        )

    monkeypatch.setattr(daily, "external_assessment_collect_pair", collect)
    with pytest.raises(KeyboardInterrupt):
        daily.acquire_cohort(store, p, 0)
    assert store.puts == []
    calls = wire_success(monkeypatch, store, p)
    daily.acquire_cohort(store, p, 0)
    assert calls == ["6758", "7203"]
    assert stage(store, p).result.complete


@pytest.mark.parametrize("fault", ["after", "bad_receipt"])
def test_ambiguous_stage_put_adopts_actual_saved_result(monkeypatch, fault):
    p, store = plan(), FakeObjectStore()
    wire_success(monkeypatch, store, p)
    store.put_fault = (2, fault)
    report = daily.acquire_cohort(store, p, 0)
    assert report["cohort_complete"] is True
    assert len(store.puts) == 2
    assert stage(store, p).result.observations == (observation(),)


@pytest.mark.parametrize("fault", ["before", "cas"])
def test_unsaved_stage_failure_is_visible_after_durable_raw_upload(monkeypatch, fault):
    p, store = plan(), FakeObjectStore()
    wire_success(monkeypatch, store, p, raw=True)
    store.put_fault = (2, fault)
    with pytest.raises(LakePublishError):
        daily.acquire_cohort(store, p, 0)
    assert len(store.puts) == 2
    assert store.puts[0][0].startswith("external-assessment/raw/")
    assert staging.read_stage(store, p, 0) is None


def test_competing_saved_timestamp_wins_without_overwrite_or_wrong_raw(monkeypatch):
    p, store = plan(), FakeObjectStore()
    original_put = store.put_file
    winner_response = response(
        "monex",
        "7203",
        monex_html().encode(),
        url=monex.public_url("7203"),
        received_at_utc=RECEIVED.replace(second=2),
    )
    winner_obs = monex.parse_html(
        winner_response.body.decode(),
        ticker="7203",
        source_url=winner_response.requested_url,
        fetched_at_utc=winner_response.received_at_utc,
    )
    with external_assessment_raw.RawBundle(p.run_id, 0) as bundle:
        bundle.append(winner_response)
        winner_key = bundle.finish()
        bundle.upload(store)
    assert winner_key is not None
    winner = staging.CohortStage(
        record_outcome(
            p,
            new_result(p, 0),
            ExternalAssessmentCollectionOutcome(
                "monex",
                "7203",
                "observed",
                http_status=200,
                attempt_id=winner_response.attempt_id,
            ),
            winner_obs,
        ),
        raw_bundle_keys=(winner_key,),
    )

    def conflict(key, path, **kwargs):
        if key == staging.stage_key(p, 0):
            store.seed(key, staging.stage_bytes(p, winner))
        return original_put(key, path, **kwargs)

    monkeypatch.setattr(store, "put_file", conflict)
    wire_success(monkeypatch, store, p, raw=True)
    assert daily.acquire_cohort(store, p, 0)["cohort_complete"] is True
    saved = stage(store, p)
    losing_key = store.puts[1][0]
    assert losing_key != winner_key
    assert saved == winner
    assert saved.result.entries[0].attempt_id == winner_response.attempt_id
    assert saved.result.observations == (winner_obs,)
    assert saved.raw_bundle_keys == (winner_key,)
    assert losing_key not in saved.raw_bundle_keys
    _, retained = external_assessment_raw.read_raw_bundle(store, winner_key)
    _, losing = external_assessment_raw.read_raw_bundle(store, losing_key)
    assert retained == (winner_response,)
    assert losing[0].attempt_id != winner_response.attempt_id
    assert store.puts[2][0] == staging.stage_key(p, 0)
    assert len(store.puts) == 3


def test_partial_stage_conflict_keeps_saved_failure_and_reports_missing(monkeypatch):
    p, store = plan(tickers=("6758", "7203")), FakeObjectStore()
    previous = completed_pair(p, new_result(p, 0), "6758", "parse_failure")
    seed_stage(store, p, previous)
    store.put_fault = (2, "cas")
    calls = wire_success(monkeypatch, store, p)
    report = daily.acquire_cohort(store, p, 0)
    assert calls == ["7203"]
    assert report["cohort_complete"] is False
    assert stage(store, p).result == previous
    assert len(store.puts) == 2


def test_stage_commit_cannot_change_saved_outcomes_or_timestamps():
    p, store = plan(), FakeObjectStore()
    saved = staging.CohortStage(completed_pair(p, new_result(p, 0), "7203", "observed"))
    store.seed(staging.stage_key(p, 0), staging.stage_bytes(p, saved))
    previous = staging.read_stage(store, p, 0)
    changed = staging.CohortStage(completed_pair(p, new_result(p, 0), "7203", "parse_failure"))
    with pytest.raises(ValueError, match="cannot change"):
        staging.commit_stage(store, p, changed, previous=previous)
    assert store.puts == []


@pytest.mark.parametrize(
    "fault", ["old_format", "duplicate_field", "wrong_binding", "negative_counter", "unknown_state"]
)
def test_stage_rejects_old_checkpoint_and_corrupt_payload(fault):
    p = plan()
    value = json.loads(staging.stage_bytes(p, staging.CohortStage(new_result(p, 0))))
    if fault == "old_format":
        value["format"] = "external-assessment-cohort-stage-v1"
    elif fault == "wrong_binding":
        value["result"]["plan_sha256"] = "different"
    elif fault == "negative_counter":
        value["http_requests"] = -1
    elif fault == "unknown_state":
        value["result"]["entries"][0]["state"] = "uncertain"
    payload = json.dumps(value).encode()
    if fault == "duplicate_field":
        payload = payload.replace(b'"http_requests": 0', b'"http_requests": 0, "http_requests": 1')
    with pytest.raises(ValueError, match=r"stage|binding|source or state"):
        staging.load_stage(p, payload)
