from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from tools.cloud.batch_summary import (
    BATCH_SUMMARY_SCHEMA_VERSION,
    DELIVERY_DELIVERED,
    DELIVERY_NOT_ATTEMPTED,
    ERROR_IMPACT_DEGRADED,
    ERROR_IMPACT_FAILED,
    EXECUTION_AVAILABLE,
    EXECUTION_NOT_STARTED,
    EXECUTION_UNAVAILABLE,
    OUTCOME_DEGRADED,
    OUTCOME_FAILED,
    OUTCOME_LABELS,
    OUTCOME_SKIPPED,
    OUTCOME_SUCCEEDED,
    OUTCOMES,
    PUBLISH_GENERATED,
    PUBLISH_NOT_GENERATED,
    PUBLISH_PUBLISHED,
    PUBLISH_UPLOAD_FAILED,
    BatchError,
    BatchExecutionSummary,
    BatchResult,
    Delivery,
    Execution,
    SummaryValidationError,
    WorkflowRunSummary,
    load_batch_execution_summary,
    order_errors_for_display,
    write_json_atomic,
)


def _screening_result(**overrides) -> dict:
    base = {
        "batch_name": "screening",
        "datasets": ["screening-run", "screening-selection"],
        "status": "ok",
        "duration_seconds": 12.5,
        "metrics": {
            "asof": "2026-07-21",
            "run_revision_id": "rev-1",
            "selection_id": "sel-1",
            "universe": 3800,
            "candidates": 42,
            "selected": 12,
            "edinet_quarantined_events": 0,
            "edinet_quarantined_tickers": 0,
            "edinet_quarantine_sample": "none",
        },
        "errors": [],
    }
    base.update(overrides)
    return base


def _macro_result(**overrides) -> dict:
    base = {
        "batch_name": "macro",
        "datasets": ["macro-series"],
        "status": "ok",
        "duration_seconds": 4.0,
        "metrics": {"target": 30, "success": 30, "failure": 0},
        "errors": [],
    }
    base.update(overrides)
    return base


def _export_result(**overrides) -> dict:
    base = {
        "batch_name": "serving-export",
        "datasets": ["views", "history"],
        "status": "ok",
        "duration_seconds": 2.0,
        "metrics": {
            "local_output": True,
            "delta_measured": True,
            "delta_entered": 1,
            "delta_entered_tickers": ["7148 FPG E[r]+18.2%"],
            "delta_exited": 0,
            "delta_er_moves": 2,
            "delta_holdings": 1,
            "delta_macro_flags": 0,
            "delta_macro_extremes": 0,
            "delta_unavailable": "",
        },
        "errors": [],
    }
    base.update(overrides)
    return base


def _prune_result(**overrides) -> dict:
    base = {
        "batch_name": "prune",
        "datasets": ["runs-store"],
        "status": "ok",
        "duration_seconds": 0.5,
        "metrics": {},
        "errors": [],
    }
    base.update(overrides)
    return base


def _batch_summary_payload(**overrides) -> dict:
    base = {
        "schema_version": BATCH_SUMMARY_SCHEMA_VERSION,
        "asof": "2026-07-21",
        "outcome": OUTCOME_SUCCEEDED,
        "started_at": "2026-07-21T18:30:00+09:00",
        "finished_at": "2026-07-21T18:35:00+09:00",
        "duration_seconds": 300.0,
        "batches": [_screening_result(), _macro_result(), _export_result(), _prune_result()],
        "local_export": True,
    }
    base.update(overrides)
    return base


def _error_payload(**overrides) -> dict:
    base = {
        "code": "subprocess_failed",
        "stage": "macro-refresh",
        "impact": ERROR_IMPACT_DEGRADED,
        "message": "step 'macro-refresh' exited 1",
    }
    base.update(overrides)
    return base


