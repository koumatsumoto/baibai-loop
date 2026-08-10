"""Read-only decision boundary before a shortlist cycle creates a screening run."""

from __future__ import annotations

import json
import math
import subprocess  # nosec B404
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from baibai_engine.read_api.shortlist import list_shortlist_payloads
from baibai_engine.screening.rule_config import load_screening_rules
from baibai_engine.screening.run_store import (
    RunPublication,
    ScreeningRunReader,
    SelectionPublication,
)

PreflightDecision = Literal[
    "reuse",
    "resume-current-code",
    "rerun-current-code",
    "blocked",
]


@dataclass(frozen=True, slots=True)
class GitState:
    commit: str
    clean: bool


def shortlist_preflight(
    *,
    as_of: date,
    cloud_summary_path: Path,
    runs_db_path: Path,
    app_db_path: Path,
    repo_root: Path,
    previous_run_revision_id: str | None = None,
    git_state: GitState | None = None,
) -> dict[str, object]:
    """Choose reuse or one current-code rerun without mutating retention state."""

    cloud = _load_cloud_screening(cloud_summary_path)
    git = git_state or _git_state(repo_root)
    reader = ScreeningRunReader(runs_db_path)
    default_profile = load_screening_rules().selection.default_profile
    current_code = _resolve_current_code(
        reader,
        as_of=as_of,
        commit=git.commit,
        default_profile=default_profile,
    )
    run = reader.get_run(cloud["run_revision_id"])
    selection = reader.get_selection(cloud["selection_id"])
    run_commit = None if run is None else run.application_git_commit
    cloud_publication_valid = (
        run is not None
        and selection is not None
        and run.as_of_date == as_of.isoformat()
        and selection.run_revision_id == cloud["run_revision_id"]
        and selection.as_of_date == as_of.isoformat()
        and run_commit is not None
        and selection.application_git_commit == git.commit
        and selection.publication_kind == "machine"
        and selection.profile == default_profile
    )
    cloud_matches_head = cloud_publication_valid and run_commit == git.commit
    previous = _resolve_previous(
        reader,
        app_db_path=app_db_path,
        before=as_of,
        explicit_run_revision_id=previous_run_revision_id,
    )
    reasons: list[str] = []

    if cloud["as_of"] != as_of.isoformat():
        reasons.append("cloud batch as-of does not match the requested as-of")
    if cloud["overall_outcome"] not in {
        "succeeded",
        "published_with_deferred_failure",
    }:
        reasons.append("cloud batch did not finish with a reusable outcome")
    if cloud["publish_state"] != "published" or cloud["screening_status"] != "ok":
        reasons.append("cloud screening publication is not complete")
    if not git.clean:
        reasons.append("checked-out worktree is dirty")
    selection_needed = not cloud_matches_head and current_code["status"] != "resolved"
    if selection_needed and previous["status"] in {
        "ambiguous",
        "canonical-unavailable",
        "invalid-explicit",
    }:
        reasons.append("previous publication must be resolved before run or select")
    if not cloud_matches_head and current_code["status"] == "ambiguous":
        reasons.append("multiple current-code publications require an explicit choice")
    current_code_reusable = current_code["status"] in {"resolved", "run-only"}
    if not cloud_matches_head and not current_code_reusable:
        if run is None:
            reasons.append("cloud run is absent from the local run store; pull-runs first")
        if selection is None:
            reasons.append("cloud selection is absent from the local run store; pull-runs first")
        elif (
            selection.run_revision_id != cloud["run_revision_id"]
            or selection.as_of_date != as_of.isoformat()
        ):
            reasons.append("cloud selection does not bind the reported run and as-of")

    if (
        not cloud_matches_head
        and not current_code_reusable
        and run is not None
        and run.as_of_date != as_of.isoformat()
    ):
        reasons.append("cloud run does not match the requested as-of")
    if (
        not cloud_matches_head
        and not current_code_reusable
        and run is not None
        and run_commit is None
    ):
        reasons.append("cloud run has no application commit provenance")

    reusable: dict[str, object]
    if reasons:
        decision: PreflightDecision = "blocked"
        reusable = {
            "source": "cloud-batch",
            "run_revision_id": cloud["run_revision_id"],
            "selection_id": cloud["selection_id"],
            "application_git_commit": run_commit,
        }
    elif cloud_matches_head:
        decision = "reuse"
        reusable = {
            "source": "cloud-batch",
            "run_revision_id": cloud["run_revision_id"],
            "selection_id": cloud["selection_id"],
            "application_git_commit": run_commit,
        }
    elif current_code["status"] == "resolved":
        decision = "reuse"
        reusable = {
            "source": "local-current-code",
            "run_revision_id": current_code["run_revision_id"],
            "selection_id": current_code["selection_id"],
            "application_git_commit": git.commit,
        }
    elif current_code["status"] == "run-only":
        decision = "resume-current-code"
        reusable = {
            "source": "local-current-code",
            "run_revision_id": current_code["run_revision_id"],
            "selection_id": None,
            "application_git_commit": git.commit,
        }
    else:
        decision = "rerun-current-code"
        reusable = {
            "source": "cloud-batch",
            "run_revision_id": cloud["run_revision_id"],
            "selection_id": cloud["selection_id"],
            "application_git_commit": run_commit,
        }

    return {
        "kind": "shortlist-preflight",
        "as_of": as_of.isoformat(),
        "decision": decision,
        "reasons": reasons,
        "cloud_batch": {
            "overall_outcome": cloud["overall_outcome"],
            "publish_state": cloud["publish_state"],
            "as_of": cloud["as_of"],
            "run_url": cloud["run_url"],
        },
        "checked_out": {"commit": git.commit, "clean": git.clean},
        "reusable": reusable,
        "current_code": current_code,
        "previous": previous,
    }


