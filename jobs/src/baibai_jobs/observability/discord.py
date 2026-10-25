"""Discord run notification for the cloud daily batch.

One message per terminal state of ``cloud-daily-batch``. It serves the daily
machine loop's "見せる" role: from one message a reader learns whether the day's
artefacts were published, which step to open when they were not, and the generated
company/model/candidate counts. Generation is distinct from cloud-store reflection.
Detailed metrics and release identifiers stay in the workflow log linked by the Run
line. Shared formatting also serves source receipts and missing-run alerts.

Dependency-free by design: only the Python standard library and no 3.13+ syntax,
so the notification step (and the pre-``setup-python`` smoke check) can import
and run it on the GitHub-hosted runner's system ``python3``.

The webhook URL is read only from the notification step's environment, never from
a CLI argument, and never echoed. Validation failures, timeouts and HTTP errors
report a sanitized reason that omits the URL and any response body. A delivery
failure is printed and the script still exits 0, so notification delivery does
not change the jobs/publication outcome. A failed workflow does not itself prove
that publication never happened; failures can also occur after a pointer switch.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path

WEBHOOK_ENV_VAR = "DISCORD_WEBHOOK_URL"
DEFAULT_TIMEOUT_SECONDS = 10.0
MESSAGE_MAX_CHARS = 2000
_SCALAR_MAX_CHARS = 120

OUTCOME_OK = "ok"
OUTCOME_DEGRADED = "degraded"
OUTCOME_FAILED = "failed"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_LABELS = {
    OUTCOME_OK: "[OK]",
    OUTCOME_DEGRADED: "[DEGRADED]",
    OUTCOME_FAILED: "[FAILED]",
    OUTCOME_CANCELLED: "[CANCELLED]",
}
# Non-batch workflow steps in execution order. The first one that neither
# succeeded nor was skipped names where the reader should look first; a step that
# was cancelled rather than failed still stopped the publish.
STEP_ORDER = (
    "smoke",
    "setup",
    "sync",
    "lease-acquire",
    "pull",
    "hydrate",
    "source-integration",
    "publish-lake",
    "upload-stores",
    "source-seal",
    "source-inputs",
    "source-check",
    "portfolio-views",
    "lease-release",
)

_DISCORD_HOSTS = ("discord.com", "discordapp.com")
_WEBHOOK_PATH_PREFIX = "/api/webhooks/"
# Any C0 control, space, or DEL: a webhook secret pasted with stray whitespace
# would otherwise reach http.client and be quoted (token included) in its error.
_FORBIDDEN_URL_CHARS = re.compile(r"[\x00-\x20\x7f]")


class DeliveryError(RuntimeError):
    """Delivery failed; the message is sanitized (no URL, no response body)."""


Transport = Callable[[str, bytes, float], int]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so a 3xx cannot leave the validated Discord host.

    ``prepare_webhook_url`` validates the initial URL's host; following a redirect
    would skip that check for the ``Location`` target. A redirect therefore raises
    ``HTTPError``, which ``deliver`` reports as a sanitized failure. Discord's
    ``wait=true`` endpoint replies 200 directly, so legitimate delivery never
    needs a redirect.
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


def sanitize_one_line(value: object, max_chars: int = _SCALAR_MAX_CHARS) -> str:
    """Render a value as bounded single-line text.

    Whitespace runs collapse to one space and braces are stripped, so a value
    written by another process can never inject a newline or a format
    placeholder into a rendered message.
    """

    text = re.sub(r"\s+", " ", str(value)).strip()
    text = text.replace("{", "").replace("}", "")
    return text[:max_chars]


def write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
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


def prepare_webhook_url(raw_url: str) -> str:
    """Validate the webhook URL and return it with ``wait=true`` for delivery.

    Rejects an unset URL, a non-HTTPS scheme, a non-Discord host, a non-webhook
    path, and any control character or space. The error never includes the URL or
    its token.
    """

    url = raw_url.strip()
    if not url:
        raise DeliveryError("webhook URL is not configured")
    if _FORBIDDEN_URL_CHARS.search(url):
        raise DeliveryError("webhook URL contains a control character or space")
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        raise DeliveryError("webhook URL is malformed") from None
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


def executor_label(env: Mapping[str, str]) -> str:
    return "GitHub Actions" if env.get("GITHUB_ACTIONS") == "true" else "Local"


def run_url(env: Mapping[str, str]) -> str:
    server = env.get("GITHUB_SERVER_URL", "https://github.com")
    repository = env.get("GITHUB_REPOSITORY", "local/local")
    run_id = env.get("GITHUB_RUN_ID", "")
    if not run_id or not env.get("GITHUB_REPOSITORY"):
        return ""
    attempt = env.get("GITHUB_RUN_ATTEMPT", "1")
    return f"{server}/{repository}/actions/runs/{run_id}/attempts/{attempt}"


def load_notice(path: Path | None) -> dict[str, object]:
    """Read what the batch left for the notifier; anything unreadable reads as absent.

    An absent result can mean no file, an unreadable file, or an invalid payload.
    It does not establish where execution stopped. Exit code and step outcomes
    remain available independently, and unknown notice fields stay unknown.
    """

    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def derive_failed_step(step_outcomes: Mapping[str, str]) -> str:
    """Name the workflow step a reader should open first, or "" when none stands out."""

    for step in STEP_ORDER:
        if step_outcomes.get(step, "skipped") not in {"success", "skipped"}:
            return step
    return ""


def decide_outcome(
    *,
    batch_exit_code: str,
    failed_step: str,
    cancelled: bool,
) -> str:
    """Map the observable workflow state to the one label the message carries."""

    if cancelled:
        return OUTCOME_CANCELLED
    if failed_step == "portfolio-views" and batch_exit_code == "0":
        return OUTCOME_DEGRADED
    if failed_step:
        return OUTCOME_FAILED
    if batch_exit_code == "0":
        return OUTCOME_OK
    return OUTCOME_FAILED


def render_generation(notice: Mapping[str, object]) -> list[str]:
    """Report generation counts without claiming that cloud-store upload succeeded."""

    if notice.get("models_ready") is False:
        return ["モデル統合: 未完了"]
    if notice.get("models_ready") is not True:
        return []
    counts = [notice.get(key) for key in ("company_count", "model_count", "candidate_count")]
    if any(type(value) is not int or value < 0 for value in counts):
        return ["モデル生成: 完了 / 件数未確認"]
    companies, models, candidates = counts
    return [f"モデル生成: {companies}社 / {models}件 / 候補{candidates}社"]


def render_deferred_inputs(report: Mapping[str, object]) -> list[str]:
    extractions = report.get("extractions")
    if not isinstance(extractions, Mapping):
        return []
    lines = []
    for source, result in extractions.items():
        if not isinstance(result, Mapping) or not result.get("deferred_inputs"):
            continue
        reasons = sanitize_one_line(result["deferred_inputs"], 180)
        input_date = sanitize_one_line(result.get("input_asof") or "未取得")
        lines.append(f"入力延期: {sanitize_one_line(source)} / {reasons} / 保存一覧日={input_date}")
    return lines[:4]


def render_run_notice(
    *,
    label: str,
    batch: str,
    asof: str,
    details: list[str],
    url: str,
) -> str:
    """Use one compact layout and keep the Run link when details need trimming."""

    header = (
        f"**{label} {sanitize_one_line(batch)}**\n"
        f"対象日: {sanitize_one_line(asof) or '未確定'}\n"
        f"Executor: {executor_label(os.environ)}"
    )
    footer = f"\n\nRun: <{url}>" if url else "\n\nRun: なし"
    body = "\n\n" + "\n".join(f"- {line}" for line in details) if details else ""
    budget = max(0, MESSAGE_MAX_CHARS - len(header) - len(footer))
    if len(body) > budget:
        body = body[: max(0, budget - 1)].rstrip() + "…"
    return header + body + footer


def step_label(value: str) -> str:
    return {
        "success": "成功",
        "failure": "失敗",
        "cancelled": "取消",
        "skipped": "未実行",
    }.get(value, "未確認")


def render_message(
    *,
    outcome: str,
    asof: str,
    failed_step: str,
    notice: Mapping[str, object],
    url: str,
    integration_report: Mapping[str, object] | None = None,
    step_outcomes: Mapping[str, str] | None = None,
) -> str:
    """Separate workflow outcome, publication steps and generated-model counts."""

    details = []
    if failed_step:
        details.append(f"要確認工程: {sanitize_one_line(failed_step)}")
    if step_outcomes is not None:
        details.append(
            "工程結果: "
            + " / ".join(
                f"{name} {step_label(step_outcomes.get(step, 'unknown'))}"
                for step, name in (
                    ("publish-lake", "Lake公開"),
                    ("upload-stores", "Store反映"),
                    ("portfolio-views", "保有・運用画面更新"),
                )
            )
        )
    if outcome != OUTCOME_CANCELLED:
        generation_notice = notice
        if step_outcomes and step_outcomes.get("publish-lake") in {"failure", "cancelled"}:
            generation_notice = {"models_ready": False}
        details.extend(render_generation(generation_notice))
    details.extend(render_deferred_inputs(integration_report or {}))
    return render_run_notice(
        label=OUTCOME_LABELS[outcome],
        batch="共通統合（cloud-daily-batch）",
        asof=asof,
        details=details,
        url=url,
    )


def deliver(
    raw_url: str,
    message: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    transport: Transport = _urllib_transport,
) -> str | None:
    """POST the message once; return None when delivered, else a sanitized reason."""

    try:
        url = prepare_webhook_url(raw_url)
    except DeliveryError as exc:
        return str(exc)
    body = json.dumps(
        {"content": message, "allowed_mentions": {"parse": []}}, ensure_ascii=False
    ).encode("utf-8")
    try:
        status = transport(url, body, timeout)
    except urllib.error.HTTPError as exc:
        # The HTTP status code is a safe scalar and the one fact that separates a
        # revoked webhook (401/404) from rate limiting (429); nothing else from
        # the response crosses the redaction boundary.
        return f"delivery failed: http {exc.code}"
    except Exception as exc:
        # Deliberately broad: only the exception's *type name* is ever reported, so
        # widening costs no information and closes the redaction boundary. An
        # allowlist of exception types would be fail-open here — anything not
        # listed escapes as a traceback carrying the URL (and its token).
        return f"delivery failed: {type(exc).__name__}"
    if 200 <= status < 300:
        return None
    return f"delivery failed: http {status}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="notify_discord",
        description="notify Discord #batch-runs once about the terminal state of a run",
    )
    parser.add_argument("--notice-path", type=Path, default=None)
    parser.add_argument("--integration-report-path", type=Path, default=None)
    parser.add_argument("--batch-exit-code", type=str, default="")
    for step in STEP_ORDER:
        parser.add_argument(f"--{step}-outcome", type=str, default="skipped")
    parser.add_argument("--cancelled", type=str, default="false")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    return parser


def main(argv: list[str] | None = None, *, transport: Transport = _urllib_transport) -> int:
    args = build_parser().parse_args(argv)
    env = os.environ
    notice = load_notice(args.notice_path)
    step_outcomes = {
        step: getattr(args, f"{step.replace('-', '_')}_outcome") for step in STEP_ORDER
    }
    failed_step = derive_failed_step(step_outcomes)
    if not failed_step:
        if args.batch_exit_code == "":
            # The batch was never reached and no tracked step failed: an untracked
            # one (such as checkout or setup-uv) did.
            failed_step = "pre-batch"
        elif args.batch_exit_code != "0":
            failed_step = sanitize_one_line(notice.get("failed_stage") or "batch")
    outcome = decide_outcome(
        batch_exit_code=args.batch_exit_code,
        failed_step=failed_step,
        cancelled=args.cancelled == "true",
    )
    message = render_message(
        outcome=outcome,
        asof=sanitize_one_line(notice.get("asof") or ""),
        failed_step=failed_step,
        notice=notice if args.batch_exit_code == "0" or not notice else {"models_ready": False},
        url=run_url(env),
        integration_report=load_notice(args.integration_report_path),
        step_outcomes=step_outcomes,
    )
    print(message, flush=True)
    failure = deliver(
        env.get(WEBHOOK_ENV_VAR, ""), message, timeout=args.timeout, transport=transport
    )
    if failure is not None:
        print(f"error: discord notification failed: {failure}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
