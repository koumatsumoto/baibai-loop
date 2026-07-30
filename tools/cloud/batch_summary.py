"""Structured batch execution summaries and Discord-safe error redaction.

This module is the structured contract between the daily batch and the workflow
notification adapter. It is deliberately dependency-free: it imports only the
Python standard library and avoids 3.13+ syntax so the notification step can
import and run it on the GitHub-hosted runner's system ``python3`` before
``setup-python`` / ``uv sync`` have run.

Two schema-versioned models are kept separate on purpose:

* ``BatchExecutionSummary`` is finalized by ``daily_batch.py`` before the process
  exits. It carries the as-of date, the batch execution outcome, the per-batch
  results, and whether a local export was produced. It never carries upload or
  delivery state.
* ``WorkflowRunSummary`` is finalized once by the notification step after every
  prior step outcome is known. It wraps the batch summary in a discriminated
  ``execution`` union (``available`` / ``not_started`` / ``unavailable``) and adds
  GitHub metadata, the R2 publish state, the Discord delivery result, the overall
  outcome, and workflow-level errors.

Errors are typed and redacted. A ``BatchError`` is built only from an allowlisted
``code`` / ``stage`` / ``impact`` plus validated scalars rendered through a fixed
template. Subprocess stderr, arbitrary exception text, and provider response
bodies are never an input to the message builder; the rendered message is a
single line bounded to 400 characters.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BATCH_SUMMARY_SCHEMA_VERSION = 1
WORKFLOW_SUMMARY_SCHEMA_VERSION = 1

MESSAGE_MAX_CHARS = 400
_SCALAR_MAX_CHARS = 120

OUTCOME_SUCCEEDED = "succeeded"
OUTCOME_SKIPPED = "skipped_non_business_day"
OUTCOME_DEGRADED = "published_with_deferred_failure"
OUTCOME_FAILED = "failed"
# A job that hit `timeout-minutes` is reported by GitHub as *cancelled*, not
# failed, so an interrupted run needs its own outcome: a hung batch is the most
# likely silent failure here, and folding it into `failed` would make a routine
# manual cancel look identical to it.
OUTCOME_CANCELLED = "cancelled"
OUTCOMES = (
    OUTCOME_SUCCEEDED,
    OUTCOME_SKIPPED,
    OUTCOME_DEGRADED,
    OUTCOME_FAILED,
    OUTCOME_CANCELLED,
)
OUTCOME_LABELS = {
    OUTCOME_SUCCEEDED: "[OK]",
    OUTCOME_SKIPPED: "[SKIPPED]",
    OUTCOME_DEGRADED: "[DEGRADED]",
    OUTCOME_FAILED: "[FAILED]",
    OUTCOME_CANCELLED: "[CANCELLED]",
}

PUBLISH_NOT_GENERATED = "not_generated"
PUBLISH_GENERATED = "generated"
PUBLISH_UPLOAD_FAILED = "upload_failed"
PUBLISH_PUBLISHED = "published"
PUBLISH_STATES = (
    PUBLISH_NOT_GENERATED,
    PUBLISH_GENERATED,
    PUBLISH_UPLOAD_FAILED,
    PUBLISH_PUBLISHED,
)

# Overall outcome constrains the publish state it may appear with. ``failed`` is
# compatible with every publish state because a run can fail before, during, or
# after upload; the other outcomes pin a single publish state.
_ALLOWED_PUBLISH_BY_OUTCOME = {
    OUTCOME_SUCCEEDED: frozenset({PUBLISH_PUBLISHED}),
    OUTCOME_SKIPPED: frozenset({PUBLISH_NOT_GENERATED}),
    OUTCOME_DEGRADED: frozenset({PUBLISH_PUBLISHED}),
    OUTCOME_FAILED: frozenset(PUBLISH_STATES),
    # An interrupted run can be cut at any point, before or after either upload.
    OUTCOME_CANCELLED: frozenset(PUBLISH_STATES),
}

BATCH_STATUS_OK = "ok"
BATCH_STATUS_DEGRADED = "degraded"
BATCH_STATUS_FAILED = "failed"
BATCH_STATUS_SKIPPED = "skipped"
BATCH_STATUSES = (BATCH_STATUS_OK, BATCH_STATUS_DEGRADED, BATCH_STATUS_FAILED, BATCH_STATUS_SKIPPED)

DELIVERY_NOT_ATTEMPTED = "not_attempted"
DELIVERY_DELIVERED = "delivered"
DELIVERY_FAILED = "failed"
DELIVERY_STATUSES = (DELIVERY_NOT_ATTEMPTED, DELIVERY_DELIVERED, DELIVERY_FAILED)

EXECUTION_AVAILABLE = "available"
EXECUTION_NOT_STARTED = "not_started"
EXECUTION_UNAVAILABLE = "unavailable"
EXECUTION_KINDS = (EXECUTION_AVAILABLE, EXECUTION_NOT_STARTED, EXECUTION_UNAVAILABLE)

ERROR_IMPACT_FAILED = "failed"
ERROR_IMPACT_DEGRADED = "degraded"
ERROR_IMPACTS = (ERROR_IMPACT_FAILED, ERROR_IMPACT_DEGRADED)

# Fixed vocabulary for typed errors. A message builder only accepts these values;
# free text never becomes a code, stage, or impact.
ERROR_STAGES = (
    "smoke",
    "setup",
    "sync",
    "pull-stores",
    "verify-cache-coverage",
    "bootstrap-cache",
    "extract-edinet-metrics",
    "screening-run",
    "screening-select",
    "macro-list",
    "macro-refresh",
    "export-read-models",
    "screening-prune",
    "upload-machine",
    "upload-serving",
    "batch",
    # A step before the batch that the notification does not track by id
    # (checkout, setup-uv, the Playwright steps). Naming it "pre-batch" keeps the
    # report from pointing at a step that actually succeeded.
    "pre-batch",
    "summary",
    "notification",
    "calendar",
    "input",
    "root",
)

# Fixed message templates keyed by error code. Placeholders are filled only from
# validated scalars; there is no template that embeds stderr, exception text, or a
# provider response body.
_MESSAGE_TEMPLATES = {
    "subprocess_failed": "step '{stage}' exited {returncode}",
    "command_not_found": "step '{stage}' command was not found",
    "calendar_store_missing": "market calendar store is missing",
    "calendar_unreadable": "market calendar store is unreadable",
    "calendar_uncovered": "market calendar does not cover {asof}",
    "invalid_asof": "asof '{value}' is not a valid YYYY-MM-DD date",
    "invalid_root": "repo root is missing required project markers",
    "run_view_invalid": "screening run output is missing run_revision_id",
    "select_output_invalid": "screening select output is missing selection_id",
    "runs_store_unreadable": "runs store is unreadable for previous-run resolution",
    "summary_missing": "batch summary was not produced",
    "summary_invalid": "batch summary is invalid: {reason}",
    "summary_conflict": "batch summary conflicts with the workflow outcome",
    "upload_failed": "upload step '{stage}' failed",
    "step_failed": "GitHub Actions step failed; open the run log",
    "batch_failed": "batch failed before completion",
}
ERROR_CODES = tuple(_MESSAGE_TEMPLATES)

SUMMARY_INVALID_REASONS = (
    "malformed_json",
    "summary_missing",
    "schema_version",
    "missing_field",
    "invalid_field",
    "duplicate_batch",
    "non_finite_metric",
    "unknown_status",
    "unknown_batch",
    "inconsistent_outcome",
    "delivery_in_batch_summary",
)

# Per-batch metric schema: each batch_name maps to its required metric keys and
# their exact scalar types. A new logical batch is added as an entry here; the
# renderer and the workflow summary stay unchanged, so the envelope does not
# depend on how many processes produce the results.
_BATCH_METRIC_SCHEMA: dict[str, dict[str, type]] = {
    "screening": {
        "asof": str,
        "run_revision_id": str,
        "selection_id": str,
        "universe": int,
        "candidates": int,
        "selected": int,
    },
    "macro": {"target": int, "success": int, "failure": int},
    # The delta counts ride the export batch because the notification is the only
    # channel that reaches a reader without being opened. Every key is always
    # present: ``delta_measured`` false with zero counts says "not measured", which
    # zero counts alone could not distinguish from "nothing changed".
    "serving-export": {
        "local_output": bool,
        "delta_measured": bool,
        "delta_entered": int,
        "delta_exited": int,
        "delta_er_moves": int,
        "delta_holdings": int,
        "delta_macro_flags": int,
        "delta_unavailable": str,
    },
    "prune": {},
}
BATCH_NAMES = tuple(_BATCH_METRIC_SCHEMA)


class SummaryValidationError(ValueError):
    """A summary payload failed schema, vocabulary, or consistency validation.

    ``reason`` carries the machine vocabulary the notification reports; the
    message stays human-facing. Without it every structural failure collapses to
    ``malformed_json``, which sends an operator looking for a truncated write
    when the real cause is a schema bump or a drifted metric key.
    """

    def __init__(self, message: str, *, reason: str = "invalid_field") -> None:
        super().__init__(message)
        self.reason = reason if reason in SUMMARY_INVALID_REASONS else "invalid_field"


def sanitize_one_line(value: object, max_chars: int = _SCALAR_MAX_CHARS) -> str:
    """Render a value as bounded single-line text.

    Whitespace runs collapse to one space and braces are stripped so a value can
    never inject a format placeholder or a newline into a rendered message. Used
    for typed-error scalars and for free-text workflow inputs (e.g. ``asof``)
    before they reach a notification payload.
    """

    text = re.sub(r"\s+", " ", str(value)).strip()
    text = text.replace("{", "").replace("}", "")
    return text[:max_chars]


def _sanitize_message(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()[:MESSAGE_MAX_CHARS]


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return isinstance(value, int)


def _is_json_scalar(value: object) -> bool:
    if value is None or isinstance(value, (str, bool)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return isinstance(value, int)


def _validate_metric_value(batch_name: str, key: str, value: object) -> None:
    if isinstance(value, list):
        for item in value:
            if not _is_json_scalar(item):
                raise SummaryValidationError(
                    f"batch {batch_name!r} metric {key!r} list has a non-scalar or non-finite item",
                    reason="non_finite_metric",
                )
        return
    if not _is_json_scalar(value):
        raise SummaryValidationError(
            f"batch {batch_name!r} metric {key!r} is not a finite JSON scalar"
        )


def _check_metric_type(batch_name: str, key: str, value: object, expected: type) -> None:
    # ``expected`` is exactly one of bool/int/float/str, so at most one branch fires.
    if expected is bool and not isinstance(value, bool):
        raise SummaryValidationError(f"batch {batch_name!r} metric {key!r} must be bool")
    if expected is int and (isinstance(value, bool) or not isinstance(value, int)):
        raise SummaryValidationError(f"batch {batch_name!r} metric {key!r} must be int")
    if expected is float and (isinstance(value, bool) or not _is_finite_number(value)):
        raise SummaryValidationError(
            f"batch {batch_name!r} metric {key!r} must be finite number",
            reason="non_finite_metric",
        )
    if expected is str and not isinstance(value, str):
        raise SummaryValidationError(f"batch {batch_name!r} metric {key!r} must be str")


def _validate_metric_schema(batch_name: str, metrics: dict[str, object], status: str) -> None:
    schema = _BATCH_METRIC_SCHEMA.get(batch_name)
    if schema is None:
        raise SummaryValidationError(f"unknown batch_name {batch_name!r}", reason="unknown_batch")
    if status in (BATCH_STATUS_FAILED, BATCH_STATUS_SKIPPED):
        # A failed or skipped batch carries no metrics; its errors tell the story.
        if metrics:
            raise SummaryValidationError(
                f"batch {batch_name!r} with status {status!r} must have empty metrics"
            )
        return
    missing = set(schema) - set(metrics)
    if missing:
        raise SummaryValidationError(
            f"batch {batch_name!r} metrics missing keys: {sorted(missing)}"
        )
    extra = set(metrics) - set(schema)
    if extra:
        raise SummaryValidationError(
            f"batch {batch_name!r} metrics has unknown keys: {sorted(extra)}"
        )
    for key, expected in schema.items():
        _check_metric_type(batch_name, key, metrics[key], expected)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON via a temp file + rename so a reader never sees a partial file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        tmp_path.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


@dataclass(frozen=True, slots=True)
class BatchError:
    """A typed, redacted error. Built only from allowlisted fields + scalars."""

    code: str
    stage: str
    impact: str
    message: str

    def to_json(self) -> dict[str, str]:
        return {
            "code": self.code,
            "stage": self.stage,
            "impact": self.impact,
            "message": self.message,
        }

    @classmethod
    def from_json(cls, payload: object) -> BatchError:
        if not isinstance(payload, dict):
            raise SummaryValidationError("error must be an object")
        for key in ("code", "stage", "impact", "message"):
            if not isinstance(payload.get(key), str):
                raise SummaryValidationError(f"error.{key} must be a string")
        code = payload["code"]
        stage = payload["stage"]
        impact = payload["impact"]
        if code not in _MESSAGE_TEMPLATES:
            raise SummaryValidationError(f"unknown error code {code!r}")
        if stage not in ERROR_STAGES:
            raise SummaryValidationError(f"unknown error stage {stage!r}")
        if impact not in ERROR_IMPACTS:
            raise SummaryValidationError(f"unknown error impact {impact!r}")
        message = _sanitize_message(payload["message"])
        if not message:
            raise SummaryValidationError("error.message must be non-empty")
        return cls(code=code, stage=stage, impact=impact, message=message)

    @classmethod
    def build(cls, *, code: str, stage: str, impact: str, **scalars: object) -> BatchError:
        """Render a fixed template from allowlisted fields and validated scalars.

        The signature structurally excludes stderr, exception text, and response
        bodies: only ``code`` / ``stage`` / ``impact`` plus named scalars that map
        to template placeholders are accepted.
        """

        if code not in _MESSAGE_TEMPLATES:
            raise SummaryValidationError(f"unknown error code {code!r}")
        if stage not in ERROR_STAGES:
            raise SummaryValidationError(f"unknown error stage {stage!r}")
        if impact not in ERROR_IMPACTS:
            raise SummaryValidationError(f"unknown error impact {impact!r}")
        safe = {key: sanitize_one_line(value) for key, value in scalars.items()}
        try:
            rendered = _MESSAGE_TEMPLATES[code].format(stage=stage, **safe)
        except KeyError as exc:
            raise SummaryValidationError(
                f"error code {code!r} is missing scalar {exc} for its template"
            ) from exc
        return cls(code=code, stage=stage, impact=impact, message=_sanitize_message(rendered))

    @classmethod
    def subprocess_failure(
        cls, *, stage: str, returncode: int, impact: str = ERROR_IMPACT_FAILED
    ) -> BatchError:
        if stage not in ERROR_STAGES:
            raise SummaryValidationError(f"unknown error stage {stage!r}")
        # `type(...) is not int` rather than isinstance: bool is an int subclass, and a
        # True that reached here would be rendered as exit code 1.
        if type(returncode) is not int:
            raise SummaryValidationError("returncode must be an int")
        return cls.build(
            code="subprocess_failed", stage=stage, impact=impact, returncode=returncode
        )

    @classmethod
    def summary_invalid(cls, *, reason: str, stage: str = "summary") -> BatchError:
        if reason not in SUMMARY_INVALID_REASONS:
            raise SummaryValidationError(f"unknown summary-invalid reason {reason!r}")
        return cls.build(
            code="summary_invalid", stage=stage, impact=ERROR_IMPACT_FAILED, reason=reason
        )


@dataclass(frozen=True, slots=True)
class BatchResult:
    """One logical batch's outcome inside a batch execution summary."""

    batch_name: str
    datasets: tuple[str, ...]
    status: str
    duration_seconds: float
    metrics: dict[str, Any] = field(default_factory=dict)
    errors: tuple[BatchError, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "batch_name": self.batch_name,
            "datasets": list(self.datasets),
            "status": self.status,
            "duration_seconds": self.duration_seconds,
            "metrics": dict(self.metrics),
            "errors": [error.to_json() for error in self.errors],
        }

    @classmethod
    def from_json(cls, payload: object) -> BatchResult:
        if not isinstance(payload, dict):
            raise SummaryValidationError("batch result must be an object")
        batch_name = payload.get("batch_name")
        if not isinstance(batch_name, str) or not batch_name:
            raise SummaryValidationError("batch result batch_name must be a non-empty string")
        status = payload.get("status")
        if status not in BATCH_STATUSES:
            raise SummaryValidationError(
                f"batch {batch_name!r} has unknown status {status!r}", reason="unknown_status"
            )
        datasets_raw = payload.get("datasets")
        if not isinstance(datasets_raw, list) or not all(
            isinstance(item, str) and item for item in datasets_raw
        ):
            raise SummaryValidationError(f"batch {batch_name!r} datasets must be non-empty strings")
        duration = payload.get("duration_seconds")
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(duration)
        ):
            raise SummaryValidationError(f"batch {batch_name!r} duration_seconds must be finite")
        metrics_raw = payload.get("metrics")
        if not isinstance(metrics_raw, dict):
            raise SummaryValidationError(f"batch {batch_name!r} metrics must be an object")
        for key, value in metrics_raw.items():
            _validate_metric_value(batch_name, key, value)
        _validate_metric_schema(batch_name, metrics_raw, status)
        errors_raw = payload.get("errors")
        if not isinstance(errors_raw, list):
            raise SummaryValidationError(f"batch {batch_name!r} errors must be a list")
        errors = tuple(BatchError.from_json(item) for item in errors_raw)
        return cls(
            batch_name=batch_name,
            datasets=tuple(datasets_raw),
            status=status,
            duration_seconds=float(duration),
            metrics=dict(metrics_raw),
            errors=errors,
        )


