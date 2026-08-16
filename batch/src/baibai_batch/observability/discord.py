"""Discord run-notification adapter for the cloud daily batch.

This is a per-workflow notification adapter, not a logging handler and not a
shared notifier: it composes the terminal ``WorkflowRunSummary`` for one workflow
run, renders a bounded Discord message, and POSTs it once to the webhook named by
the ``DISCORD_WEBHOOK_URL`` repository secret. The channel is fixed by the
webhook (``#batch-runs``); this code never selects a channel.

Dependency-free by design: only the Python standard library and no 3.13+ syntax,
so the notification step (and the pre-``setup-python`` smoke check) can import
and run it on the GitHub-hosted runner's system ``python3``.

Secret handling follows the issue contract: the webhook URL is read only from the
notification step's environment, never from a CLI argument, and is never echoed.
Validation failures, timeouts, and HTTP errors exit non-zero with a sanitized
reason that omits the URL and any response body, so a run whose data processing
succeeded still fails loudly when delivery fails.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from baibai_batch.observability.summary import (
    DELIVERY_DELIVERED,
    DELIVERY_FAILED,
    DELIVERY_NOT_ATTEMPTED,
    EXECUTION_AVAILABLE,
    EXECUTION_NOT_STARTED,
    EXECUTION_UNAVAILABLE,
    OUTCOME_CANCELLED,
    OUTCOME_DEGRADED,
    OUTCOME_FAILED,
    OUTCOME_LABELS,
    OUTCOME_SKIPPED,
    OUTCOME_SUCCEEDED,
    PUBLISH_GENERATED,
    PUBLISH_NOT_GENERATED,
    PUBLISH_PUBLISHED,
    PUBLISH_UPLOAD_FAILED,
    WORKFLOW_SUMMARY_SCHEMA_VERSION,
    BatchError,
    Delivery,
    Execution,
    LakeReleaseSummary,
    SummaryValidationError,
    WorkflowRunSummary,
    load_batch_execution_summary,
    order_errors_for_display,
    sanitize_one_line,
    write_json_atomic,
)

WEBHOOK_ENV_VAR = "DISCORD_WEBHOOK_URL"
DEFAULT_TIMEOUT_SECONDS = 10.0
MESSAGE_MAX_CHARS = 2000
ERRORS_SHOWN = 3
# The metrics that name the tickers which entered and left the machine pool. Both
# are lists, so each gets a line of its own instead of the scalar metric run; a
# reader who only sees the notification can start on the day's names from them.
ENTERED_TICKERS_METRIC = "delta_entered_tickers"
EXITED_TICKERS_METRIC = "delta_exited_tickers"
# Whether the export could read the delta view at all, and why not. A zero count and
# an unreadable view are different facts and the line has to say which one it is.
DELTA_MEASURED_METRIC = "delta_measured"
DELTA_UNAVAILABLE_METRIC = "delta_unavailable"
# Keys the message renders in their own line and must not repeat inside a batch's
# scalar metric run.
_METRICS_RENDERED_SEPARATELY = frozenset({ENTERED_TICKERS_METRIC, EXITED_TICKERS_METRIC})
ENTERED_TICKERS_SHOWN = 5
# One entry is already bounded by the producer; bounding it again keeps a summary
# file this process did not write from setting the message's width.
_ENTERED_ENTRY_MAX_CHARS = 48
_ENTERED_TICKERS_PREFIX = "🆕 新規 longlist 入り: "
_EXITED_TICKERS_PREFIX = "👋 longlist 退出: "
_DELTA_EMPTY_TEXT = "なし"
_DELTA_UNMEASURED_TEXT = "計測なし"
_DELTA_UNMEASURED_UNKNOWN_REASON = "理由不明"
_DELTA_UNREADABLE_REASON = "metric_unreadable"
_DISCORD_HOSTS = ("discord.com", "discordapp.com")
_WEBHOOK_PATH_PREFIX = "/api/webhooks/"
# C0 controls, space, and DEL. A URL carrying any of these reaches http.client,
# which raises InvalidURL with the offending path (and therefore the token) in
# its message.
_FORBIDDEN_URL_CHARS = re.compile(r"[\x00-\x20\x7f]")


class DeliveryError(RuntimeError):
    """Delivery failed; the message is sanitized (no URL, no response body)."""


Transport = Callable[[str, bytes, float], int]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so a 3xx cannot leave the validated Discord host.

    ``prepare_webhook_url`` validates the initial URL's host; following a redirect
    would skip that check for the ``Location`` target. A redirect therefore raises
    ``HTTPError`` (a ``URLError``), which ``deliver`` reports as a sanitized
    failure. Discord's ``wait=true`` endpoint replies 200 directly, so legitimate
    delivery never needs a redirect.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


# Cloudflare in front of discord.com rejects urllib's default "Python-urllib/x.y"
# User-Agent with 403 (bot filtering), so the adapter identifies itself explicitly.
_REQUEST_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "baibai-loop-notify/1.0",
}


def _urllib_transport(url: str, body: bytes, timeout: float) -> int:
    request = urllib.request.Request(url, data=body, headers=_REQUEST_HEADERS, method="POST")
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(request, timeout=timeout) as response:  # nosec B310
        return int(response.status)


def prepare_webhook_url(raw_url: str) -> str:
    """Validate the webhook URL and return it with ``wait=true`` for delivery.

    Rejects an unset URL, a non-HTTPS scheme, a non-Discord host, a non-webhook
    path, and any control character or space. The error never includes the URL or
    its token.

    The whitespace check is what keeps the token out of the Actions log: a secret
    pasted with a stray space survives ``urlsplit`` (which only strips leading C0
    bytes and ``\\t\\r\\n``) and reaches ``http.client``, whose ``InvalidURL``
    quotes the offending path — token included.
    """

    url = raw_url.strip()
    if not url:
        raise DeliveryError("webhook URL is not configured")
    if _FORBIDDEN_URL_CHARS.search(url):
        raise DeliveryError("webhook URL contains a control character or space")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise DeliveryError("webhook URL must use https")
    if parsed.hostname not in _DISCORD_HOSTS:
        raise DeliveryError("webhook URL host is not a Discord endpoint")
    if not parsed.path.startswith(_WEBHOOK_PATH_PREFIX):
        raise DeliveryError("webhook URL path is not a Discord webhook")
    query = urllib.parse.urlencode(
        {**urllib.parse.parse_qs(parsed.query), "wait": "true"}, doseq=True
    )
    return urllib.parse.urlunparse(parsed._replace(query=query))


def _github_metadata(env: Mapping[str, str]) -> dict[str, str]:
    server = env.get("GITHUB_SERVER_URL", "https://github.com")
    repository = env.get("GITHUB_REPOSITORY", "local/local")
    run_id = env.get("GITHUB_RUN_ID", "0")
    attempt = env.get("GITHUB_RUN_ATTEMPT", "1")
    return {
        "workflow": env.get("GITHUB_WORKFLOW", "cloud-daily-batch"),
        "repository": repository,
        "trigger": env.get("GITHUB_EVENT_NAME", "unknown"),
        "run_attempt": attempt,
        "run_url": f"{server}/{repository}/actions/runs/{run_id}/attempts/{attempt}",
    }


def _load_execution(
    summary_path: Path | None, batch_exit_code: str, failed_step: str
) -> tuple[Execution, BatchError | None]:
    """Build the execution union from the batch step's reachability and summary.

    Returns the execution and, when the summary is unavailable, the contract error
    describing why.
    """

    if batch_exit_code == "":
        # The batch was never reached. Only the tracked steps can be named; a
        # failure in an untracked one (checkout, setup-uv, the Playwright steps)
        # must not be attributed to "setup", which sends the reader to a step
        # that in fact succeeded.
        return Execution.not_started(failed_step or "pre-batch"), None
    if summary_path is None:
        error = BatchError.summary_invalid(reason="summary_missing")
        return Execution.unavailable(error), error
    try:
        summary = load_batch_execution_summary(summary_path)
    except SummaryValidationError as exc:
        error = BatchError.summary_invalid(reason=exc.reason)
        return Execution.unavailable(error), error
    return Execution.available(summary), None


def decide_outcome(
    *,
    execution: Execution,
    batch_exit_code: str,
    publish_state: str,
    failed_step: str,
) -> tuple[str, str, bool]:
    """Apply the terminal-outcome decision table.

    Returns ``(overall_outcome, publish_state, conflict)``. ``conflict`` is set
    when an available batch summary claims a publish state the observable workflow
    state contradicts (e.g. the summary says succeeded but nothing was published);
    the caller records a ``summary_conflict`` error and reports ``[FAILED]`` so a
    silent publish regression cannot be reported as ``[OK]``.
    """

    if batch_exit_code == "":
        return OUTCOME_FAILED, PUBLISH_NOT_GENERATED, False
    if failed_step:
        return OUTCOME_FAILED, publish_state, False
    if execution.kind == EXECUTION_UNAVAILABLE:
        return OUTCOME_FAILED, publish_state, False
    if publish_state == PUBLISH_UPLOAD_FAILED:
        return OUTCOME_FAILED, publish_state, False
    summary = execution.summary
    if summary is None:
        return OUTCOME_FAILED, publish_state, False
    summary_outcome = summary.outcome
    if summary_outcome == OUTCOME_SKIPPED:
        return OUTCOME_SKIPPED, PUBLISH_NOT_GENERATED, False
    if summary_outcome in (OUTCOME_SUCCEEDED, OUTCOME_DEGRADED):
        # These outcomes assert the run published; the observable publish state
        # must agree, else the summary contradicts the workflow state.
        if publish_state != PUBLISH_PUBLISHED:
            return OUTCOME_FAILED, publish_state, True
        return summary_outcome, PUBLISH_PUBLISHED, False
    return OUTCOME_FAILED, publish_state, False


# Non-batch steps whose failure is a workflow (not batch) failure, in step order.
# The deferred-report step that turns exit 3 into a job failure is intentionally
# excluded: its failure is the batch's deferred signal, already carried by the
# batch exit code and the degraded summary.
_NON_BATCH_STEPS: tuple[tuple[str, str], ...] = (
    ("smoke", "smoke"),
    ("setup", "setup"),
    ("sync", "sync"),
    ("pull-stores", "pull"),
    ("hydrate", "hydrate"),
    ("publish-lake", "publish-lake"),
    ("upload-machine", "upload-machine"),
    ("upload-serving", "upload-serving"),
    ("publish-serving", "publish-serving"),
)

# The store push and the views mirror run inside one step and report themselves
# through its outputs. Those outputs are absent whenever the step did not reach its
# own last lines — a job timeout, a cancel, a dead runner — and the step's GitHub
# outcome is the only account of that run. Reading the absence as "did not upload"
# would report a run that may have half-replaced production views as one that
# published nothing.
_UPLOAD_BRANCH_KEYS = ("upload-machine", "upload-serving")


def _upload_ended_without_reporting(step_outcomes: Mapping[str, str]) -> bool:
    step_outcome = step_outcomes.get("upload-parallel", "skipped")
    if step_outcome in {"skipped", "success"}:
        return False
    return any(step_outcomes.get(key, "") == "" for key in _UPLOAD_BRANCH_KEYS)


def derive_failed_step(step_outcomes: Mapping[str, str]) -> str:
    """Name the step a reader should open first, or "" when none stands out."""

    for stage, key in _NON_BATCH_STEPS:
        if step_outcomes.get(key) == "failure":
            return stage
    if _upload_ended_without_reporting(step_outcomes):
        # Neither side reported, so which one got further is unknown. The step that
        # ran both is the honest answer; naming one branch would send the reader
        # after a push that may have been fine while the mirror was mid-delete.
        return "upload-parallel"
    # A step that was cancelled rather than failed still stopped the publish, and
    # the reader needs somewhere to start.
    if step_outcomes.get("publish-serving", "skipped") not in {"skipped", "success"}:
        return "publish-serving"
    return ""


def derive_publish_state(*, local_export: bool, step_outcomes: Mapping[str, str]) -> str:
    """Derive the R2 publish state from the upload outcomes + local export.

    An upload failure wins, because the remote may be partially updated; so does an
    upload step that ended without saying what it managed, for the same reason.
    Otherwise every stage succeeding means published, a local export that never
    uploaded is generated, and no export is not_generated.
    """

    upload_machine = step_outcomes.get("upload-machine", "skipped")
    upload_serving = step_outcomes.get("upload-serving", "skipped")
    publish_serving = step_outcomes.get("publish-serving", "skipped")
    if "failure" in {upload_machine, upload_serving, publish_serving}:
        return PUBLISH_UPLOAD_FAILED
    if _upload_ended_without_reporting(step_outcomes):
        return PUBLISH_UPLOAD_FAILED
    if upload_machine == "success" and upload_serving == "success":
        if publish_serving == "success":
            return PUBLISH_PUBLISHED
        return PUBLISH_UPLOAD_FAILED
    if local_export:
        return PUBLISH_GENERATED
    return PUBLISH_NOT_GENERATED


def _total_duration_seconds(run_started_at: str) -> float:
    try:
        started = datetime.fromisoformat(run_started_at)
    except ValueError:
        return 0.0
    now = datetime.now(UTC)
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    duration = (now - started).total_seconds()
    return max(duration, 0.0)


def read_lake_release(path: Path | None, *, outcome: str) -> LakeReleaseSummary | None:
    """Read what the publication reported, and only when a publication succeeded.

    The record on disk names the release the local store corresponds to, which the fill
    also writes. Reading it after a skipped or failed publication would report the
    generation the run started from as the one it published.
    """

    if path is None or outcome != "success":
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    changed = payload.get("changed_partitions")
    total = sum(changed.values()) if isinstance(changed, dict) else 0
    try:
        return LakeReleaseSummary.from_json(
            {
                "release_id": payload.get("release_id"),
                "data_as_of": payload.get("data_as_of"),
                "changed_partitions": total,
                "uploaded_objects": payload.get("uploaded_objects"),
                "uploaded_bytes": payload.get("uploaded_bytes"),
            }
        )
    except SummaryValidationError:
        return None


def build_workflow_summary(
    *,
    summary_path: Path | None,
    batch_exit_code: str,
    local_export: bool,
    step_outcomes: Mapping[str, str],
    asof: str,
    run_started_at: str,
    env: Mapping[str, str],
    cancelled: bool = False,
    lake: LakeReleaseSummary | None = None,
) -> WorkflowRunSummary:
    failed_step = derive_failed_step(step_outcomes)
    publish_state = derive_publish_state(local_export=local_export, step_outcomes=step_outcomes)
    execution, contract_error = _load_execution(summary_path, batch_exit_code, failed_step)
    overall_outcome, final_publish_state, conflict = decide_outcome(
        execution=execution,
        batch_exit_code=batch_exit_code,
        publish_state=publish_state,
        failed_step=failed_step,
    )
    if cancelled:
        # An interrupted run never reached a terminal state of its own, so the
        # step outcomes below describe a partial run. Publish state stays as
        # observed: the cut can land before or after either upload.
        overall_outcome = OUTCOME_CANCELLED
    workflow_errors: list[BatchError] = []
    if failed_step:
        workflow_errors.append(
            BatchError.build(code="step_failed", stage=failed_step, impact="failed")
        )
    elif execution.kind == EXECUTION_NOT_STARTED:
        # No tracked step reports the failure, so without this the report names a
        # stage and then shows no error at all.
        workflow_errors.append(
            BatchError.build(
                code="step_failed", stage=execution.stage or "pre-batch", impact="failed"
            )
        )
    if contract_error is not None:
        workflow_errors.append(contract_error)
    if conflict:
        workflow_errors.append(
            BatchError.build(code="summary_conflict", stage="summary", impact="failed")
        )

    # The workflow asof is free-text dispatch input; sanitize it before it can
    # reach the message. The batch summary's asof (when available) is already
    # validated by daily_batch and takes precedence.
    summary_asof: str | None = sanitize_one_line(asof) if asof else None
    if execution.kind == EXECUTION_AVAILABLE and execution.summary is not None:
        summary_asof = execution.summary.asof

    return WorkflowRunSummary(
        schema_version=WORKFLOW_SUMMARY_SCHEMA_VERSION,
        workflow=env.get("GITHUB_WORKFLOW", "cloud-daily-batch"),
        repository=env.get("GITHUB_REPOSITORY", "local/local"),
        trigger=env.get("GITHUB_EVENT_NAME", "unknown"),
        run_attempt=env.get("GITHUB_RUN_ATTEMPT", "1"),
        run_url=_github_metadata(env)["run_url"],
        asof=summary_asof,
        finished_at=datetime.now(UTC).isoformat(),
        duration_seconds=_total_duration_seconds(run_started_at),
        overall_outcome=overall_outcome,
        publish_state=final_publish_state,
        execution=execution,
        delivery=Delivery(status=DELIVERY_NOT_ATTEMPTED),
        workflow_errors=tuple(workflow_errors),
        lake=lake,
    )


def _collect_errors(summary: WorkflowRunSummary) -> list[BatchError]:
    errors: list[BatchError] = list(summary.workflow_errors)
    if summary.execution.kind == EXECUTION_AVAILABLE and summary.execution.summary is not None:
        for batch in summary.execution.summary.batches:
            errors.extend(batch.errors)
    elif summary.execution.kind == EXECUTION_UNAVAILABLE and summary.execution.error is not None:
        errors.append(summary.execution.error)
    # The contract error is held by both `execution` and `workflow_errors`, and
    # only three errors are shown: a duplicate would push a real one out of view.
    unique: list[BatchError] = []
    seen: set[tuple[str, str, str, str]] = set()
    for error in errors:
        key = (error.code, error.stage, error.impact, error.message)
        if key not in seen:
            seen.add(key)
            unique.append(error)
    return order_errors_for_display(unique)


def _format_metrics(metrics: Mapping[str, object]) -> str:
    keys = sorted(set(metrics) - _METRICS_RENDERED_SEPARATELY)
    if not keys:
        return ""
    return " ".join(f"{key}={metrics[key]}" for key in keys)


def _render_delta_tickers(summary: WorkflowRunSummary) -> list[str]:
    """Render both sides of the pool delta, one line each, on every run.

    Silence would carry three different facts — nothing entered, the delta could not
    be measured, and the notification path is broken — and a reader cannot tell them
    apart. So the lines are always present and say which case it is; what changes
    between a quiet day and an actionable one is the text, not whether the line
    exists.

    The lines appear only when the batch summary is available, because that is where
    the metrics live. A run that never got that far already says so in its own line.

    Entries are sanitized here as well as at the producer because the summary file
    is another process's output — a newline in it would otherwise forge lines in the
    message.
    """

    if summary.execution.kind != EXECUTION_AVAILABLE or summary.execution.summary is None:
        return []
    lines: list[str] = []
    for batch in summary.execution.summary.batches:
        metrics = batch.metrics
        if not any(key in metrics for key in (ENTERED_TICKERS_METRIC, EXITED_TICKERS_METRIC)):
            continue
        unmeasured = _delta_unmeasured_reason(metrics)
        lines.append(
            _ENTERED_TICKERS_PREFIX
            + _render_delta_side(metrics, ENTERED_TICKERS_METRIC, unmeasured)
        )
        lines.append(
            _EXITED_TICKERS_PREFIX + _render_delta_side(metrics, EXITED_TICKERS_METRIC, unmeasured)
        )
    return lines


def _delta_unmeasured_reason(metrics: Mapping[str, object]) -> str | None:
    """Return why the delta is unmeasured, or None when it was measured."""

    measured = metrics.get(DELTA_MEASURED_METRIC)
    if measured is not False:
        return None
    reason = metrics.get(DELTA_UNAVAILABLE_METRIC)
    text = sanitize_one_line(reason, _ENTERED_ENTRY_MAX_CHARS) if reason is not None else ""
    return text or _DELTA_UNMEASURED_UNKNOWN_REASON


def _render_delta_side(metrics: Mapping[str, object], metric: str, unmeasured: str | None) -> str:
    if unmeasured is not None:
        return f"{_DELTA_UNMEASURED_TEXT}（{unmeasured}）"
    value = metrics.get(metric)
    if not isinstance(value, list):
        return f"{_DELTA_UNMEASURED_TEXT}（{_DELTA_UNREADABLE_REASON}）"
    entries = [
        text for item in value if (text := sanitize_one_line(item, _ENTERED_ENTRY_MAX_CHARS))
    ]
    if not entries:
        return _DELTA_EMPTY_TEXT
    rendered = " / ".join(entries[:ENTERED_TICKERS_SHOWN])
    if len(entries) > ENTERED_TICKERS_SHOWN:
        rendered += f" (+{len(entries) - ENTERED_TICKERS_SHOWN})"
    return rendered


def _render_error_overview(errors: list[BatchError]) -> list[str]:
    if not errors:
        return []
    lines = ["errors:"]
    for error in errors[:ERRORS_SHOWN]:
        lines.append(f"- [{error.impact}] {error.message}")
    if len(errors) > ERRORS_SHOWN:
        lines.append(f"- +{len(errors) - ERRORS_SHOWN} more")
    return lines


def render_message(summary: WorkflowRunSummary) -> str:
    """Render a deterministic, bounded (<=2000 char) Discord message."""

    label = OUTCOME_LABELS[summary.overall_outcome]
    lines = [
        f"{label} {summary.overall_outcome} — {summary.workflow}",
        f"repo: {summary.repository} | trigger: {summary.trigger} | attempt: {summary.run_attempt}",
        (
            f"as-of: {summary.asof or '-'} | duration: {summary.duration_seconds:.1f}s "
            f"| publish: {summary.publish_state}"
        ),
    ]
    if summary.execution.kind == EXECUTION_AVAILABLE and summary.execution.summary is not None:
        lines.append("batches:")
        for batch in summary.execution.summary.batches:
            metrics = _format_metrics(batch.metrics)
            suffix = f": {metrics}" if metrics else ""
            lines.append(f"- {batch.batch_name} {batch.status}{suffix}")
    elif summary.execution.kind == EXECUTION_NOT_STARTED:
        lines.append(f"batch not started (failed at: {summary.execution.stage})")
    elif summary.execution.kind == EXECUTION_UNAVAILABLE:
        lines.append("batch summary unavailable")
    if summary.lake is not None:
        lake = summary.lake
        lines.append(
            f"lake: {lake.release_id} as-of {lake.data_as_of} | "
            f"changed {lake.changed_partitions} partition(s) | "
            f"uploaded {lake.uploaded_objects} object(s), {lake.uploaded_bytes} bytes"
        )
    lines.extend(_render_delta_tickers(summary))
    lines.extend(_render_error_overview(_collect_errors(summary)))
    lines.append(f"run: {summary.run_url}")

    message = "\n".join(lines)
    if len(message) > MESSAGE_MAX_CHARS:
        message = message[: MESSAGE_MAX_CHARS - 1].rstrip() + "…"
    return message


def deliver(
    raw_url: str,
    message: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    transport: Transport = _urllib_transport,
) -> Delivery:
    """POST the message once; return a sanitized delivery result (no retry)."""

    try:
        url = prepare_webhook_url(raw_url)
    except DeliveryError as exc:
        return Delivery(status=DELIVERY_FAILED, detail=str(exc))
    body = json.dumps(
        {"content": message, "allowed_mentions": {"parse": []}}, ensure_ascii=False
    ).encode("utf-8")
    try:
        status = transport(url, body, timeout)
    except urllib.error.HTTPError as exc:
        # The HTTP status code is a safe scalar and the one fact that separates a
        # revoked webhook (401/404) from rate limiting (429); nothing else from
        # the response crosses the redaction boundary.
        return Delivery(status=DELIVERY_FAILED, detail=f"delivery failed: http {exc.code}")
    except Exception as exc:
        # Deliberately broad: only the exception's *type name* is ever reported, so
        # widening costs no information and closes the redaction boundary. An
        # allowlist of exception types is fail-open here — anything not listed
        # escapes as a traceback carrying the URL (and its token) into the log.
        reason = type(exc).__name__
        return Delivery(status=DELIVERY_FAILED, detail=f"delivery failed: {reason}")
    if 200 <= status < 300:
        return Delivery(status=DELIVERY_DELIVERED)
    return Delivery(status=DELIVERY_FAILED, detail=f"delivery failed: http {status}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="notify_discord",
        description="compose the terminal workflow summary and notify Discord #batch-runs once",
    )
    parser.add_argument("--summary-path", type=Path, default=None)
    parser.add_argument("--batch-exit-code", type=str, default="")
    parser.add_argument("--local-export", type=str, default="false")
    parser.add_argument("--smoke-outcome", type=str, default="skipped")
    parser.add_argument("--setup-outcome", type=str, default="skipped")
    parser.add_argument("--sync-outcome", type=str, default="skipped")
    parser.add_argument("--pull-outcome", type=str, default="skipped")
    parser.add_argument("--upload-machine-outcome", type=str, default="skipped")
    parser.add_argument("--upload-serving-outcome", type=str, default="skipped")
    # The step that runs the two uploads together. Its own outcome is supplied by
    # GitHub on every terminal state, so it is what stands in when the step ended
    # before it could report which side got through.
    parser.add_argument("--upload-parallel-outcome", type=str, default="skipped")
    parser.add_argument("--publish-serving-outcome", type=str, default="skipped")
    parser.add_argument("--hydrate-outcome", type=str, default="skipped")
    parser.add_argument("--publish-lake-outcome", type=str, default="skipped")
    parser.add_argument("--lake-release-path", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="")
    parser.add_argument("--run-started-at", type=str, default="")
    parser.add_argument("--cancelled", type=str, default="false")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    return parser


def main(argv: list[str] | None = None, *, transport: Transport = _urllib_transport) -> int:
    args = build_parser().parse_args(argv)
    env = os.environ
    step_outcomes = {
        "smoke": args.smoke_outcome,
        "setup": args.setup_outcome,
        "sync": args.sync_outcome,
        "pull": args.pull_outcome,
        "hydrate": args.hydrate_outcome,
        "publish-lake": args.publish_lake_outcome,
        "upload-machine": args.upload_machine_outcome,
        "upload-serving": args.upload_serving_outcome,
        "upload-parallel": args.upload_parallel_outcome,
        "publish-serving": args.publish_serving_outcome,
    }
    try:
        summary = build_workflow_summary(
            summary_path=args.summary_path,
            batch_exit_code=args.batch_exit_code,
            local_export=args.local_export == "true",
            step_outcomes=step_outcomes,
            asof=args.asof,
            run_started_at=args.run_started_at,
            env=env,
            cancelled=args.cancelled == "true",
            lake=read_lake_release(args.lake_release_path, outcome=args.publish_lake_outcome),
        )
    except SummaryValidationError as exc:
        print(f"error: cannot compose workflow summary: {exc}", file=sys.stderr)
        return 1
    message = render_message(summary)
    delivery = deliver(
        env.get(WEBHOOK_ENV_VAR, ""), message, timeout=args.timeout, transport=transport
    )
    final_summary = replace(summary, delivery=delivery)
    # Validate what is about to be published, the same way the batch validates the
    # summary it writes. This is also the only production caller of the reader, so
    # the schema rules it encodes stay exercised rather than test-only.
    payload = WorkflowRunSummary.from_json(final_summary.to_json()).to_json()
    write_json_atomic(args.output, payload)
    if delivery.status != DELIVERY_DELIVERED:
        print(f"error: discord notification failed: {delivery.detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