def _workflow_summary_payload(**overrides) -> dict:
    base = {
        "schema_version": 1,
        "workflow": "cloud-daily-batch",
        "repository": "koumatsumoto/baibai-loop",
        "trigger": "schedule",
        "run_attempt": "1",
        "run_url": "https://github.com/koumatsumoto/baibai-loop/actions/runs/1",
        "asof": "2026-07-21",
        "finished_at": "2026-07-21T09:41:12+00:00",
        "duration_seconds": 320.0,
        "overall_outcome": OUTCOME_SUCCEEDED,
        "publish_state": PUBLISH_PUBLISHED,
        "execution": {"kind": EXECUTION_AVAILABLE, "summary": _batch_summary_payload()},
        "delivery": {"status": DELIVERY_NOT_ATTEMPTED, "detail": ""},
        "workflow_errors": [],
    }
    base.update(overrides)
    return base


# --- BatchError: redaction contract ---------------------------------------


def test_error_build_renders_fixed_template_from_scalars() -> None:
    error = BatchError.build(
        code="subprocess_failed", stage="macro-refresh", impact=ERROR_IMPACT_DEGRADED, returncode=1
    )
    assert error.message == "step 'macro-refresh' exited 1"
    assert error.code == "subprocess_failed"
    assert error.stage == "macro-refresh"
    assert error.impact == ERROR_IMPACT_DEGRADED


@pytest.mark.parametrize("field_name", ["code", "stage", "impact"])
def test_error_build_rejects_unknown_allowlist_values(field_name: str) -> None:
    kwargs = {"code": "subprocess_failed", "stage": "macro-refresh", "impact": ERROR_IMPACT_FAILED}
    kwargs[field_name] = "not-allowlisted"
    with pytest.raises(SummaryValidationError, match="unknown"):
        BatchError.build(**kwargs)


def test_error_build_collapses_newlines_and_braces_in_scalars() -> None:
    error = BatchError.build(
        code="invalid_asof",
        stage="input",
        impact=ERROR_IMPACT_FAILED,
        value="2026-13-99\n{stderr}\nprovider down",
    )
    assert "\n" not in error.message
    assert "{" not in error.message
    assert "}" not in error.message
    assert error.message == "asof '2026-13-99 stderr provider down' is not a valid YYYY-MM-DD date"


def test_error_build_message_is_bounded_to_400_chars() -> None:
    error = BatchError.build(
        code="invalid_asof", stage="input", impact=ERROR_IMPACT_FAILED, value="x" * 5000
    )
    assert len(error.message) <= 400


def test_error_build_rejects_missing_template_scalar() -> None:
    with pytest.raises(SummaryValidationError, match="missing scalar"):
        BatchError.build(code="subprocess_failed", stage="batch", impact=ERROR_IMPACT_FAILED)


def test_error_subprocess_failure_helper_validates_stage_and_returncode() -> None:
    error = BatchError.subprocess_failure(stage="screening-run", returncode=2)
    assert error.message == "step 'screening-run' exited 2"
    with pytest.raises(SummaryValidationError, match="unknown error stage"):
        BatchError.subprocess_failure(stage="not-a-stage", returncode=1)
    with pytest.raises(SummaryValidationError, match="returncode"):
        BatchError.subprocess_failure(stage="batch", returncode="1")  # type: ignore[arg-type]
    # bool is an int subclass, so True would otherwise be rendered as exit code 1.
    with pytest.raises(SummaryValidationError, match="returncode"):
        BatchError.subprocess_failure(stage="batch", returncode=True)


def test_error_summary_invalid_helper_rejects_unknown_reason() -> None:
    error = BatchError.summary_invalid(reason="malformed_json")
    assert "malformed_json" in error.message
    with pytest.raises(SummaryValidationError, match="reason"):
        BatchError.summary_invalid(reason="free-form traceback text")


