"""Private, content-addressed input for one exact JPX recovery, never executable input."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_jobs.integration.jpx_recovery_verification import MARKET_DATASETS

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ETag = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
StageID = Annotated[
    str, Field(pattern=r"^jpx-(earnings|regulations)-[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
]
ReleaseID = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]
PREFIX = "coordination/jpx-recovery/v1/"


def unique_json(payload: bytes | str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    return json.loads(payload, object_pairs_hook=pairs)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Fingerprint(_Strict):
    count: Annotated[int, Field(ge=0)]
    sha256: Digest


class ObjectProof(_Strict):
    etag: ETag
    sha256: Digest


class Baseline(_Strict):
    current: ObjectProof
    machine_receipt: ObjectProof
    market_etag: ETag
    runs_etag: ETag
    economic_etag: ETag
    lake_release_id: ReleaseID
    lake_manifest_sha256: Digest

    def generations(self) -> dict[str, str]:
        return {
            "lake_release_id": self.lake_release_id,
            "lake_manifest_sha256": self.lake_manifest_sha256,
        }


class StageProof(_Strict):
    role: Literal["recovery", "preserved", "pending"]
    stage_id: StageID
    handoff_sha256: Digest
    marker_sha256: Digest | None
    receipt_sha256: Digest
    receipt_bytes: Annotated[int, Field(gt=0, le=64 * 1024 * 1024)]

    @model_validator(mode="after")
    def marker_scope(self) -> Self:
        if (self.marker_sha256 is None) != (self.role == "pending"):
            raise ValueError("only pending stages must have no integrated marker")
        return self


class RecoveryContract(_Strict):
    version: Literal[1]
    source_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    asof: str
    plan_json: Annotated[str, Field(max_length=100_000)]
    plan_sha256: Digest
    baseline: Baseline
    stages: Annotated[list[StageProof], Field(min_length=6, max_length=6)]
    before: dict[str, Fingerprint]
    after_jpx: dict[str, Fingerprint]
    jpx_coverage_before: Fingerprint
    jpx_coverage_after: Fingerprint
    edinet_coverage_before: Fingerprint
    edinet_projection_clock_updates: Annotated[int, Field(ge=0)]
    edinet_archive_count: Annotated[int, Field(gt=0)]

    @property
    def plan(self) -> dict[str, Any]:
        return unique_json(self.plan_json)  # type: ignore[no-any-return]

    @model_validator(mode="after")
    def exact_scope(self) -> Self:
        if date.fromisoformat(self.asof).isoformat() != self.asof:
            raise ValueError("invalid recovery target date")
        if hashlib.sha256(self.plan_json.encode()).hexdigest() != self.plan_sha256:
            raise ValueError("original plan bytes differ")
        plan = self.plan
        if (
            set(plan) != {"handoffs", "edinet_input", "external", "external_failed"}
            or plan["external"] != []
            or plan["external_failed"] != []
            or not isinstance(plan["handoffs"], list)
            or len(plan["handoffs"]) != 2
        ):
            raise ValueError("recovery requires only two exact JPX handoffs")
        fixed = InputSnapshot.model_validate(plan["edinet_input"])
        if (
            fixed.kind != "native_l1"
            or fixed.reference != "edinet-" + fixed.sha256
            or fixed.descriptor is None
            or set(fixed.descriptor) != {"format", "selection_sha256"}
            or fixed.descriptor["format"] != "edinet-input-v1"
        ):
            raise ValueError("fixed EDINET input is required")
        stages = {stage.stage_id: stage for stage in self.stages}
        if len(stages) != 6:
            raise ValueError("duplicate protected stage")
        for role in ("recovery", "preserved", "pending"):
            members = [stage for stage in self.stages if stage.role == role]
            if len(members) != 2 or {stage.stage_id.split("-")[1] for stage in members} != {
                "earnings",
                "regulations",
            }:
                raise ValueError("each stage role requires both JPX sources")
        handoffs = plan["handoffs"]
        if {entry.get("source") for entry in handoffs} != {"jpx-earnings", "jpx-regulations"}:
            raise ValueError("unexpected recovery source")
        for entry in handoffs:
            stage = stages.get(entry.get("stage_id"))
            if (
                set(entry)
                != {
                    "market",
                    "source",
                    "stage_id",
                    "manifest_sha256",
                    "started_at",
                    "asof",
                    "integration",
                    "explicit_jpx_recovery",
                }
                or entry["market"] is not True
                or entry["explicit_jpx_recovery"] is not True
                or stage is None
                or stage.role != "recovery"
                or entry["manifest_sha256"] != stage.handoff_sha256
                or not stage.stage_id.startswith(entry["source"] + "-")
                or date.fromisoformat(entry["asof"]) > date.fromisoformat(self.asof)
            ):
                raise ValueError("handoff differs from the exact recovery scope")
        if set(self.before) != set(MARKET_DATASETS):
            raise ValueError("incomplete market preservation contract")
        if set(self.after_jpx) != {name for name in MARKET_DATASETS if name.startswith("jpx.")}:
            raise ValueError("all five JPX tables are required")
        if self.after_jpx["jpx.earnings_calendar"] != self.before["jpx.earnings_calendar"]:
            raise ValueError("newer earnings calendar must be preserved")
        return self

    def expected_before(self) -> dict[str, dict[str, Any]]:
        return {name: value.model_dump() for name, value in self.before.items()}

    def expected_after(self) -> dict[str, dict[str, Any]]:
        return {
            **self.expected_before(),
            **{name: value.model_dump() for name, value in self.after_jpx.items()},
        }


def decode_contract(payload: bytes, digest: str) -> RecoveryContract:
    if not 0 < len(payload) <= 2_000_000 or hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("private recovery contract digest or size differs")
    return RecoveryContract.model_validate(unique_json(payload))
