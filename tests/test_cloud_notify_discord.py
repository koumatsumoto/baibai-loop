from __future__ import annotations

import json
import urllib.error
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tools.cloud.batch_summary import (
    DELIVERY_DELIVERED,
    DELIVERY_FAILED,
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
from tools.cloud.notify_discord import (
    WEBHOOK_ENV_VAR,
    DeliveryError,
    _NoRedirect,
    build_workflow_summary,
    deliver,
    derive_failed_step,
    derive_publish_state,
    main,
    prepare_webhook_url,
    render_message,
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
        },
        "errors": [],
    }
    base.update(overrides)
    return base


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


UPLOADS_OK = {"upload-machine": "success", "upload-serving": "success"}


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
    _write_batch_summary(summary_path, outcome=OUTCOME_SUCCEEDED)
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
    assert "https://github.com/koumatsumoto/baibai-loop/actions/runs/123/attempts/1" in message
    assert len(message) <= 2000


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
