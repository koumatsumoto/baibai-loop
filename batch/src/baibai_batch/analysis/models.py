"""Stop invalid analysis packets and AI results before canonical publication."""

from __future__ import annotations

from datetime import date
from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TaskReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    task_id: str = Field(min_length=1, max_length=160)
    type: Literal["research-triage", "macro-context"]
    subject: str = Field(min_length=1, max_length=80)
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload_path: str = Field(min_length=1)
    required_result_schema: str = Field(min_length=1)
    reused: bool = False

    @field_validator("payload_path")
    @classmethod
    def _bounded_payload_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "tasks":
            raise ValueError("payload_path must stay below packet/tasks")
        return value


class BatchReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    batch_id: str = Field(min_length=1, max_length=160)
    type: Literal["research-triage", "macro-context"]
    task_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("task_ids", mode="before")
    @classmethod
    def _tuple_task_ids(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class PacketIndex(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    packet_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    asof: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    source_manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: str = Field(min_length=1)
    prompt_policy_version: str = Field(min_length=1)
    packet_schema_version: Literal[1]
    rules_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["no_ai", "ai_required", "machine_incomplete"]
    tasks: tuple[TaskReference, ...]
    batches: tuple[BatchReference, ...] = ()
    warning: str | None = None
    required_human_action: str | None = None
    packet_bytes: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)

    @field_validator("tasks", mode="before")
    @classmethod
    def _tuple_tasks(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("batches", mode="before")
    @classmethod
    def _tuple_batches(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _task_contract(self) -> Self:
        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("packet task_id values must be unique")
        if self.status == "no_ai" and any(not task.reused for task in self.tasks):
            raise ValueError("no_ai packet cannot contain unevaluated tasks")
        if self.status == "ai_required" and not any(not task.reused for task in self.tasks):
            raise ValueError("ai_required packet must contain an unevaluated task")
        batch_ids = [batch.batch_id for batch in self.batches]
        if len(batch_ids) != len(set(batch_ids)):
            raise ValueError("packet batch_id values must be unique")
        batched_task_ids = [task_id for batch in self.batches for task_id in batch.task_ids]
        if len(batched_task_ids) != len(set(batched_task_ids)):
            raise ValueError("one task cannot appear in multiple model batches")
        expected = [task.task_id for task in self.tasks if not task.reused]
        if batched_task_ids != expected:
            raise ValueError("model batches must cover unevaluated tasks in stable task order")
        task_types = {task.task_id: task.type for task in self.tasks}
        if any(
            task_types.get(task_id) != batch.type
            for batch in self.batches
            for task_id in batch.task_ids
        ):
            raise ValueError("model batch type must match every referenced task")
        return self


class TriageJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    verdict: Literal["research", "skip"]
    rationale: str = Field(min_length=1, max_length=1200)
    research_question: str | None = Field(default=None, max_length=600)
    key_risk: str | None = Field(default=None, max_length=600)

    @model_validator(mode="after")
    def _decision_shape(self) -> Self:
        if self.verdict == "research" and (not self.research_question or not self.key_risk):
            raise ValueError("research requires research_question and key_risk")
        if self.verdict == "skip" and (
            self.research_question is not None or self.key_risk is not None
        ):
            raise ValueError("skip forbids research_question and key_risk")
        return self


class MacroJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    phase: Literal["independent_current"]
    current_assessment: str = Field(min_length=1, max_length=6000)
    counter_evidence: tuple[str, ...] = Field(min_length=1, max_length=20)
    source_ids: tuple[str, ...] = Field(min_length=1, max_length=80)

    @field_validator("counter_evidence", "source_ids", mode="before")
    @classmethod
    def _tuple_strings(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class AIResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    task_id: str = Field(min_length=1)
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    judgment: TriageJudgment | MacroJudgment


class AIResultEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    packet_id: str = Field(min_length=1)
    results: tuple[AIResult, ...]

    @field_validator("results", mode="before")
    @classmethod
    def _tuple_results(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _unique_results(self) -> Self:
        task_ids = [result.task_id for result in self.results]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("AI result task_id values must be unique")
        return self


class DailyStepManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(min_length=1)
    argv: tuple[str, ...]
    returncode: int
    duration_seconds: float = Field(ge=0)
    stdout_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stderr_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("argv", mode="before")
    @classmethod
    def _tuple_argv(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class DailyManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    status: Literal["skipped_non_business_day", "machine_complete", "deferred"]
    asof: str
    exit_code: Literal[0, 3]
    run_revision_id: str | None
    review_set_id: str | None
    deferred_failure_count: int = Field(ge=0)
    steps: tuple[DailyStepManifest, ...]

    @field_validator("asof")
    @classmethod
    def _iso_date(cls, value: str) -> str:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError("asof must be an ISO date")
        return value

    @field_validator("steps", mode="before")
    @classmethod
    def _tuple_steps(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _outcome_contract(self) -> Self:
        if self.status == "skipped_non_business_day":
            if (
                self.exit_code != 0
                or self.run_revision_id is not None
                or self.review_set_id is not None
                or self.deferred_failure_count != 0
            ):
                raise ValueError("non-business-day manifest has inconsistent publication state")
            return self
        if not self.run_revision_id or not self.review_set_id:
            raise ValueError("business-day manifest requires run and Review Set identities")
        if self.status == "machine_complete" and (
            self.exit_code != 0 or self.deferred_failure_count != 0
        ):
            raise ValueError("machine_complete manifest has inconsistent exit state")
        if self.status == "deferred" and (self.exit_code != 3 or self.deferred_failure_count < 1):
            raise ValueError("deferred manifest has inconsistent exit state")
        return self


__all__ = [
    "AIResult",
    "AIResultEnvelope",
    "BatchReference",
    "DailyManifest",
    "MacroJudgment",
    "PacketIndex",
    "TaskReference",
    "TriageJudgment",
]
