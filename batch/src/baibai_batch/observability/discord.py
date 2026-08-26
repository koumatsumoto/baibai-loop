"""Discord run notification for the cloud daily batch.

One message per terminal state of ``cloud-daily-batch``. It serves the daily
machine loop's "見せる" role: from one message a reader learns whether the day's
artefacts were published, which step to open when they were not, and which names
entered or left the longlist — the only channel that reaches the reader without
being opened. Everything else about a run (durations, per-batch metrics, the lake
release) lives in the workflow log the ``run:`` line points at.

Dependency-free by design: only the Python standard library and no 3.13+ syntax,
so the notification step (and the pre-``setup-python`` smoke check) can import
and run it on the GitHub-hosted runner's system ``python3``.

The webhook URL is read only from the notification step's environment, never from
a CLI argument, and never echoed. Validation failures, timeouts and HTTP errors
report a sanitized reason that omits the URL and any response body. A delivery
failure is printed and the script still exits 0: a red job means the day's
artefacts were not published (``docs/architecture.md`` §Failure policy), and a
broken webhook is not that.
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
ENTERED_TICKERS_SHOWN = 5
_SCALAR_MAX_CHARS = 120
_ENTRY_MAX_CHARS = 48

OUTCOME_OK = "ok"
OUTCOME_SKIPPED = "skipped"
OUTCOME_DEGRADED = "degraded"
OUTCOME_FAILED = "failed"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_LABELS = {
    OUTCOME_OK: "[OK]",
    OUTCOME_SKIPPED: "[SKIPPED]",
    OUTCOME_DEGRADED: "[DEGRADED]",
    OUTCOME_FAILED: "[FAILED]",
    OUTCOME_CANCELLED: "[CANCELLED]",
}
# The batch published the screening result but a deferred step (macro / prune)
# failed afterwards. The job stays green; this label is how the failure reaches
# the reader.
EXIT_DEFERRED_FAILURE = "3"

# Non-batch workflow steps in execution order. The first one that neither
# succeeded nor was skipped names where the reader should look first; a step that
# was cancelled rather than failed still stopped the publish.
STEP_ORDER = (
    "smoke",
    "setup",
    "sync",
    "pull",
    "hydrate",
    "publish-lake",
    "upload-stores",
    "publish-serving",
)

_ENTERED_PREFIX = "🆕 新規 longlist 入り: "
_EXITED_PREFIX = "👋 longlist 退出: "
_DELTA_EMPTY_TEXT = "なし"
_DELTA_UNMEASURED_TEXT = "計測なし"
_DELTA_UNMEASURED_UNKNOWN_REASON = "理由不明"
_DELTA_UNREADABLE_REASON = "metric_unreadable"
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


def run_url(env: Mapping[str, str]) -> str:
    server = env.get("GITHUB_SERVER_URL", "https://github.com")
    repository = env.get("GITHUB_REPOSITORY", "local/local")
    run_id = env.get("GITHUB_RUN_ID", "0")
    attempt = env.get("GITHUB_RUN_ATTEMPT", "1")
    return f"{server}/{repository}/actions/runs/{run_id}/attempts/{attempt}"


def load_notice(path: Path | None) -> dict[str, object]:
    """Read what the batch left for the notifier; anything unreadable reads as absent.

    The batch writes the notice on every terminal path it reaches. No notice means
    the batch never reached one (it was not started, or died before its first
    line), which the exit code and the step outcomes already say.
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
    skipped: bool,
) -> str:
    """Map the observable workflow state to the one label the message carries."""

    if cancelled:
        return OUTCOME_CANCELLED
    if failed_step:
        return OUTCOME_FAILED
    if batch_exit_code == "0":
        return OUTCOME_SKIPPED if skipped else OUTCOME_OK
    if batch_exit_code == EXIT_DEFERRED_FAILURE:
        return OUTCOME_DEGRADED
    return OUTCOME_FAILED


def _render_side(value: object) -> str:
    if not isinstance(value, list):
        return f"{_DELTA_UNMEASURED_TEXT}（{_DELTA_UNREADABLE_REASON}）"
    entries = [text for item in value if (text := sanitize_one_line(item, _ENTRY_MAX_CHARS))]
    if not entries:
        return _DELTA_EMPTY_TEXT
    rendered = " / ".join(entries[:ENTERED_TICKERS_SHOWN])
    if len(entries) > ENTERED_TICKERS_SHOWN:
        rendered += f" (+{len(entries) - ENTERED_TICKERS_SHOWN})"
    return rendered


def render_delta(notice: Mapping[str, object]) -> list[str]:
    """Render both sides of the longlist delta, one line each, whenever a pool exists.

    Silence would carry three different facts — nothing entered, the delta could not
    be measured, and the notification path is broken — and a reader cannot tell them
    apart. So on every run that reached the export the two lines are present and say
    which case it is. A non-business day has no pool and no lines.
    """

    if not notice or notice.get("skipped") is True:
        return []
    if notice.get("delta_measured") is not True:
        reason = sanitize_one_line(
            notice.get("delta_unmeasured_reason") or _DELTA_UNMEASURED_UNKNOWN_REASON,
            _ENTRY_MAX_CHARS,
        )
        text = f"{_DELTA_UNMEASURED_TEXT}（{reason}）"
        return [_ENTERED_PREFIX + text, _EXITED_PREFIX + text]
    return [
        _ENTERED_PREFIX + _render_side(notice.get("entered")),
        _EXITED_PREFIX + _render_side(notice.get("exited")),
    ]


def render_message(
    *,
    outcome: str,
    asof: str,
    failed_step: str,
    notice: Mapping[str, object],
    url: str,
) -> str:
    """Render the bounded (<= 2000 chars) message: one headline, the delta, the run."""

    headline = f"{OUTCOME_LABELS[outcome]} as-of {asof or '-'}"
    if failed_step:
        headline += f" — failed step: {failed_step}"
    lines = [headline, *render_delta(notice), f"run: {url}"]
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
            # one (checkout, setup-uv, the Playwright steps) did.
            failed_step = "pre-batch"
        elif args.batch_exit_code not in {"0", EXIT_DEFERRED_FAILURE}:
            failed_step = sanitize_one_line(notice.get("failed_stage") or "batch")
    outcome = decide_outcome(
        batch_exit_code=args.batch_exit_code,
        failed_step=failed_step,
        cancelled=args.cancelled == "true",
        skipped=notice.get("skipped") is True,
    )
    if outcome == OUTCOME_DEGRADED and not failed_step:
        failed_step = sanitize_one_line(notice.get("failed_stage") or "")
    message = render_message(
        outcome=outcome,
        asof=sanitize_one_line(notice.get("asof") or ""),
        failed_step=failed_step,
        notice=notice,
        url=run_url(env),
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