def test_error_from_json_round_trips_and_rejects_unknown_vocabulary() -> None:
    error = BatchError.subprocess_failure(stage="export-read-models", returncode=1)
    assert BatchError.from_json(error.to_json()) == error
    with pytest.raises(SummaryValidationError, match="unknown error code"):
        BatchError.from_json(_error_payload(code="not-a-code"))
    with pytest.raises(SummaryValidationError, match="unknown error stage"):
        BatchError.from_json(_error_payload(stage="not-a-stage"))
    with pytest.raises(SummaryValidationError, match="unknown error impact"):
        BatchError.from_json(_error_payload(impact="catastrophic"))


# --- BatchResult ----------------------------------------------------------


def test_batch_result_round_trips() -> None:
    result = BatchResult.from_json(_screening_result())
    assert result.batch_name == "screening"
    assert result.metrics["selected"] == 12
    assert BatchResult.from_json(result.to_json()) == result


def test_batch_result_rejects_unknown_status() -> None:
    with pytest.raises(SummaryValidationError, match="unknown status"):
        BatchResult.from_json(_screening_result(status="exploded"))


def test_batch_result_rejects_unknown_batch_name() -> None:
    with pytest.raises(SummaryValidationError, match="unknown batch_name"):
        BatchResult.from_json(_screening_result(batch_name="not-a-batch"))


def test_batch_result_rejects_missing_and_extra_metric_keys() -> None:
    missing = _screening_result()
    del missing["metrics"]["selected"]
    with pytest.raises(SummaryValidationError, match="missing keys"):
        BatchResult.from_json(missing)
    extra = _screening_result()
    extra["metrics"]["surprise"] = 1
    with pytest.raises(SummaryValidationError, match="unknown keys"):
        BatchResult.from_json(extra)


def test_batch_result_rejects_bool_masquerading_as_int_metric() -> None:
    payload = _screening_result()
    payload["metrics"]["universe"] = True
    with pytest.raises(SummaryValidationError, match="must be int"):
        BatchResult.from_json(payload)


def test_batch_result_rejects_non_finite_metric() -> None:
    payload = _macro_result()
    payload["metrics"]["success"] = math.inf
    with pytest.raises(SummaryValidationError, match="finite"):
        BatchResult.from_json(payload)
    payload["metrics"]["success"] = math.nan
    with pytest.raises(SummaryValidationError, match="finite"):
        BatchResult.from_json(payload)


def test_batch_result_accepts_scalar_list_metric_but_rejects_non_scalar_items() -> None:
    # General metric validation allows scalar lists; the macro schema declares an
    # int for this key, so a list is still rejected by the per-batch schema check.
    payload = _macro_result()
    payload["metrics"]["success"] = [1, 2, 3]
    with pytest.raises(SummaryValidationError, match="must be int"):
        BatchResult.from_json(payload)


def test_batch_result_requires_the_entered_ticker_names_on_every_export() -> None:
    # The names ride the same strict schema as the counts: a run that stops emitting
    # them has to fail loudly rather than notify a reader with a silent gap.
    payload = _export_result()
    del payload["metrics"]["delta_entered_tickers"]
    with pytest.raises(SummaryValidationError, match="missing keys"):
        BatchResult.from_json(payload)


def test_batch_result_accepts_an_empty_entered_ticker_list() -> None:
    payload = _export_result()
    payload["metrics"]["delta_entered_tickers"] = []
    assert BatchResult.from_json(payload).metrics["delta_entered_tickers"] == []


@pytest.mark.parametrize("value", ["7148 FPG", 3, ["7148 FPG", 3], [None], [True]])
def test_batch_result_rejects_entered_ticker_names_that_are_not_strings(value: object) -> None:
    # The renderer prints these items as text; anything else would reach a Discord
    # message as a repr.
    payload = _export_result()
    payload["metrics"]["delta_entered_tickers"] = value
    with pytest.raises(SummaryValidationError, match="must be a list of str"):
        BatchResult.from_json(payload)