def _load_cloud_screening(path: Path) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"cloud run summary is unavailable: {path}") from error
    except json.JSONDecodeError as error:
        raise ValueError("cloud run summary is not valid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("cloud run summary must be an object")
    _validate_workflow_summary(payload)
    execution = payload.get("execution")
    assert isinstance(execution, dict)
    if execution.get("kind") != "available":
        raise ValueError("cloud run summary has no available batch execution")
    summary = execution.get("summary")
    assert isinstance(summary, dict)
    batches = summary["batches"]
    assert isinstance(batches, list)
    screening = next(item for item in batches if item["batch_name"] == "screening")
    if not isinstance(screening, dict) or not isinstance(screening.get("metrics"), dict):
        raise ValueError("cloud run summary has no screening result")
    metrics = screening["metrics"]
    values: dict[str, object] = {
        "overall_outcome": payload.get("overall_outcome"),
        "publish_state": payload.get("publish_state"),
        "run_url": payload.get("run_url"),
        "as_of": metrics.get("asof"),
        "run_revision_id": metrics.get("run_revision_id"),
        "selection_id": metrics.get("selection_id"),
        "screening_status": screening.get("status"),
        "workflow_as_of": payload.get("asof"),
        "execution_as_of": summary.get("asof"),
    }
    missing = [key for key, value in values.items() if not isinstance(value, str) or not value]
    if missing:
        raise ValueError(f"cloud screening result is missing: {', '.join(missing)}")
    normalized = {key: str(value) for key, value in values.items()}
    if not (normalized["workflow_as_of"] == normalized["execution_as_of"] == normalized["as_of"]):
        raise ValueError("cloud run summary carries contradictory as-of values")
    return normalized


_OUTCOMES = {
    "succeeded",
    "skipped_non_business_day",
    "published_with_deferred_failure",
    "failed",
    "cancelled",
}
_PUBLISH_STATES = {"not_generated", "generated", "upload_failed", "published"}
_BATCH_STATUSES = {"ok", "degraded", "failed", "skipped"}
_BATCH_METRIC_SCHEMA: dict[str, dict[str, type[object]]] = {
    "screening": {
        "asof": str,
        "run_revision_id": str,
        "selection_id": str,
        "universe": int,
        "candidates": int,
        "selected": int,
        "edinet_quarantined_events": int,
        "edinet_quarantined_tickers": int,
        "edinet_quarantine_sample": str,
    },
    "macro": {"target": int, "success": int, "failure": int},
    "serving-export": {
        "local_output": bool,
        "delta_measured": bool,
        "delta_entered": int,
        "delta_entered_tickers": list,
        "delta_exited": int,
        "delta_exited_tickers": list,
        "delta_er_moves": int,
        "delta_holdings": int,
        "delta_macro_flags": int,
        "delta_macro_extremes": int,
        "delta_unavailable": str,
    },
    "prune": {},
    "task-reconcile": {},
}
_ERROR_CODES = {
    "subprocess_failed",
    "command_not_found",
    "calendar_store_missing",
    "calendar_unreadable",
    "calendar_uncovered",
    "invalid_asof",
    "invalid_root",
    "run_view_invalid",
    "select_output_invalid",
    "runs_store_unreadable",
    "summary_missing",
    "summary_invalid",
    "summary_conflict",
    "edinet_summary_invalid",
    "upload_failed",
    "step_failed",
    "batch_failed",
}
_ERROR_STAGES = {
    "smoke",
    "setup",
    "sync",
    "pull-stores",
    "verify-cache-coverage",
    "bootstrap-cache",
    "extract-edinet-metrics",
    "refresh-edinet-documents",
    "refresh-buyback-reports",
    "screening-run",
    "screening-select",
    "task-reconcile-earnings",
    "macro-list",
    "macro-refresh",
    "export-read-models",
    "screening-prune",
    "upload-machine",
    "upload-serving",
    "upload-parallel",
    "publish-serving",
    "batch",
    "pre-batch",
    "summary",
    "notification",
    "calendar",
    "input",
    "root",
}


def _validate_workflow_summary(payload: dict[str, object]) -> None:
    """Apply the producer contract needed before treating a cloud result as authority.

    The engine cannot import the delivery package by architecture contract, so this
    read boundary mirrors the schema fields that establish publication identity and
    terminal consistency. A schema version bump must update both producer and reader.
    """

    if payload.get("schema_version") != 1:
        raise ValueError("cloud run summary has an unsupported schema")
    _require_nonempty_strings(
        payload,
        ("workflow", "repository", "trigger", "run_attempt", "run_url", "finished_at"),
        label="cloud run summary",
    )
    if not str(payload["run_url"]).startswith("https://"):
        raise ValueError("cloud run summary run_url must use https")
    _require_finite_number(payload.get("duration_seconds"), label="workflow duration_seconds")
    overall = payload.get("overall_outcome")
    publish = payload.get("publish_state")
    if overall not in _OUTCOMES or publish not in _PUBLISH_STATES:
        raise ValueError("cloud run summary has an invalid terminal state")
    if overall in {"succeeded", "published_with_deferred_failure"} and publish != "published":
        raise ValueError("cloud run summary terminal state contradicts publish state")
    if overall == "skipped_non_business_day" and publish != "not_generated":
        raise ValueError("cloud run summary terminal state contradicts publish state")
    asof = payload.get("asof")
    if asof is not None and not isinstance(asof, str):
        raise ValueError("cloud run summary asof must be a string or null")
    _validate_errors(payload.get("workflow_errors"), label="cloud workflow")
    delivery = payload.get("delivery")
    if (
        not isinstance(delivery, dict)
        or delivery.get("status") not in {"not_attempted", "delivered", "failed"}
        or not isinstance(delivery.get("detail", ""), str)
    ):
        raise ValueError("cloud run summary delivery is invalid")
    execution = payload.get("execution")
    if not isinstance(execution, dict) or execution.get("kind") != "available":
        raise ValueError("cloud run summary has no available batch execution")
    summary = execution.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("cloud run summary execution payload is missing")
    _validate_execution_summary(summary)
    if (
        overall in {"succeeded", "published_with_deferred_failure"}
        and summary["outcome"] != overall
    ):
        raise ValueError("cloud run summary has contradictory execution outcome")


def _validate_execution_summary(summary: dict[str, object]) -> None:
    if summary.get("schema_version") != 1:
        raise ValueError("cloud batch summary has an unsupported schema")
    _require_nonempty_strings(
        summary,
        ("asof", "outcome", "started_at", "finished_at"),
        label="cloud batch summary",
    )
    if summary["outcome"] not in _OUTCOMES:
        raise ValueError("cloud batch summary has an invalid outcome")
    _require_finite_number(summary.get("duration_seconds"), label="batch duration_seconds")
    if not isinstance(summary.get("local_export"), bool):
        raise ValueError("cloud batch summary local_export must be bool")
    batches = summary.get("batches")
    if not isinstance(batches, list):
        raise ValueError("cloud run summary batches are missing")
    names: list[str] = []
    statuses: set[str] = set()
    for item in batches:
        if not isinstance(item, dict):
            raise ValueError("cloud batch result must be an object")
        _validate_batch_result(item)
        names.append(str(item["batch_name"]))
        statuses.add(str(item["status"]))
    if len(names) != len(set(names)):
        raise ValueError("cloud batch summary has duplicate batch names")
    if "screening" not in names:
        raise ValueError("cloud run summary has no screening result")
    outcome = summary["outcome"]
    if outcome == "succeeded" and not statuses <= {"ok", "skipped"}:
        raise ValueError("cloud batch summary outcome contradicts batch statuses")
    if outcome == "published_with_deferred_failure" and "failed" in statuses:
        raise ValueError("cloud batch summary outcome contradicts batch statuses")


def _validate_batch_result(item: dict[str, object]) -> None:
    name = item.get("batch_name")
    if not isinstance(name, str) or name not in _BATCH_METRIC_SCHEMA:
        raise ValueError(f"cloud batch summary has unknown batch: {name!r}")
    status = item.get("status")
    if status not in _BATCH_STATUSES:
        raise ValueError(f"cloud batch {name!r} has an invalid status")
    datasets = item.get("datasets")
    if not isinstance(datasets, list) or not all(
        isinstance(value, str) and value for value in datasets
    ):
        raise ValueError(f"cloud batch {name!r} datasets are invalid")
    _require_finite_number(item.get("duration_seconds"), label=f"cloud batch {name!r} duration")
    _validate_errors(item.get("errors"), label=f"cloud batch {name!r}")
    metrics = item.get("metrics")
    if not isinstance(metrics, dict):
        raise ValueError(f"cloud batch {name!r} metrics must be an object")
    schema = _BATCH_METRIC_SCHEMA[name]
    if status in {"failed", "skipped"}:
        if metrics:
            raise ValueError(f"cloud batch {name!r} with status {status!r} must have no metrics")
        return
    missing = set(schema) - set(metrics)
    extra = set(metrics) - set(schema)
    if missing or extra:
        raise ValueError(
            f"cloud batch {name!r} metrics mismatch: "
            f"missing={sorted(missing)}, extra={sorted(extra)}"
        )
    for key, expected in schema.items():
        _require_metric_type(name=name, key=key, value=metrics[key], expected=expected)


def _require_metric_type(
    *,
    name: str,
    key: str,
    value: object,
    expected: type[object],
) -> None:
    valid = False
    if expected is bool:
        valid = isinstance(value, bool)
    elif expected is int:
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif expected is str:
        valid = isinstance(value, str)
    elif expected is list:
        valid = isinstance(value, list) and all(isinstance(item, str) for item in value)
    if not valid:
        raise ValueError(f"cloud batch {name!r} metric {key!r} has an invalid type")


def _validate_errors(value: object, *, label: str) -> None:
    if not isinstance(value, list):
        raise ValueError(f"{label} errors must be a list")
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"{label} error must be an object")
        code = item.get("code")
        stage = item.get("stage")
        impact = item.get("impact")
        message = item.get("message")
        if (
            code not in _ERROR_CODES
            or stage not in _ERROR_STAGES
            or impact not in {"failed", "degraded"}
            or not isinstance(message, str)
            or not message.strip()
        ):
            raise ValueError(f"{label} error has an invalid contract")


