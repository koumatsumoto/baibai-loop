"""既存の固定stageと試行hashを保ち、未計算は明示して渡す。"""

import hashlib
import json

from tests.helpers.r2_store import MemoryR2Store

from baibai_jobs.acquisition.external_assessments.attempts import (
    journal_prefix,
    read_attempts,
    save_attempts,
)
from baibai_jobs.acquisition.external_assessments.cohorts import make_plan
from baibai_jobs.acquisition.external_assessments.identity import STAGE_FORMAT
from baibai_jobs.acquisition.external_assessments.staging import (
    load_stage,
    prefix,
    save_new_plan,
    stage_bytes,
)
from baibai_jobs.integration.external_assessment_ready import (
    load_ready,
    prepare_saved_stages,
    save_ready,
)


def _plan():
    return make_plan(
        run_id="synthetic-kabumap-legacy",
        tickers=("7203",),
        sources=("kabumap",),
        universe_binding="synthetic-master",
        collector_binding="synthetic-collector-v3",
    )


def _legacy_stage(plan):
    # 旧契約を明示したfixture。新modelのdumpから期待bytesを作らない。
    return _bytes(
        {
            "format": STAGE_FORMAT,
            "http_requests": 1,
            "response_content_bytes": 123,
            "raw_bundle_keys": ["synthetic/retained-bundle.tar.gz"],
            "result": {
                "plan_sha256": plan.plan_sha256,
                "cohort_index": 0,
                "entries": [
                    {
                        "source": "kabumap",
                        "ticker": "7203",
                        "state": "success",
                        "reason": None,
                        "http_status": 200,
                        "attempt_id": "b8b7d231-3dc1-42c6-a87f-200a9fd9e682",
                        "observation": {
                            "ticker": "7203",
                            "fetched_at_utc": "2026-10-10T00:00:00Z",
                            "snapshot_date": "2026-10-10",
                            "distributor": "kabumap",
                            "origin": "Quants Research",
                            "source_url": "https://example.test/kabumap/7203",
                            "parser_version": "kabumap-quants-3",
                            "contract_version": "1",
                            "score_as_of_date": "2026-10-09",
                            "score_scale_max": 10,
                            "composite_scale_max": 100,
                            "growth_score": 2.0,
                            "undervaluation_score": 1.0,
                            "size_score": 3.0,
                            "technical_score": 4.0,
                            "financial_health_score": 5.0,
                            "market_trend_score": 6.0,
                            "composite_score": 42.0,
                            "industry_average_score": 52.0,
                        },
                    }
                ],
            },
        }
    )


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def test_legacy_stage_and_attempt_journal_keep_exact_canonical_bytes():
    plan, store = _plan(), MemoryR2Store()
    payload = _legacy_stage(plan)
    stage = load_stage(plan, payload)
    assert stage.result.observations[0].availability == "available"
    assert stage_bytes(plan, stage) == payload
    journal = _bytes(
        {
            "format": "external-assessment-attempt-journal-v1",
            "plan_sha256": plan.plan_sha256,
            "candidate_stage": json.loads(payload),
            "attempts": [],
        }
    )
    key = journal_prefix(plan) + hashlib.sha256(journal).hexdigest() + ".json"
    store.store_object(key, journal)
    assert read_attempts(store, plan)[0]["candidate_stage"] == json.loads(payload)
    before = list(store.put_keys)
    assert save_attempts(store, plan, stage, []) == key
    assert store.put_keys == before
    assert store.get_bytes(key) == journal


def test_legacy_ready_reference_is_idempotent_after_new_model_load():
    plan, store = _plan(), MemoryR2Store()
    save_new_plan(store, plan)
    payload = _legacy_stage(plan)
    stage = load_stage(plan, payload)
    digest = hashlib.sha256(payload).hexdigest()
    stage_key = prefix(plan.run_id) + f"/confirmed-stages/{digest}.json"
    store.store_object(stage_key, payload)
    identity_value = {
        "run_id": plan.run_id,
        "source": "kabumap",
        "plan_sha256": plan.plan_sha256,
        "confirmed_partial": False,
        "stages": [{"index": 0, "key": stage_key, "sha256": digest}],
    }
    identity = hashlib.sha256(json.dumps(identity_value, sort_keys=True).encode()).hexdigest()
    key = prefix(plan.run_id) + f"/ready-{identity}.json"
    ready_bytes = json.dumps(
        {**identity_value, "created_at": "2026-10-10T01:00:00+00:00"}, sort_keys=True
    ).encode()
    store.store_object(key, ready_bytes)
    reference = {"key": key, "sha256": hashlib.sha256(ready_bytes).hexdigest()}
    loaded = load_ready(store, reference)
    assert loaded.stages[0][0] == stage
    before = list(store.put_keys)
    assert save_ready(store, loaded) == reference
    assert store.put_keys == before
    assert store.get_bytes(stage_key) == payload


def test_not_computed_stage_preserves_explicit_state_and_has_normal_null_accounting():
    plan, store = _plan(), MemoryR2Store()
    value = json.loads(_legacy_stage(plan))
    entry = value["result"]["entries"][0]
    entry.update(state="normal_null", reason="all_values_missing")
    observation = entry["observation"]
    observation.update(
        availability="not_computed", score_as_of_date=None, parser_version="kabumap-quants-4"
    )
    for name in (
        "growth_score",
        "undervaluation_score",
        "size_score",
        "technical_score",
        "financial_health_score",
        "market_trend_score",
        "composite_score",
        "industry_average_score",
    ):
        observation[name] = None
    payload = _bytes(value)
    stage = load_stage(plan, payload)
    assert stage_bytes(plan, stage) == payload
    prepared = prepare_saved_stages(plan, {0: (stage, "synthetic-etag")})
    assert prepared.accounting["state_counts"] == {
        "success": 0,
        "normal_null": 1,
        "failure": 0,
        "unattempted": 0,
    }
    assert prepared.accounting["success_completeness"] is True
    save_new_plan(store, plan)
    reference = save_ready(store, prepared)
    loaded = load_ready(store, reference)
    assert loaded.stages[0][0] == stage
    assert save_ready(store, loaded) == reference