def test_batch_result_failed_status_requires_empty_metrics() -> None:
    failed = _screening_result(status="failed", metrics={})
    result = BatchResult.from_json(failed)
    assert result.status == "failed"
    assert result.metrics == {}

    failed_with_metrics = _screening_result(status="failed")
    with pytest.raises(SummaryValidationError, match="empty metrics"):
        BatchResult.from_json(failed_with_metrics)


def test_batch_result_errors_preserve_occurrence_order() -> None:
    errors = [
        _error_payload(stage="macro-list", message="step 'macro-list' exited 1"),
        _error_payload(stage="macro-refresh", message="step 'macro-refresh' exited 1"),
        _error_payload(stage="screening-prune", message="step 'screening-prune' exited 1"),
    ]
    result = BatchResult.from_json(_macro_result(errors=errors))
    assert [error.stage for error in result.errors] == [
        "macro-list",
        "macro-refresh",
        "screening-prune",
    ]


# --- BatchExecutionSummary ------------------------------------------------


def test_batch_execution_summary_round_trips() -> None:
    summary = BatchExecutionSummary.from_json(_batch_summary_payload())
    assert summary.outcome == OUTCOME_SUCCEEDED
    assert summary.local_export is True
    assert [batch.batch_name for batch in summary.batches] == [
        "screening",
        "macro",
        "serving-export",
        "prune",
    ]
    assert BatchExecutionSummary.from_json(summary.to_json()) == summary


def test_batch_execution_summary_rejects_delivery_state() -> None:
    payload = _batch_summary_payload()
    payload["delivery"] = {"status": DELIVERY_DELIVERED, "detail": ""}
    with pytest.raises(SummaryValidationError, match="delivery"):
        BatchExecutionSummary.from_json(payload)


def test_batch_execution_summary_rejects_unknown_outcome() -> None:
    with pytest.raises(SummaryValidationError, match="unknown outcome"):
        BatchExecutionSummary.from_json(_batch_summary_payload(outcome="partly_done"))


def test_batch_execution_summary_rejects_duplicate_batch_name() -> None:
    payload = _batch_summary_payload(batches=[_macro_result(), _macro_result()])
    with pytest.raises(SummaryValidationError, match="duplicate batch_name"):
        BatchExecutionSummary.from_json(payload)


def test_batch_execution_summary_rejects_wrong_schema_version() -> None:
    with pytest.raises(SummaryValidationError, match="schema_version"):
        BatchExecutionSummary.from_json(_batch_summary_payload(schema_version=99))


# --- Execution union ------------------------------------------------------


def test_execution_available_round_trips() -> None:
    execution = Execution.from_json(
        {"kind": EXECUTION_AVAILABLE, "summary": _batch_summary_payload()}
    )
    assert execution.kind == EXECUTION_AVAILABLE
    assert execution.summary is not None
    assert execution.to_json()["kind"] == EXECUTION_AVAILABLE


def test_execution_not_started_requires_allowlisted_stage() -> None:
    execution = Execution.not_started("sync")
    assert execution.stage == "sync"
    with pytest.raises(SummaryValidationError, match="unknown not_started stage"):
        Execution.not_started("not-a-stage")


def test_execution_unavailable_carries_typed_error() -> None:
    error = BatchError.summary_invalid(reason="malformed_json")
    execution = Execution.unavailable(error)
    restored = Execution.from_json(execution.to_json())
    assert restored.kind == EXECUTION_UNAVAILABLE
    assert restored.error == error


def test_execution_from_json_rejects_unknown_kind() -> None:
    with pytest.raises(SummaryValidationError, match="unknown kind"):
        Execution.from_json({"kind": "paused"})


# --- Delivery -------------------------------------------------------------


def test_delivery_round_trips_and_sanitizes_detail() -> None:
    delivery = Delivery.from_json({"status": "failed", "detail": "http 500\n<body>secret</body>"})
    assert delivery.status == "failed"
    assert "\n" not in delivery.detail
    assert Delivery.from_json(delivery.to_json()) == delivery


