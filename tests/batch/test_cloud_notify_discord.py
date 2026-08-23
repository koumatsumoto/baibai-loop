from __future__ import annotations

import dataclasses
import json
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest

from baibai_batch.observability import discord as notify_discord
from baibai_batch.observability.discord import (
    _NON_BATCH_STEPS,
    WEBHOOK_ENV_VAR,
    DeliveryError,
    _NoRedirect,
    build_workflow_summary,
    deliver,
    derive_failed_step,
    derive_publish_state,
    main,
    prepare_webhook_url,
    read_lake_publish_report,
    render_message,
)
from baibai_batch.observability.summary import (
    DELIVERY_DELIVERED,
    DELIVERY_FAILED,
    ERROR_STAGES,
    EXECUTION_AVAILABLE,
    EXECUTION_NOT_STARTED,
    EXECUTION_UNAVAILABLE,
    OUTCOME_CANCELLED,
    OUTCOME_DEGRADED,
    OUTCOME_FAILED,
    OUTCOME_LABELS,
    OUTCOME_SKIPPED,
    OUTCOME_SUCCEEDED,
    PUBLISH_NOT_GENERATED,
    PUBLISH_PUBLISHED,
    PUBLISH_UPLOAD_FAILED,
    WorkflowRunSummary,
    write_json_atomic,
)

ENV = {
    "GITHUB_WORKFLOW": "cloud-daily-batch",
    "GITHUB_REPOSITORY": "koumatsumoto/baibai-loop",
    "GITHUB_EVENT_NAME": "schedule",
    "GITHUB_RUN_ATTEMPT": "1",
    "GITHUB_SERVER_URL": "https://github.com",
    "GITHUB_RUN_ID": "123",
}
VALID_URL = "https://discord.com/api/webhooks/111/secret-token"


def test_read_lake_publish_report_uses_only_a_successful_current_run(tmp_path: Path) -> None:
    report = tmp_path / "lake-publish-report.json"
    report.write_text(
        json.dumps(
            {
                "release_id": "release-1",
                "data_as_of": "2026-08-23",
                "changed_partitions": {"dataset-a": 2, "dataset-b": 3},
                "uploaded_objects": 4,
                "uploaded_bytes": 5,
            }
        ),
        encoding="utf-8",
    )

    summary = read_lake_publish_report(report, outcome="success")

    assert summary is not None
    assert summary.release_id == "release-1"
    assert summary.changed_partitions == 5
    assert read_lake_publish_report(report, outcome="failure") is None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _screening_batch(**overrides) -> dict:
    base = {
        "batch_name": "screening",
        "datasets": ["screening-run", "screening-selection"],
        "status": "ok",
        "duration_seconds": 12.0,
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


def _export_batch(
    entered_tickers: list[str] | None = None,
    exited_tickers: list[str] | None = None,
    **overrides,
) -> dict:
    entered = [] if entered_tickers is None else entered_tickers
    exited = [] if exited_tickers is None else exited_tickers
    base = {
        "batch_name": "serving-export",
        "datasets": ["views", "history"],
        "status": "ok",
        "duration_seconds": 2.0,
        "metrics": {
            "local_output": True,
            "delta_measured": True,
            "delta_entered": len(entered),
            "delta_entered_tickers": entered,
            "delta_exited": len(exited),
            "delta_exited_tickers": exited,
            "delta_er_moves": 0,
            "delta_holdings": 0,
            "delta_macro_flags": 0,
            "delta_macro_extremes": 0,
            "delta_unavailable": "",
        },
        "errors": [],
    }
    base.update(overrides)
    return base


def _without_entered_tickers(summary: WorkflowRunSummary) -> WorkflowRunSummary:
    """The same summary as it would be if the metric key had never been added."""

    execution = summary.execution
    assert execution.summary is not None
    batches = tuple(
        dataclasses.replace(
            batch,
            metrics={
                key: value
                for key, value in batch.metrics.items()
                if key != notify_discord.ENTERED_TICKERS_METRIC
            },
        )
        for batch in execution.summary.batches
    )
    return dataclasses.replace(
        summary,
        execution=dataclasses.replace(
            execution, summary=dataclasses.replace(execution.summary, batches=batches)
        ),
    )


def _write_batch_summary(
    path: Path, *, outcome: str = OUTCOME_SUCCEEDED, local_export: bool = True, batches=None
) -> None:
    payload = {
        "schema_version": 1,
        "asof": "2026-07-21",
        "outcome": outcome,
        "started_at": "2026-07-21T18:30:00+09:00",
        "finished_at": "2026-07-21T18:35:00+09:00",
        "duration_seconds": 300.0,
        "batches": batches if batches is not None else [_screening_batch()],
        "local_export": local_export,
    }
    write_json_atomic(path, payload)


class FakeTransport:
    def __init__(self, status: int = 204, error: Exception | None = None) -> None:
        self.status = status
        self.error = error
        self.calls: list[tuple[str, bytes, float]] = []

    def __call__(self, url: str, body: bytes, timeout: float) -> int:
        self.calls.append((url, body, timeout))
        if self.error is not None:
            raise self.error
        return self.status


# --- webhook URL validation -----------------------------------------------


def test_prepare_webhook_url_accepts_https_discord_webhook_and_adds_wait() -> None:
    prepared = prepare_webhook_url(VALID_URL)
    assert prepared.startswith(VALID_URL)
    assert "wait=true" in prepared


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ("", "not configured"),
        ("http://discord.com/api/webhooks/1/x", "https"),
        ("https://evil.example.com/api/webhooks/1/x", "host"),
        ("https://discord.com/api/not-a-webhook", "path"),
    ],
)
def test_prepare_webhook_url_rejects_invalid(raw: str, match: str) -> None:
    with pytest.raises(DeliveryError, match=match):
        prepare_webhook_url(raw)


