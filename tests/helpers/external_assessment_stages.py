"""外部評価の固定planと条件付きstageを試す共有入力。

serial ETag・読取競合・PUT応答喪失を注入するfakeであり、通常のMemoryR2Storeとは
失敗契約が異なる。似たAPIだけを理由に統合しない。
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime

import baibai_jobs.acquisition.external_assessments.daily as daily
import baibai_jobs.acquisition.external_assessments.staging as staging
from baibai_engine.machine_analysis.observations.external_assessments.models import (
    ModelValuationObservation as ExternalAssessmentModelValuationObservation,
)
from baibai_engine.storage.objects.r2 import LakeCASConflict, LakePublishError, RemoteObject
from baibai_jobs.acquisition.external_assessments.cohorts import make_plan, record_outcome
from baibai_jobs.acquisition.external_assessments.collection import (
    CollectionOutcome as ExternalAssessmentCollectionOutcome,
)
from baibai_jobs.acquisition.external_assessments.collection import (
    CollectionResult as ExternalAssessmentCollectionResult,
)
from baibai_jobs.acquisition.external_assessments.collection import (
    collector_binding as external_assessment_collector_binding,
)
from tests.helpers.external_assessment_responses import response

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