@dataclass(frozen=True, slots=True)
class BatchExecutionSummary:
    """Pre-upload batch result finalized by ``daily_batch.py`` before exit."""

    schema_version: int
    asof: str
    outcome: str
    started_at: str
    finished_at: str
    duration_seconds: float
    batches: tuple[BatchResult, ...]
    local_export: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "asof": self.asof,
            "outcome": self.outcome,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "batches": [batch.to_json() for batch in self.batches],
            "local_export": self.local_export,
        }

    @classmethod
    def from_json(cls, payload: object) -> BatchExecutionSummary:
        if not isinstance(payload, dict):
            raise SummaryValidationError("batch execution summary must be an object")
        if "delivery" in payload:
            raise SummaryValidationError(
                "batch execution summary must not carry delivery state",
                reason="delivery_in_batch_summary",
            )
        if payload.get("schema_version") != BATCH_SUMMARY_SCHEMA_VERSION:
            raise SummaryValidationError(
                f"unsupported batch summary schema_version {payload.get('schema_version')!r}",
                reason="schema_version",
            )
        for key in ("asof", "outcome", "started_at", "finished_at"):
            if not isinstance(payload.get(key), str) or not payload.get(key):
                raise SummaryValidationError(f"batch summary {key} must be a non-empty string")
        outcome = payload["outcome"]
        if outcome not in OUTCOMES:
            raise SummaryValidationError(f"batch summary has unknown outcome {outcome!r}")
        duration = payload.get("duration_seconds")
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(duration)
        ):
            raise SummaryValidationError("batch summary duration_seconds must be finite")
        if not isinstance(payload.get("local_export"), bool):
            raise SummaryValidationError("batch summary local_export must be a bool")
        batches_raw = payload.get("batches")
        if not isinstance(batches_raw, list):
            raise SummaryValidationError("batch summary batches must be a list")
        batches = tuple(BatchResult.from_json(item) for item in batches_raw)
        seen: set[str] = set()
        for batch in batches:
            if batch.batch_name in seen:
                raise SummaryValidationError(
                    f"duplicate batch_name {batch.batch_name!r}", reason="duplicate_batch"
                )
            seen.add(batch.batch_name)
        _require_outcome_matches_batches(outcome, batches)
        return cls(
            schema_version=BATCH_SUMMARY_SCHEMA_VERSION,
            asof=payload["asof"],
            outcome=outcome,
            started_at=payload["started_at"],
            finished_at=payload["finished_at"],
            duration_seconds=float(duration),
            batches=batches,
            local_export=payload["local_export"],
        )