def test_prepare_webhook_url_error_never_leaks_the_url() -> None:
    secret_url = "https://discord.com/api/webhooks/111/super-secret-token"
    with pytest.raises(DeliveryError) as excinfo:
        prepare_webhook_url(secret_url.replace("https", "http"))
    assert "super-secret-token" not in str(excinfo.value)
    assert secret_url not in str(excinfo.value)


@pytest.mark.parametrize("suffix", [" ", "\t", "\n", "\x7f"])
def test_prepare_webhook_url_rejects_a_secret_pasted_with_stray_whitespace(
    suffix: str,
) -> None:
    # A URL that keeps a control character reaches http.client, whose InvalidURL
    # quotes the path — token included — as an uncaught traceback.
    with pytest.raises(DeliveryError, match="control character or space"):
        prepare_webhook_url(f"https://discord.com/api/webhooks/111/super-secret-token{suffix}x")


def test_delivery_of_a_url_with_stray_whitespace_never_reveals_the_token(
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = "super-secret-token"
    delivery = deliver(f"https://discord.com/api/webhooks/111/{token} ", "hello")

    assert delivery.status == DELIVERY_FAILED
    assert token not in delivery.detail
    captured = capsys.readouterr()
    assert token not in captured.out
    assert token not in captured.err


def test_delivery_reports_only_the_type_of_an_unlisted_transport_exception() -> None:
    token = "super-secret-token"

    def _raise(url: str, body: bytes, timeout: float) -> int:
        # Not an OSError: an allowlist of exception types would let this escape
        # as a traceback carrying the URL.
        raise RuntimeError(f"boom {url}")

    delivery = deliver(f"https://discord.com/api/webhooks/111/{token}", "hello", transport=_raise)

    assert delivery.status == DELIVERY_FAILED
    assert delivery.detail == "delivery failed: RuntimeError"
    assert token not in delivery.detail


# --- decision table -------------------------------------------------------


UPLOADS_OK = {
    "upload-machine": "success",
    "upload-serving": "success",
    "upload-stores": "success",
    "publish-serving": "success",
}


def _build(
    tmp_path: Path,
    *,
    summary_path: Path | None = None,
    batch_exit_code: str = "",
    local_export: bool = False,
    step_outcomes: dict[str, str] | None = None,
    asof: str = "2026-07-21",
    cancelled: bool = False,
) -> WorkflowRunSummary:
    return build_workflow_summary(
        summary_path=summary_path,
        batch_exit_code=batch_exit_code,
        local_export=local_export,
        step_outcomes=step_outcomes or {},
        asof=asof,
        run_started_at=_now_iso(),
        env=ENV,
        cancelled=cancelled,
    )


# --- derive helpers -------------------------------------------------------


def test_derive_failed_step_returns_first_failed_non_batch_step_in_order() -> None:
    assert derive_failed_step({}) == ""
    assert derive_failed_step({"sync": "failure"}) == "sync"
    assert derive_failed_step({"setup": "success", "pull": "failure"}) == "pull-stores"
    # setup precedes pull even when both fail
    assert derive_failed_step({"setup": "failure", "pull": "failure"}) == "setup"
    # skipped / success are not failures
    assert derive_failed_step({"setup": "skipped", "sync": "success"}) == ""


def test_derive_publish_state_priority() -> None:
    # an upload failure wins over a local export
    assert (
        derive_publish_state(local_export=True, step_outcomes={"upload-machine": "failure"})
        == PUBLISH_UPLOAD_FAILED
    )
    assert derive_publish_state(local_export=True, step_outcomes=UPLOADS_OK) == PUBLISH_PUBLISHED
    assert derive_publish_state(local_export=True, step_outcomes={}) == "generated"
    assert derive_publish_state(local_export=False, step_outcomes={}) == PUBLISH_NOT_GENERATED


def test_every_step_the_notifier_can_name_is_a_nameable_error_stage() -> None:
    """A step name the summary cannot carry turns a failure into no notification.

    `derive_failed_step` feeds `BatchError.build`, which rejects a stage outside
    the allowlist; the rejection propagates out of `main` before the webhook is
    called, so the one failure mode the notification exists to report becomes
    silence. Tracking a step without allowlisting it is the way that happens.
    """
    nameable = {stage for stage, _key in _NON_BATCH_STEPS}
    nameable.add("upload-stores")

    assert nameable <= set(ERROR_STAGES)


def test_a_failed_tail_publish_still_produces_a_notification(tmp_path: Path) -> None:
    summary_path = tmp_path / "batch.json"
    _write_batch_summary(summary_path, outcome=OUTCOME_SUCCEEDED)

    summary = _build(
        tmp_path,
        summary_path=summary_path,
        batch_exit_code="0",
        local_export=True,
        step_outcomes={
            "upload-machine": "success",
            "upload-serving": "success",
            "upload-stores": "success",
            "publish-serving": "failure",
        },
    )

    # Composing the summary is what used to raise: the stage name reached the
    # allowlist check and was rejected, so the notifier exited before delivering.
    assert summary.publish_state == PUBLISH_UPLOAD_FAILED
    assert [error.stage for error in summary.workflow_errors] == ["publish-serving"]


def test_a_cancelled_tail_publish_still_names_where_to_look() -> None:
    assert derive_failed_step({"publish-serving": "cancelled"}) == "publish-serving"


def test_an_upload_step_that_died_before_reporting_names_the_step_that_ran_both() -> None:
    """Which side got further is unknown, so neither branch may be blamed."""
    assert derive_failed_step({"upload-stores": "cancelled"}) == "upload-stores"


def test_views_mirror_failure_alone_is_an_upload_failure() -> None:
    """The mirror runs with `--delete`, so a half-applied one is a changed remote."""
    assert (
        derive_publish_state(
            local_export=True,
            step_outcomes={
                "upload-machine": "success",
                "upload-serving": "failure",
                "upload-stores": "failure",
            },
        )
        == PUBLISH_UPLOAD_FAILED
    )
    assert derive_failed_step({"upload-machine": "success", "upload-serving": "failure"}) == (
        "upload-serving"
    )


def test_a_skipped_mirror_names_the_store_push_rather_than_reading_as_unknown() -> None:
    """The mirror is skipped by design when the store push fails, and that is a
    measurement: the remote `views/` is still the previous run's.

    Left absent it would read as the step dying before it could say anything, which
    is the state where the remote may be half-replaced — the reader would be sent
    after a step that ran both instead of the push that actually failed.
    """
    skipped = {
        "upload-machine": "failure",
        "upload-serving": "skipped",
        "upload-stores": "failure",
    }

    assert derive_failed_step(skipped) == "upload-machine"
    assert derive_publish_state(local_export=True, step_outcomes=skipped) == PUBLISH_UPLOAD_FAILED


def test_an_upload_step_that_died_before_reporting_is_an_upload_failure() -> None:
    """A job timeout or a cancel leaves the step's own outputs unwritten.

    Reading that absence as "uploaded nothing" would describe a run that may have
    replaced part of production as one that never touched it. The step outcome is
    supplied by GitHub on every terminal state, so it is what decides here.
    """
    killed = {"upload-stores": "cancelled"}

    assert derive_publish_state(local_export=True, step_outcomes=killed) == PUBLISH_UPLOAD_FAILED
    assert derive_failed_step(killed) == "upload-stores"


def test_history_and_freshness_not_published_is_not_published(tmp_path: Path) -> None:
    """`published` has to mean the durable record and the freshness claim went out."""
    assert (
        derive_publish_state(
            local_export=True,
            step_outcomes={
                "upload-machine": "success",
                "upload-serving": "success",
                "upload-stores": "success",
                "publish-serving": "skipped",
            },
        )
        == PUBLISH_UPLOAD_FAILED
    )


def test_batch_not_reached_is_failed_not_started(tmp_path: Path) -> None:
    summary = _build(tmp_path, batch_exit_code="", step_outcomes={"sync": "failure"})
    assert summary.overall_outcome == OUTCOME_FAILED
    assert summary.publish_state == PUBLISH_NOT_GENERATED
    assert summary.execution.kind == EXECUTION_NOT_STARTED
    assert summary.execution.stage == "sync"
    assert any(error.stage == "sync" for error in summary.workflow_errors)


def test_non_batch_step_failure_is_failed(tmp_path: Path) -> None:
    summary_path = tmp_path / "batch.json"
    _write_batch_summary(summary_path, outcome=OUTCOME_SUCCEEDED)
    summary = _build(
        tmp_path,
        summary_path=summary_path,
        batch_exit_code="0",
        local_export=True,
        step_outcomes={"upload-machine": "failure", "upload-serving": "skipped"},
    )
    assert summary.overall_outcome == OUTCOME_FAILED
    assert summary.publish_state == PUBLISH_UPLOAD_FAILED


def test_missing_summary_is_unavailable_failed(tmp_path: Path) -> None:
    summary = _build(tmp_path, summary_path=tmp_path / "absent.json", batch_exit_code="1")
    assert summary.execution.kind == EXECUTION_UNAVAILABLE
    assert summary.overall_outcome == OUTCOME_FAILED


def test_malformed_summary_is_unavailable_failed(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    summary = _build(tmp_path, summary_path=bad, batch_exit_code="1")
    assert summary.execution.kind == EXECUTION_UNAVAILABLE
    assert summary.overall_outcome == OUTCOME_FAILED


def test_upload_failure_overrides_successful_batch(tmp_path: Path) -> None:
    summary_path = tmp_path / "batch.json"
    _write_batch_summary(summary_path, outcome=OUTCOME_SUCCEEDED)
    summary = _build(
        tmp_path,
        summary_path=summary_path,
        batch_exit_code="0",
        local_export=True,
        step_outcomes={"upload-machine": "success", "upload-serving": "failure"},
    )
    assert summary.overall_outcome == OUTCOME_FAILED
    assert summary.publish_state == PUBLISH_UPLOAD_FAILED


@pytest.mark.parametrize(
    ("batch_outcome", "exit_code", "expected_outcome", "expected_publish"),
    [
        (OUTCOME_SUCCEEDED, "0", OUTCOME_SUCCEEDED, PUBLISH_PUBLISHED),
        (OUTCOME_DEGRADED, "3", OUTCOME_DEGRADED, PUBLISH_PUBLISHED),
        (OUTCOME_SKIPPED, "0", OUTCOME_SKIPPED, PUBLISH_NOT_GENERATED),
    ],
)
def test_available_batch_maps_summary_outcome(
    tmp_path: Path, batch_outcome: str, exit_code: str, expected_outcome: str, expected_publish: str
) -> None:
    summary_path = tmp_path / "batch.json"
    local_export = batch_outcome != OUTCOME_SKIPPED
    batches = [] if batch_outcome == OUTCOME_SKIPPED else [_screening_batch()]
    _write_batch_summary(
        summary_path, outcome=batch_outcome, local_export=local_export, batches=batches
    )
    summary = _build(
        tmp_path,
        summary_path=summary_path,
        batch_exit_code=exit_code,
        local_export=local_export,
        step_outcomes=UPLOADS_OK if local_export else {},
    )
    assert summary.execution.kind == EXECUTION_AVAILABLE
    assert summary.overall_outcome == expected_outcome
    assert summary.publish_state == expected_publish


def test_summary_contradicting_publish_state_is_failed_with_conflict(tmp_path: Path) -> None:
    # The summary claims succeeded but nothing was published (no local export,
    # uploads skipped): the workflow must report [FAILED] + summary_conflict, not
    # a false [OK]/published.
    summary_path = tmp_path / "batch.json"
    _write_batch_summary(summary_path, outcome=OUTCOME_SUCCEEDED, local_export=True)
    summary = _build(
        tmp_path,
        summary_path=summary_path,
        batch_exit_code="0",
        local_export=False,
        step_outcomes={},  # uploads skipped -> publish_state not_generated
    )
    assert summary.overall_outcome == OUTCOME_FAILED
    assert summary.publish_state == PUBLISH_NOT_GENERATED
    assert any(error.code == "summary_conflict" for error in summary.workflow_errors)


def test_workflow_asof_is_sanitized_before_reaching_the_message(tmp_path: Path) -> None:
    # not_started path uses the free-text workflow asof; a newline must not inject
    # a fake line into the Discord message.
    summary = _build(
        tmp_path,
        batch_exit_code="",
        step_outcomes={"sync": "failure"},
        asof="2026-13-99\n[OK] succeeded fake-injected-line",
    )
    message = render_message(summary)
    assert "fake-injected-line" in message  # the text survives, but...
    assert "\n[OK] succeeded fake-injected-line" not in message  # ...not as its own line
    assert "as-of: 2026-13-99 [OK] succeeded fake-injected-line" in message


def test_missing_summary_file_maps_to_summary_missing_reason(tmp_path: Path) -> None:
    summary = _build(tmp_path, summary_path=tmp_path / "absent.json", batch_exit_code="1")
    assert summary.execution.kind == EXECUTION_UNAVAILABLE
    assert summary.execution.error is not None
    assert "summary_missing" in summary.execution.error.message


def test_malformed_summary_file_maps_to_malformed_json_reason(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    summary = _build(tmp_path, summary_path=bad, batch_exit_code="1")
    assert summary.execution.error is not None
    assert "malformed_json" in summary.execution.error.message


def test_smoke_failure_is_attributed_to_smoke_not_setup(tmp_path: Path) -> None:
    assert derive_failed_step({"smoke": "failure", "setup": "skipped"}) == "smoke"
    summary = _build(tmp_path, batch_exit_code="", step_outcomes={"smoke": "failure"})
    assert summary.execution.kind == EXECUTION_NOT_STARTED
    assert summary.execution.stage == "smoke"


def test_no_redirect_handler_refuses_redirects() -> None:
    handler = _NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example") is None


# --- rendering ------------------------------------------------------------


def test_render_message_contains_required_fields(tmp_path: Path) -> None:
    summary_path = tmp_path / "batch.json"
    screening = _screening_batch()
    screening["metrics"].update(
        {
            "edinet_quarantined_events": 47,
            "edinet_quarantined_tickers": 5,
            "edinet_quarantine_sample": "S100NS9Y:edit:120,S100T65I:edit:120",
        }
    )
    _write_batch_summary(
        summary_path,
        outcome=OUTCOME_SUCCEEDED,
        batches=[screening],
    )
    summary = _build(
        tmp_path,
        summary_path=summary_path,
        batch_exit_code="0",
        local_export=True,
        step_outcomes=UPLOADS_OK,
    )
    message = render_message(summary)
    assert "[OK] succeeded" in message
    assert "cloud-daily-batch" in message
    assert "koumatsumoto/baibai-loop" in message
    assert "schedule" in message
    assert "2026-07-21" in message
    assert "publish: published" in message
    assert "screening ok" in message
    assert "universe=3800" in message
    assert "edinet_quarantined_events=47" in message
    assert "edinet_quarantined_tickers=5" in message
    assert "edinet_quarantine_sample=S100NS9Y:edit:120,S100T65I:edit:120" in message
    assert "https://github.com/koumatsumoto/baibai-loop/actions/runs/123/attempts/1" in message
    assert len(message) <= 2000


def _render_with_entered(
    tmp_path: Path,
    entered_tickers: list[str],
    exited_tickers: list[str] | None = None,
    **export_overrides,
) -> WorkflowRunSummary:
    summary_path = tmp_path / "batch.json"
    _write_batch_summary(
        summary_path,
        outcome=OUTCOME_SUCCEEDED,
        batches=[
            _screening_batch(),
            _export_batch(entered_tickers, exited_tickers, **export_overrides),
        ],
    )
    return _build(
        tmp_path,
        summary_path=summary_path,
        batch_exit_code="0",
        local_export=True,
        step_outcomes=UPLOADS_OK,
    )


def test_render_message_names_the_tickers_on_both_sides_of_the_delta(tmp_path: Path) -> None:
    summary = _render_with_entered(
        tmp_path,
        ["7148 FPG E[r]+18.2%", "4849 EN Japan E[r]+11.0%"],
        ["6088 SIGMAXYZ E[r]+8.4%"],
    )

    message = render_message(summary)

    assert [line for line in message.splitlines() if line.startswith(("🆕", "👋"))] == [
        "🆕 新規 longlist 入り: 7148 FPG E[r]+18.2% / 4849 EN Japan E[r]+11.0%",
        "👋 longlist 退出: 6088 SIGMAXYZ E[r]+8.4%",
    ]
    # The names have their own lines; the batch's scalar run stays as it was.
    assert "delta_entered_tickers=" not in message
    assert "delta_exited_tickers=" not in message
    assert "delta_entered=2" in message
    assert "delta_exited=1" in message


def test_render_message_caps_the_named_tickers_and_says_how_many_are_left(tmp_path: Path) -> None:
    summary = _render_with_entered(tmp_path, [f"100{index} Name{index}" for index in range(7)])

    message = render_message(summary)

    entered_line = next(line for line in message.splitlines() if line.startswith("🆕"))
    assert entered_line.count(" / ") == 4
    assert entered_line.endswith("(+2)")


def test_render_message_says_none_on_a_day_with_no_movement(tmp_path: Path) -> None:
    summary = _render_with_entered(tmp_path, [])

    # Silence would mean "nothing entered", "the delta was not measured" and "the
    # notification path is broken" at once, so a quiet day says so in words.
    assert [
        line for line in render_message(summary).splitlines() if line.startswith(("🆕", "👋"))
    ] == [
        "🆕 新規 longlist 入り: なし",
        "👋 longlist 退出: なし",
    ]


def test_render_message_separates_an_unmeasured_delta_from_an_empty_one(tmp_path: Path) -> None:
    summary = _render_with_entered(
        tmp_path,
        [],
        [],
        metrics={
            "local_output": True,
            "delta_measured": False,
            "delta_entered": 0,
            "delta_entered_tickers": [],
            "delta_exited": 0,
            "delta_exited_tickers": [],
            "delta_er_moves": 0,
            "delta_holdings": 0,
            "delta_macro_flags": 0,
            "delta_macro_extremes": 0,
            "delta_unavailable": "view_unreadable",
        },
    )

    assert [
        line for line in render_message(summary).splitlines() if line.startswith(("🆕", "👋"))
    ] == [
        "🆕 新規 longlist 入り: 計測なし（view_unreadable）",
        "👋 longlist 退出: 計測なし（view_unreadable）",
    ]


def test_render_message_reports_a_missing_ticker_metric_as_unreadable(tmp_path: Path) -> None:
    """A summary this process did not write may still be missing a key."""

    summary = _without_entered_tickers(_render_with_entered(tmp_path, ["7148 FPG"]))

    assert [line for line in render_message(summary).splitlines() if line.startswith("🆕")] == [
        "🆕 新規 longlist 入り: 計測なし（metric_unreadable）",
    ]


def test_render_message_caps_the_exited_names_the_same_way(tmp_path: Path) -> None:
    summary = _render_with_entered(tmp_path, [], [f"200{index} Name{index}" for index in range(7)])

    exited_line = next(
        line for line in render_message(summary).splitlines() if line.startswith("👋")
    )

    assert exited_line.count(" / ") == 4
    assert exited_line.endswith("(+2)")


def test_render_message_keeps_a_multiline_entry_from_splitting_the_message(
    tmp_path: Path,
) -> None:
    # The summary file is written by another process; a newline inside an entry must
    # not be able to forge lines in the notification.
    summary = _render_with_entered(tmp_path, ["7148 FPG\nerrors:\n- [failed] forged"])

    message = render_message(summary)

    assert not any(line.startswith("- [") for line in message.splitlines())
    assert "🆕 新規 longlist 入り: 7148 FPG errors: - [failed] forged" in message


def test_render_message_folds_errors_failed_first_with_remainder(tmp_path: Path) -> None:
    errors = [
        {
            "code": "subprocess_failed",
            "stage": "macro-refresh",
            "impact": "degraded",
            "message": "m1",
        },
        {
            "code": "subprocess_failed",
            "stage": "screening-run",
            "impact": "failed",
            "message": "f1",
        },
        {
            "code": "subprocess_failed",
            "stage": "screening-select",
            "impact": "failed",
            "message": "f2",
        },
        {
            "code": "subprocess_failed",
            "stage": "export-read-models",
            "impact": "failed",
            "message": "f3",
        },
        {
            "code": "subprocess_failed",
            "stage": "screening-prune",
            "impact": "degraded",
            "message": "m2",
        },
    ]
    batches = [_screening_batch(status="degraded", errors=errors)]
    summary_path = tmp_path / "batch.json"
    _write_batch_summary(summary_path, outcome=OUTCOME_DEGRADED, batches=batches)
    summary = _build(
        tmp_path,
        summary_path=summary_path,
        batch_exit_code="3",
        local_export=True,
        step_outcomes=UPLOADS_OK,
    )
    message = render_message(summary)
    error_lines = [line for line in message.splitlines() if line.startswith("- [")]
    # failed errors precede degraded errors; only 3 shown + a remainder line.
    assert error_lines[0] == "- [failed] f1"
    assert error_lines[1] == "- [failed] f2"
    assert error_lines[2] == "- [failed] f3"
    assert "- +2 more" in message
    assert "- [degraded] m1" not in message


def test_render_message_is_bounded_for_huge_output(tmp_path: Path) -> None:
    huge_metrics = _screening_batch()
    huge_metrics["metrics"]["run_revision_id"] = "x" * 5000
    # The per-scalar bound in BatchError keeps messages small; here we assert the
    # renderer never exceeds the Discord limit regardless of content.
    batches = [huge_metrics]
    summary_path = tmp_path / "batch.json"
    # run_revision_id over 120 chars is still a valid str metric; write directly.
    write_json_atomic(
        summary_path,
        {
            "schema_version": 1,
            "asof": "2026-07-21",
            "outcome": OUTCOME_SUCCEEDED,
            "started_at": "2026-07-21T18:30:00+09:00",
            "finished_at": "2026-07-21T18:35:00+09:00",
            "duration_seconds": 300.0,
            "batches": batches,
            "local_export": True,
        },
    )
    summary = _build(
        tmp_path,
        summary_path=summary_path,
        batch_exit_code="0",
        local_export=True,
        step_outcomes=UPLOADS_OK,
    )
    assert len(render_message(summary)) <= 2000


# --- delivery (fake transport) --------------------------------------------


def test_deliver_returns_delivered_on_2xx() -> None:
    transport = FakeTransport(status=204)
    result = deliver(VALID_URL, "hello", transport=transport)
    assert result.status == DELIVERY_DELIVERED
    assert len(transport.calls) == 1
    url, body, _ = transport.calls[0]
    assert "wait=true" in url
    payload = json.loads(body.decode("utf-8"))
    assert payload["content"] == "hello"
    assert payload["allowed_mentions"] == {"parse": []}


def test_deliver_returns_failed_on_non_2xx_without_body() -> None:
    transport = FakeTransport(status=500)
    result = deliver(VALID_URL, "hello", transport=transport)
    assert result.status == DELIVERY_FAILED
    assert "http 500" in result.detail


def test_urllib_transport_identifies_itself_instead_of_the_default_urllib_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Cloudflare in front of discord.com answers 403 to "Python-urllib/x.y";
    # delivery only works while the request carries an explicit User-Agent.
    captured: dict[str, str | None] = {}

    class _Response:
        status = 204

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    class _Opener:
        def open(self, request: urllib.request.Request, timeout: float) -> _Response:
            captured["user_agent"] = request.get_header("User-agent")
            return _Response()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: _Opener())
    status = notify_discord._urllib_transport("https://discord.com/api/webhooks/1/x", b"{}", 1.0)

    assert status == 204
    assert captured["user_agent"] == "baibai-loop-notify/1.0"


def test_deliver_reports_the_status_code_of_an_http_error_without_the_url() -> None:
    token = "super-secret-token"
    error = urllib.error.HTTPError(
        f"https://discord.com/api/webhooks/111/{token}", 404, "Not Found", None, None
    )
    transport = FakeTransport(error=error)
    result = deliver(f"https://discord.com/api/webhooks/111/{token}", "hello", transport=transport)
    # The status code is what separates a revoked webhook (401/404) from rate
    # limiting (429); folding HTTPError into the type-name branch hides it.
    assert result.status == DELIVERY_FAILED
    assert result.detail == "delivery failed: http 404"
    assert token not in result.detail


def test_deliver_sanitizes_transport_errors() -> None:
    transport = FakeTransport(error=urllib.error.URLError("connection refused to 1.2.3.4"))
    result = deliver(VALID_URL, "hello", transport=transport)
    assert result.status == DELIVERY_FAILED
    assert "1.2.3.4" not in result.detail
    assert "URLError" in result.detail


def test_deliver_fails_closed_on_invalid_url_without_calling_transport() -> None:
    transport = FakeTransport(status=204)
    result = deliver("", "hello", transport=transport)
    assert result.status == DELIVERY_FAILED
    assert transport.calls == []


# --- main() integration ---------------------------------------------------


def test_main_delivers_and_writes_summary_on_success(tmp_path: Path, monkeypatch) -> None:
    summary_path = tmp_path / "batch.json"
    _write_batch_summary(summary_path, outcome=OUTCOME_SUCCEEDED)
    output = tmp_path / "workflow.json"
    monkeypatch.setenv(WEBHOOK_ENV_VAR, VALID_URL)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    transport = FakeTransport(status=204)

    exit_code = main(
        [
            "--summary-path",
            str(summary_path),
            "--batch-exit-code",
            "0",
            "--local-export",
            "true",
            "--upload-machine-outcome",
            "success",
            "--upload-serving-outcome",
            "success",
            "--upload-step-outcome",
            "success",
            "--publish-serving-outcome",
            "success",
            "--asof",
            "2026-07-21",
            "--run-started-at",
            _now_iso(),
            "--output",
            str(output),
        ],
        transport=transport,
    )

    assert exit_code == 0
    written = WorkflowRunSummary.from_json(json.loads(output.read_text(encoding="utf-8")))
    assert written.overall_outcome == OUTCOME_SUCCEEDED
    assert written.delivery.status == DELIVERY_DELIVERED
    assert len(transport.calls) == 1


def test_main_fails_the_run_when_delivery_fails(tmp_path: Path, monkeypatch) -> None:
    summary_path = tmp_path / "batch.json"
    _write_batch_summary(summary_path, outcome=OUTCOME_SUCCEEDED)
    output = tmp_path / "workflow.json"
    monkeypatch.setenv(WEBHOOK_ENV_VAR, VALID_URL)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    transport = FakeTransport(status=500)

    exit_code = main(
        [
            "--summary-path",
            str(summary_path),
            "--batch-exit-code",
            "0",
            "--local-export",
            "true",
            "--upload-machine-outcome",
            "success",
            "--upload-serving-outcome",
            "success",
            "--upload-step-outcome",
            "success",
            "--publish-serving-outcome",
            "success",
            "--run-started-at",
            _now_iso(),
            "--output",
            str(output),
        ],
        transport=transport,
    )

    # Data processing succeeded but delivery failed: the run still fails, and the
    # written summary records both the successful outcome and the failed delivery.
    assert exit_code == 1
    written = WorkflowRunSummary.from_json(json.loads(output.read_text(encoding="utf-8")))
    assert written.overall_outcome == OUTCOME_SUCCEEDED
    assert written.delivery.status == DELIVERY_FAILED


def test_main_fails_closed_when_webhook_unset(tmp_path: Path, monkeypatch) -> None:
    summary_path = tmp_path / "batch.json"
    _write_batch_summary(summary_path, outcome=OUTCOME_SUCCEEDED)
    output = tmp_path / "workflow.json"
    monkeypatch.delenv(WEBHOOK_ENV_VAR, raising=False)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    transport = FakeTransport(status=204)

    exit_code = main(
        [
            "--summary-path",
            str(summary_path),
            "--batch-exit-code",
            "0",
            "--local-export",
            "true",
            "--upload-machine-outcome",
            "success",
            "--upload-serving-outcome",
            "success",
            "--run-started-at",
            _now_iso(),
            "--output",
            str(output),
        ],
        transport=transport,
    )

    assert exit_code == 1
    assert transport.calls == []
    written = WorkflowRunSummary.from_json(json.loads(output.read_text(encoding="utf-8")))
    assert written.delivery.status == DELIVERY_FAILED


# --- cancellation / timeout ------------------------------------------------


def test_a_cancelled_run_reports_cancelled_rather_than_a_partial_outcome(
    tmp_path: Path,
) -> None:
    # GitHub reports a `timeout-minutes` expiry as a cancellation, so this is the
    # shape a hung batch arrives in: some steps green, no terminal state of its own.
    summary = _build(
        tmp_path,
        batch_exit_code="",
        step_outcomes={"smoke": "success", "setup": "success", "sync": "success"},
        cancelled=True,
    )

    assert summary.overall_outcome == OUTCOME_CANCELLED
    assert OUTCOME_LABELS[summary.overall_outcome] == "[CANCELLED]"
    assert "[CANCELLED]" in render_message(summary)


def test_a_cancelled_run_keeps_the_publish_state_it_reached(tmp_path: Path) -> None:
    summary_path = tmp_path / "batch-summary.json"
    _write_batch_summary(summary_path)
    summary = _build(
        tmp_path,
        summary_path=summary_path,
        batch_exit_code="0",
        local_export=True,
        step_outcomes=UPLOADS_OK,
        cancelled=True,
    )

    assert summary.overall_outcome == OUTCOME_CANCELLED
    assert summary.publish_state == PUBLISH_PUBLISHED


# --- stale-summary defence -------------------------------------------------


def test_the_summary_records_when_the_run_reached_its_terminal_state(
    tmp_path: Path,
) -> None:
    # Without this the System page cannot separate a fresh run from the last one
    # whose upload succeeded.
    before = datetime.now(UTC)
    summary = _build(tmp_path, batch_exit_code="")
    after = datetime.now(UTC)

    finished = datetime.fromisoformat(summary.finished_at)
    assert before <= finished <= after


# --- pre-batch attribution -------------------------------------------------


def test_a_failure_in_an_untracked_step_is_not_blamed_on_setup(tmp_path: Path) -> None:
    # checkout / setup-uv / the Playwright steps are not passed to the notifier.
    # Naming "setup" would send the reader to a step that in fact succeeded.
    summary = _build(
        tmp_path,
        batch_exit_code="",
        step_outcomes={"smoke": "success", "setup": "success", "sync": "success"},
    )

    assert summary.execution.stage == "pre-batch"
    assert [error.stage for error in summary.workflow_errors] == ["pre-batch"]
    assert "pre-batch" in render_message(summary)


def test_an_unavailable_summary_is_not_reported_twice(tmp_path: Path) -> None:
    # The contract error lives on both `execution` and `workflow_errors`; only
    # three errors are shown, so a duplicate would evict a real one.
    summary_path = tmp_path / "batch-summary.json"
    summary_path.write_text("{ not json", encoding="utf-8")
    summary = _build(
        tmp_path, summary_path=summary_path, batch_exit_code="0", step_outcomes=UPLOADS_OK
    )

    message = render_message(summary)
    assert message.count("batch summary is invalid") == 1


def test_a_schema_bump_is_reported_as_a_version_problem_not_a_broken_file(
    tmp_path: Path,
) -> None:
    summary_path = tmp_path / "batch-summary.json"
    _write_batch_summary(summary_path)
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    payload["schema_version"] = 99
    write_json_atomic(summary_path, payload)

    summary = _build(
        tmp_path, summary_path=summary_path, batch_exit_code="0", step_outcomes=UPLOADS_OK
    )

    assert summary.execution.error is not None
    assert "schema_version" in summary.execution.error.message