def test_delivery_rejects_unknown_status() -> None:
    with pytest.raises(SummaryValidationError, match="unknown status"):
        Delivery.from_json({"status": "maybe"})


# --- WorkflowRunSummary: outcome / publish state consistency --------------


@pytest.mark.parametrize(
    ("outcome", "publish_state"),
    [
        (OUTCOME_SUCCEEDED, PUBLISH_PUBLISHED),
        (OUTCOME_SKIPPED, PUBLISH_NOT_GENERATED),
        (OUTCOME_DEGRADED, PUBLISH_PUBLISHED),
        (OUTCOME_FAILED, PUBLISH_NOT_GENERATED),
        (OUTCOME_FAILED, PUBLISH_GENERATED),
        (OUTCOME_FAILED, PUBLISH_UPLOAD_FAILED),
        (OUTCOME_FAILED, PUBLISH_PUBLISHED),
    ],
)
def test_workflow_summary_accepts_consistent_outcome_publish_pairs(
    outcome: str, publish_state: str
) -> None:
    payload = _workflow_summary_payload(overall_outcome=outcome, publish_state=publish_state)
    summary = WorkflowRunSummary.from_json(payload)
    assert summary.overall_outcome == outcome
    assert summary.publish_state == publish_state


@pytest.mark.parametrize(
    ("outcome", "publish_state"),
    [
        (OUTCOME_SUCCEEDED, PUBLISH_NOT_GENERATED),
        (OUTCOME_SUCCEEDED, PUBLISH_UPLOAD_FAILED),
        (OUTCOME_SKIPPED, PUBLISH_PUBLISHED),
        (OUTCOME_DEGRADED, PUBLISH_UPLOAD_FAILED),
    ],
)
def test_workflow_summary_rejects_inconsistent_outcome_publish_pairs(
    outcome: str, publish_state: str
) -> None:
    payload = _workflow_summary_payload(overall_outcome=outcome, publish_state=publish_state)
    with pytest.raises(SummaryValidationError, match="cannot pair"):
        WorkflowRunSummary.from_json(payload)


def test_workflow_summary_round_trips_with_workflow_errors_in_step_order() -> None:
    errors = [
        _error_payload(
            stage="setup",
            code="step_failed",
            message="GitHub Actions step failed; open the run log",
        ),
        _error_payload(
            stage="sync", code="step_failed", message="GitHub Actions step failed; open the run log"
        ),
    ]
    payload = _workflow_summary_payload(
        overall_outcome=OUTCOME_FAILED,
        publish_state=PUBLISH_NOT_GENERATED,
        execution={"kind": EXECUTION_NOT_STARTED, "stage": "sync"},
        workflow_errors=errors,
    )
    summary = WorkflowRunSummary.from_json(payload)
    assert [error.stage for error in summary.workflow_errors] == ["setup", "sync"]
    assert WorkflowRunSummary.from_json(summary.to_json()) == summary


def test_workflow_summary_rejects_unknown_overall_outcome() -> None:
    with pytest.raises(SummaryValidationError, match="overall_outcome"):
        WorkflowRunSummary.from_json(_workflow_summary_payload(overall_outcome="weird"))


# --- outcome labels -------------------------------------------------------


def test_every_outcome_has_a_display_label() -> None:
    assert set(OUTCOME_LABELS) == set(OUTCOMES)
    assert OUTCOME_LABELS[OUTCOME_SUCCEEDED] == "[OK]"
    assert OUTCOME_LABELS[OUTCOME_SKIPPED] == "[SKIPPED]"
    assert OUTCOME_LABELS[OUTCOME_DEGRADED] == "[DEGRADED]"
    assert OUTCOME_LABELS[OUTCOME_FAILED] == "[FAILED]"


# --- display ordering -----------------------------------------------------