def _require_outcome_matches_batches(outcome: str, batches: Sequence[BatchResult]) -> None:
    """Reject a summary whose headline outcome contradicts its own batches.

    The outcome is decided once at the end of the run while each status is
    decided inside its section, so the two can drift apart — and a drift that
    puts ``[OK]`` above a failed batch hides exactly the degradation the
    notification exists to surface. The check lives in the schema so it holds for
    any producer, however loosely the two decisions are coupled.
    """

    statuses = {batch.status for batch in batches}
    if outcome == OUTCOME_SUCCEEDED and not statuses <= {BATCH_STATUS_OK, BATCH_STATUS_SKIPPED}:
        raise SummaryValidationError(
            f"outcome {outcome!r} contradicts batch statuses {sorted(statuses)}",
            reason="inconsistent_outcome",
        )
    if outcome == OUTCOME_DEGRADED and BATCH_STATUS_FAILED in statuses:
        raise SummaryValidationError(
            f"outcome {outcome!r} cannot carry a failed batch",
            reason="inconsistent_outcome",
        )


@dataclass(frozen=True, slots=True)
class Execution:
    """Discriminated union of how the batch step's summary is available.

    * ``available`` — the batch ran and produced a valid summary.
    * ``not_started`` — the batch step was never reached (a prior step failed).
    * ``unavailable`` — the batch step ran but its summary is missing or invalid.
    """

    kind: str
    summary: BatchExecutionSummary | None = None
    stage: str | None = None
    error: BatchError | None = None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": self.kind}
        if self.kind == EXECUTION_AVAILABLE:
            assert self.summary is not None
            payload["summary"] = self.summary.to_json()
        elif self.kind == EXECUTION_NOT_STARTED:
            payload["stage"] = self.stage
        elif self.kind == EXECUTION_UNAVAILABLE:
            assert self.error is not None
            payload["error"] = self.error.to_json()
        return payload

    @classmethod
    def available(cls, summary: BatchExecutionSummary) -> Execution:
        return cls(kind=EXECUTION_AVAILABLE, summary=summary)

    @classmethod
    def not_started(cls, stage: str) -> Execution:
        if stage not in ERROR_STAGES:
            raise SummaryValidationError(f"unknown not_started stage {stage!r}")
        return cls(kind=EXECUTION_NOT_STARTED, stage=stage)

    @classmethod
    def unavailable(cls, error: BatchError) -> Execution:
        return cls(kind=EXECUTION_UNAVAILABLE, error=error)

    @classmethod
    def from_json(cls, payload: object) -> Execution:
        if not isinstance(payload, dict):
            raise SummaryValidationError("execution must be an object")
        kind = payload.get("kind")
        if kind not in EXECUTION_KINDS:
            raise SummaryValidationError(f"execution has unknown kind {kind!r}")
        if kind == EXECUTION_AVAILABLE:
            return cls.available(BatchExecutionSummary.from_json(payload.get("summary")))
        if kind == EXECUTION_NOT_STARTED:
            stage = payload.get("stage")
            if not isinstance(stage, str):
                raise SummaryValidationError("execution.not_started stage must be a string")
            return cls.not_started(stage)
        error = BatchError.from_json(payload.get("error"))
        return cls.unavailable(error)


