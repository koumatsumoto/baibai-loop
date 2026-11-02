"""追記型の試行結果とCAS前の候補stage。勝者の進捗とは別に原受領を辿る。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from baibai_engine.storage.objects.r2 import ObjectStore
from baibai_jobs.acquisition.external_assessments.cohorts import RunPlan, new_result, record_outcome
from baibai_jobs.acquisition.external_assessments.collection import CollectionResult
from baibai_jobs.acquisition.external_assessments.staging import (
    CohortStage,
    load_stage,
    prefix,
    put_private,
    read_private,
    stage_bytes,
)
from baibai_jobs.acquisition.receipts import ReceiptStore

_FORMAT = "external-assessment-attempt-journal-v1"


def attempt_value(
    plan: RunPlan, index: int, result: CollectionResult, *, completed_at: datetime
) -> dict[str, object]:
    # 各retryも最後の成功で上書きしない。元のoutcomeと原提供者の時計は別に保持する。
    record_outcome(
        plan,
        new_result(plan, index),
        result.outcomes[0],
        result.observations[0] if result.observations else None,
    )
    return {
        **result.summary(),
        "completed_at_utc": completed_at.isoformat(),
        "last_response_finished_at_utc": result.last_response_finished_at_utc.isoformat()
        if result.last_response_finished_at_utc is not None
        else None,
        "response_content_bytes": result.response_content_bytes,
    }


def journal_prefix(plan: RunPlan) -> str:
    return prefix(plan.run_id) + "/attempts/"


def save_attempts(
    store: ObjectStore, plan: RunPlan, stage: CohortStage, attempts: list[dict[str, object]]
) -> str:
    value = {
        "format": _FORMAT,
        "plan_sha256": plan.plan_sha256,
        "candidate_stage": json.loads(stage_bytes(plan, stage)),
        "attempts": attempts,
    }
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    key = journal_prefix(plan) + hashlib.sha256(payload).hexdigest() + ".json"
    existing = read_private(store, key)
    if existing is None:
        try:
            put_private(store, key, payload, previous_etag=None)
        except Exception:
            # 応答喪失だけは同一bytesの読戻しで確定。異なる候補を勝者へ上書きしない。
            saved = read_private(store, key)
            if saved is None or saved.payload != payload:
                raise
    elif existing.payload != payload:
        raise ValueError("external attempt journal content identity changed")
    return key


def read_attempts(store: ReceiptStore, plan: RunPlan) -> list[dict[str, Any]]:
    journals = []
    for key, _ in store.iter_objects(journal_prefix(plan)):
        saved = read_private(store, key)
        if saved is None:
            raise ValueError("external attempt journal disappeared")
        expected = journal_prefix(plan) + hashlib.sha256(saved.payload).hexdigest() + ".json"
        value = json.loads(saved.payload)
        if (
            key != expected
            or value["format"] != _FORMAT
            or value["plan_sha256"] != plan.plan_sha256
        ):
            raise ValueError("external attempt journal binding changed")
        load_stage(plan, json.dumps(value["candidate_stage"]).encode())
        journals.append({"key": key, **value})
    return journals