def _require_nonempty_strings(
    payload: dict[str, object],
    keys: tuple[str, ...],
    *,
    label: str,
) -> None:
    missing = [key for key in keys if not isinstance(payload.get(key), str) or not payload.get(key)]
    if missing:
        raise ValueError(f"{label} is missing: {', '.join(missing)}")


def _require_finite_number(value: object, *, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"{label} must be finite")


def _git_state(repo_root: Path) -> GitState:
    commit_before = _git(repo_root, "rev-parse", "HEAD")
    status = _git(repo_root, "status", "--porcelain")
    commit_after = _git(repo_root, "rev-parse", "HEAD")
    return GitState(
        commit=commit_after,
        clean=not status and commit_before == commit_after,
    )


def _git(repo_root: Path, *arguments: str) -> str:
    completed = subprocess.run(  # nosec B603
        ("git", "-C", str(repo_root), *arguments),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise ValueError(f"git {' '.join(arguments)} failed")
    return completed.stdout.strip()


def _resolve_current_code(
    reader: ScreeningRunReader,
    *,
    as_of: date,
    commit: str,
    default_profile: str,
) -> dict[str, object]:
    as_of_text = as_of.isoformat()
    runs = [
        item
        for item in reader.list_runs()
        if item.as_of_date == as_of_text and item.application_git_commit == commit
    ]
    run_by_id = {item.run_revision_id: item for item in runs}
    selections = [
        item
        for item in reader.list_selections(as_of_date=as_of_text)
        if item.run_revision_id in run_by_id
        and item.application_git_commit == commit
        and item.publication_kind == "machine"
        and item.profile == default_profile
    ]
    if len(runs) == 1 and len(selections) == 1:
        selection = selections[0]
        return {
            "status": "resolved",
            "run_revision_id": selection.run_revision_id,
            "selection_id": selection.selection_id,
            "candidates": [_selection_ref(item) for item in selections],
        }
    if len(selections) > 1 or len(runs) > 1:
        return _ambiguous_current_code(runs, selections)
    if len(runs) == 1:
        return {
            "status": "run-only",
            "run_revision_id": runs[0].run_revision_id,
            "selection_id": None,
            "candidates": [_run_ref(runs[0])],
        }
    return {"status": "missing", "run_revision_id": None, "selection_id": None, "candidates": []}


def _ambiguous_current_code(
    runs: list[RunPublication],
    selections: list[SelectionPublication],
) -> dict[str, object]:
    candidates: list[dict[str, object]]
    if selections:
        candidates = [_selection_ref(item) for item in selections]
    else:
        candidates = [_run_ref(item) for item in runs]
    return {
        "status": "ambiguous",
        "run_revision_id": None,
        "selection_id": None,
        "selection_rule": "choose the intended current-code publication explicitly",
        "candidates": candidates,
    }


def _run_ref(item: RunPublication) -> dict[str, object]:
    return {
        "run_revision_id": item.run_revision_id,
        "selection_id": None,
        "created_at": item.run_at,
    }


def _resolve_previous(
    reader: ScreeningRunReader,
    *,
    app_db_path: Path,
    before: date,
    explicit_run_revision_id: str | None,
) -> dict[str, object]:
    before_text = before.isoformat()
    runs = [item for item in reader.list_runs() if item.as_of_date < before_text]
    selections = [item for item in reader.list_selections() if item.as_of_date < before_text]
    canonical_by_as_of: dict[str, dict[str, object]] = {}
    for item in list_shortlist_payloads(app_db_path):
        item_as_of = item.get("as_of")
        if isinstance(item_as_of, str) and item_as_of < before_text:
            # The read API is newest-first, so the first publication is the one
            # shown by the application when a day has multiple revisions.
            canonical_by_as_of.setdefault(item_as_of, item)
    prior_dates = {item.as_of_date for item in runs} | set(canonical_by_as_of)
    if not prior_dates:
        if explicit_run_revision_id is not None:
            return {
                "status": "invalid-explicit",
                "as_of": None,
                "run_revision_id": explicit_run_revision_id,
                "selection_rule": (
                    "the explicit previous run must belong to the greatest prior as-of"
                ),
                "candidates": [],
            }
        return {"status": "missing", "as_of": None, "candidates": []}
    prior_as_of = max(prior_dates)
    candidate_runs = [item for item in runs if item.as_of_date == prior_as_of]
    candidate_selections = [item for item in selections if item.as_of_date == prior_as_of]
    canonical = canonical_by_as_of.get(prior_as_of)
    if canonical is not None:
        canonical_run = canonical.get("run_revision_id")
        if explicit_run_revision_id is not None and explicit_run_revision_id != canonical_run:
            return {
                "status": "invalid-explicit",
                "as_of": prior_as_of,
                "run_revision_id": explicit_run_revision_id,
                "selection_rule": "the explicit previous run must match the canonical shortlist",
                "candidates": [_run_ref(item) for item in candidate_runs],
            }
        resolved_run = next(
            (
                item
                for item in candidate_runs
                if item.run_revision_id == canonical.get("run_revision_id")
            ),
            None,
        )
        if resolved_run is not None:
            return _resolved_previous_run(
                resolved_run,
                source="canonical-shortlist",
                selection_id=_optional_string(canonical.get("selection_id")),
            )
        retained_tickers = _canonical_entry_tickers(canonical)
        shortlist_id = _optional_string(canonical.get("shortlist_id"))
        if retained_tickers and shortlist_id is not None:
            return {
                "status": "resolved-shortlist",
                "as_of": prior_as_of,
                "source": "canonical-shortlist-retained",
                "shortlist_id": shortlist_id,
                "run_revision_id": canonical.get("run_revision_id"),
                "selection_id": canonical.get("selection_id"),
                "candidate_count": len(retained_tickers),
                "selection_arguments": [
                    "--previous-shortlist-id",
                    shortlist_id,
                ],
                "candidates": [_run_ref(item) for item in candidate_runs],
            }
        return {
            "status": "canonical-unavailable",
            "as_of": prior_as_of,
            "run_revision_id": canonical.get("run_revision_id"),
            "selection_id": canonical.get("selection_id"),
            "selection_rule": (
                "do not substitute a non-canonical same-day publication; recover the "
                "canonical selection or use the canonical shortlist's retained longlist"
            ),
            "candidates": [_run_ref(item) for item in candidate_runs],
        }

    if explicit_run_revision_id is not None:
        explicit = next(
            (item for item in candidate_runs if item.run_revision_id == explicit_run_revision_id),
            None,
        )
        if explicit is not None:
            return _resolved_previous_run(explicit, source="explicit-run")
        return {
            "status": "invalid-explicit",
            "as_of": prior_as_of,
            "run_revision_id": explicit_run_revision_id,
            "selection_rule": "the explicit previous run must belong to the greatest prior as-of",
            "candidates": [_run_ref(item) for item in candidate_runs],
        }

    if len(candidate_runs) == 1:
        selection_id = next(
            (
                item.selection_id
                for item in candidate_selections
                if item.run_revision_id == candidate_runs[0].run_revision_id
            ),
            None,
        )
        return _resolved_previous_run(
            candidate_runs[0],
            source="unique-prior-run",
            selection_id=selection_id,
        )
    return {
        "status": "ambiguous",
        "as_of": prior_as_of,
        "selection_rule": (
            "choose the run bound by the application DB canonical shortlist; "
            "otherwise pass --previous-run-revision-id explicitly"
        ),
        "candidates": [_run_ref(item) for item in candidate_runs],
    }


def _resolved_previous_run(
    item: RunPublication,
    *,
    source: str,
    selection_id: str | None = None,
) -> dict[str, object]:
    return {
        "status": "resolved",
        "as_of": item.as_of_date,
        "source": source,
        "run_revision_id": item.run_revision_id,
        "selection_id": selection_id,
        "selection_arguments": ["--previous-run-revision-id", item.run_revision_id],
        "candidates": [_run_ref(item)],
    }


def _canonical_entry_tickers(payload: dict[str, object]) -> tuple[str, ...]:
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return ()
    tickers: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("ticker"), str):
            return ()
        tickers.append(entry["ticker"])
    return tuple(tickers)


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _selection_ref(item: SelectionPublication) -> dict[str, object]:
    return {
        "selection_id": item.selection_id,
        "run_revision_id": item.run_revision_id,
        "created_at": item.created_at,
    }


__all__ = ["GitState", "shortlist_preflight"]