@dataclass(frozen=True, slots=True)
class Delivery:
    """Discord delivery result recorded on the workflow run summary."""

    status: str
    detail: str = ""

    def to_json(self) -> dict[str, str]:
        return {"status": self.status, "detail": self.detail}

    @classmethod
    def from_json(cls, payload: object) -> Delivery:
        if not isinstance(payload, dict):
            raise SummaryValidationError("delivery must be an object")
        status = payload.get("status")
        if status not in DELIVERY_STATUSES:
            raise SummaryValidationError(f"delivery has unknown status {status!r}")
        detail = payload.get("detail", "")
        if not isinstance(detail, str):
            raise SummaryValidationError("delivery.detail must be a string")
        return cls(status=status, detail=_sanitize_message(detail))


@dataclass(frozen=True, slots=True)
class WorkflowRunSummary:
    """Terminal workflow summary finalized once by the notification step."""

    schema_version: int
    workflow: str
    repository: str
    trigger: str
    run_attempt: str
    run_url: str
    asof: str | None
    # When this summary was composed, i.e. when the run reached its terminal
    # state. Without it a reader cannot tell a fresh run from the last one that
    # managed to publish a summary — and the object is a single overwritten key
    # whose writer is a best-effort step, so it does go stale silently.
    finished_at: str
    duration_seconds: float
    overall_outcome: str
    publish_state: str
    execution: Execution
    delivery: Delivery
    workflow_errors: tuple[BatchError, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "workflow": self.workflow,
            "repository": self.repository,
            "trigger": self.trigger,
            "run_attempt": self.run_attempt,
            "run_url": self.run_url,
            "asof": self.asof,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "overall_outcome": self.overall_outcome,
            "publish_state": self.publish_state,
            "execution": self.execution.to_json(),
            "delivery": self.delivery.to_json(),
            "workflow_errors": [error.to_json() for error in self.workflow_errors],
        }

    @classmethod
    def from_json(cls, payload: object) -> WorkflowRunSummary:
        if not isinstance(payload, dict):
            raise SummaryValidationError("workflow run summary must be an object")
        if payload.get("schema_version") != WORKFLOW_SUMMARY_SCHEMA_VERSION:
            raise SummaryValidationError(
                f"unsupported workflow summary schema_version {payload.get('schema_version')!r}"
            )
        for key in ("workflow", "repository", "trigger", "run_attempt", "run_url", "finished_at"):
            if not isinstance(payload.get(key), str) or not payload.get(key):
                raise SummaryValidationError(f"workflow summary {key} must be a non-empty string")
        run_url = payload["run_url"]
        # The UI renders this as an anchor href, so nothing but https may reach it.
        if not run_url.startswith("https://"):
            raise SummaryValidationError("workflow summary run_url must be an https URL")
        asof = payload.get("asof")
        if asof is not None and not isinstance(asof, str):
            raise SummaryValidationError("workflow summary asof must be a string or null")
        duration = payload.get("duration_seconds")
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(duration)
        ):
            raise SummaryValidationError("workflow summary duration_seconds must be finite")
        overall_outcome = payload.get("overall_outcome")
        if overall_outcome not in OUTCOMES:
            raise SummaryValidationError(
                f"workflow summary unknown overall_outcome {overall_outcome!r}"
            )
        publish_state = payload.get("publish_state")
        if publish_state not in PUBLISH_STATES:
            raise SummaryValidationError(
                f"workflow summary unknown publish_state {publish_state!r}"
            )
        allowed = _ALLOWED_PUBLISH_BY_OUTCOME[overall_outcome]
        if publish_state not in allowed:
            raise SummaryValidationError(
                f"overall_outcome {overall_outcome!r} cannot pair with "
                f"publish_state {publish_state!r}"
            )
        errors_raw = payload.get("workflow_errors")
        if not isinstance(errors_raw, list):
            raise SummaryValidationError("workflow summary workflow_errors must be a list")
        workflow_errors = tuple(BatchError.from_json(item) for item in errors_raw)
        return cls(
            schema_version=WORKFLOW_SUMMARY_SCHEMA_VERSION,
            workflow=payload["workflow"],
            repository=payload["repository"],
            trigger=payload["trigger"],
            run_attempt=payload["run_attempt"],
            run_url=run_url,
            asof=asof,
            finished_at=payload["finished_at"],
            duration_seconds=float(duration),
            overall_outcome=overall_outcome,
            publish_state=publish_state,
            execution=Execution.from_json(payload.get("execution")),
            delivery=Delivery.from_json(payload.get("delivery")),
            workflow_errors=workflow_errors,
        )


def load_batch_execution_summary(path: Path) -> BatchExecutionSummary:
    """Load and validate a pre-upload batch summary; raise when missing/invalid.

    A missing/unreadable file maps to ``summary_missing``; a present file that is
    not valid JSON maps to ``malformed_json`` — keeping the two failure modes
    distinct for the notification contract.
    """

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SummaryValidationError("summary_missing", reason="summary_missing") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SummaryValidationError("malformed_json", reason="malformed_json") from exc
    return BatchExecutionSummary.from_json(payload)


def order_errors_for_display(errors: Sequence[BatchError]) -> list[BatchError]:
    """Order errors for Discord display: ``failed`` before ``degraded``.

    Relative occurrence order is preserved within each impact, so the batch's
    occurrence order (and the workflow's step order) survives the reordering.
    """

    failed = [error for error in errors if error.impact == ERROR_IMPACT_FAILED]
    degraded = [error for error in errors if error.impact == ERROR_IMPACT_DEGRADED]
    return failed + degraded