def test_order_errors_for_display_puts_failed_before_degraded_keeping_order() -> None:
    degraded_1 = BatchError.subprocess_failure(
        stage="macro-refresh", returncode=1, impact=ERROR_IMPACT_DEGRADED
    )
    failed_1 = BatchError.subprocess_failure(stage="screening-run", returncode=1)
    degraded_2 = BatchError.subprocess_failure(
        stage="screening-prune", returncode=1, impact=ERROR_IMPACT_DEGRADED
    )
    failed_2 = BatchError.subprocess_failure(stage="export-read-models", returncode=1)

    ordered = order_errors_for_display([degraded_1, failed_1, degraded_2, failed_2])

    assert ordered == [failed_1, failed_2, degraded_1, degraded_2]


# --- persistence helpers --------------------------------------------------


def test_write_json_atomic_writes_valid_json_and_replaces(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "summary.json"
    write_json_atomic(target, {"a": 1})
    write_json_atomic(target, {"b": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"b": 2}


def test_load_batch_execution_summary_handles_missing_and_malformed(tmp_path: Path) -> None:
    missing = tmp_path / "absent.json"
    with pytest.raises(SummaryValidationError):
        load_batch_execution_summary(missing)

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{not json", encoding="utf-8")
    with pytest.raises(SummaryValidationError):
        load_batch_execution_summary(malformed)

    valid = tmp_path / "valid.json"
    valid.write_text(json.dumps(_batch_summary_payload()), encoding="utf-8")
    summary = load_batch_execution_summary(valid)
    assert summary.outcome == OUTCOME_SUCCEEDED


# --- serving contract -----------------------------------------------------


def test_workflow_summary_json_keys_match_the_ui_interface() -> None:
    """Pin the key set the UI's WorkflowRunSummaryView declares.

    The published object is consumed only by TypeScript that casts rather than
    parses it, and no generator ties the two sides together, so a field added or
    renamed here would otherwise surface as a blank panel in production.
    """

    payload = WorkflowRunSummary.from_json(_workflow_summary_payload()).to_json()

    assert set(payload) == {
        "schema_version",
        "workflow",
        "repository",
        "trigger",
        "run_attempt",
        "run_url",
        "asof",
        "finished_at",
        "duration_seconds",
        "overall_outcome",
        "publish_state",
        "execution",
        "delivery",
        "workflow_errors",
    }
    assert set(payload["execution"]) == {"kind", "summary"}
    assert set(payload["execution"]["summary"]) == {
        "schema_version",
        "asof",
        "outcome",
        "started_at",
        "finished_at",
        "duration_seconds",
        "batches",
        "local_export",
    }
    assert set(payload["execution"]["summary"]["batches"][0]) == {
        "batch_name",
        "datasets",
        "status",
        "duration_seconds",
        "metrics",
        "errors",
    }
    assert set(payload["delivery"]) == {"status", "detail"}


def test_workflow_summary_rejects_a_non_https_run_url() -> None:
    # The UI renders run_url as an anchor href.
    with pytest.raises(SummaryValidationError, match="https"):
        WorkflowRunSummary.from_json(_workflow_summary_payload(run_url="javascript:alert(1)"))


def test_batch_summary_rejects_an_outcome_its_own_batches_contradict() -> None:
    payload = _batch_summary_payload()
    payload["outcome"] = OUTCOME_SUCCEEDED
    # A failed batch carries no metrics (existing rule), so build it that way.
    payload["batches"] = [_screening_result(status="failed", metrics={})]

    with pytest.raises(SummaryValidationError) as excinfo:
        BatchExecutionSummary.from_json(payload)
    assert excinfo.value.reason == "inconsistent_outcome"


def test_batch_summary_reports_a_schema_bump_as_a_version_problem() -> None:
    with pytest.raises(SummaryValidationError) as excinfo:
        BatchExecutionSummary.from_json(_batch_summary_payload(schema_version=99))
    assert excinfo.value.reason == "schema_version"
