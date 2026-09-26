from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest

from baibai_batch.observability import discord
from baibai_batch.observability.discord import (
    DeliveryError,
    decide_outcome,
    deliver,
    derive_failed_step,
    load_notice,
    prepare_webhook_url,
    render_message,
    sanitize_one_line,
)

WEBHOOK = "https://discord.com/api/webhooks/123/abc"
RUN_URL = "https://github.com/example/baibai-loop/actions/runs/1/attempts/1"


@pytest.fixture(autouse=True)
def local_executor_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


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


def _notice(path: Path, **overrides: object) -> Path:
    payload: dict[str, object] = {
        "asof": "2026-08-26",
        "skipped": False,
        "failed_stage": None,
        "delta_measured": True,
        "delta_unmeasured_reason": "",
        "entered": ["1001 Alpha E[r]+8.0%"],
        "exited": [],
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# --- webhook URL validation -----------------------------------------------


def test_prepare_webhook_url_accepts_https_discord_webhook_and_adds_wait() -> None:
    assert prepare_webhook_url(WEBHOOK) == WEBHOOK + "?wait=true"


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ("", "not configured"),
        ("http://discord.com/api/webhooks/1/a", "https"),
        ("https://example.com/api/webhooks/1/a", "Discord endpoint"),
        ("https://discord.com/api/other/1/a", "Discord webhook"),
        ("https://discord.com/api/webhooks/1/a b", "control character or space"),
    ],
)
def test_prepare_webhook_url_rejects_invalid(raw: str, match: str) -> None:
    with pytest.raises(DeliveryError, match=match):
        prepare_webhook_url(raw)


def test_prepare_webhook_url_error_never_leaks_the_url() -> None:
    secret = "https://evil.example/api/webhooks/1/SECRETTOKEN"
    with pytest.raises(DeliveryError) as info:
        prepare_webhook_url(secret)
    assert "SECRETTOKEN" not in str(info.value)


def test_delivery_of_a_url_with_stray_whitespace_never_reveals_the_token() -> None:
    transport = FakeTransport()

    failure = deliver(WEBHOOK + "SECRET TOKEN", "hello", transport=transport)

    assert failure is not None
    assert "SECRET" not in failure
    assert transport.calls == []


def test_no_redirect_handler_refuses_redirects() -> None:
    handler = discord._NoRedirect()
    request = discord.urllib.request.Request(WEBHOOK)
    assert handler.redirect_request(request, None, 302, "Found", {}, "https://x/") is None


# --- delivery -------------------------------------------------------------


def test_deliver_returns_none_on_2xx_and_posts_the_message_once() -> None:
    transport = FakeTransport(status=200)

    assert deliver(WEBHOOK, "hello", transport=transport) is None

    ((url, body, _timeout),) = transport.calls
    assert url == WEBHOOK + "?wait=true"
    assert json.loads(body) == {"content": "hello", "allowed_mentions": {"parse": []}}


def test_deliver_reports_the_status_code_of_an_http_error_without_the_url() -> None:
    error = urllib.error.HTTPError(WEBHOOK, 429, "rate limited", None, None)  # type: ignore[arg-type]

    failure = deliver(WEBHOOK, "hello", transport=FakeTransport(error=error))

    assert failure == "delivery failed: http 429"


def test_deliver_reports_only_the_type_of_an_unlisted_transport_exception() -> None:
    class Leaky(Exception):
        def __str__(self) -> str:
            return WEBHOOK + "/SECRETTOKEN"

    failure = deliver(WEBHOOK, "hello", transport=FakeTransport(error=Leaky()))

    assert failure == "delivery failed: Leaky"


def test_deliver_returns_failed_on_non_2xx_without_body() -> None:
    assert deliver(WEBHOOK, "hello", transport=FakeTransport(status=500)) == (
        "delivery failed: http 500"
    )


def test_urllib_transport_identifies_itself_instead_of_the_default_urllib_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    class Response:
        status = 204

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class Opener:
        def open(self, request: discord.urllib.request.Request, *, timeout: float) -> Response:
            seen["agent"] = request.get_header("User-agent")
            return Response()

    monkeypatch.setattr(discord.urllib.request, "build_opener", lambda *_: Opener())

    assert discord._urllib_transport(WEBHOOK, b"{}", 1.0) == 204
    assert seen["agent"] == "baibai-loop-notify/1.0"


# --- outcome and failed step ----------------------------------------------


def test_derive_failed_step_names_the_first_step_that_did_not_succeed_in_order() -> None:
    outcomes = {"smoke": "success", "setup": "success", "pull": "failure", "hydrate": "failure"}
    assert derive_failed_step(outcomes) == "pull"


def test_derive_failed_step_treats_a_cancelled_step_as_where_to_look() -> None:
    assert derive_failed_step({"upload-stores": "cancelled"}) == "upload-stores"
    assert derive_failed_step({"upload-stores": "skipped", "publish-serving": "skipped"}) == ""


@pytest.mark.parametrize(
    ("exit_code", "failed_step", "cancelled", "skipped", "expected"),
    [
        ("0", "", False, False, "ok"),
        ("0", "", False, True, "skipped"),
        ("3", "", False, False, "degraded"),
        ("1", "screening-run", False, False, "failed"),
        ("0", "upload-stores", False, False, "failed"),
        ("3", "publish-serving", False, False, "failed"),
        ("", "pre-batch", False, False, "failed"),
        ("0", "", True, False, "cancelled"),
    ],
)
def test_decide_outcome_table(
    exit_code: str, failed_step: str, cancelled: bool, skipped: bool, expected: str
) -> None:
    assert (
        decide_outcome(
            batch_exit_code=exit_code, failed_step=failed_step, cancelled=cancelled, skipped=skipped
        )
        == expected
    )


# --- message --------------------------------------------------------------


def test_render_message_is_one_headline_the_delta_and_the_run(tmp_path: Path) -> None:
    notice = load_notice(_notice(tmp_path / "n.json", exited=["9001 Zulu E[r]+6.0%"]))

    message = render_message(
        outcome="ok", asof="2026-08-26", failed_step="", notice=notice, url=RUN_URL
    )

    assert message.splitlines() == [
        "[OK] as-of 2026-08-26",
        "executor: Local",
        "🆕 新規 Review Set 入り: 1001 Alpha E[r]+8.0%",
        "👋 Review Set 退出: 9001 Zulu E[r]+6.0%",
        f"run: {RUN_URL}",
    ]


def test_render_message_names_the_failed_step_in_the_headline() -> None:
    message = render_message(
        outcome="failed", asof="", failed_step="hydrate", notice={}, url=RUN_URL
    )

    assert message.splitlines() == [
        "[FAILED] as-of - — failed step: hydrate",
        "executor: Local",
        f"run: {RUN_URL}",
    ]


@pytest.mark.parametrize(
    ("github_actions", "expected"), [("true", "GitHub Actions"), (None, "Local")]
)
def test_render_message_executor_comes_from_runner(monkeypatch, github_actions, expected) -> None:
    if github_actions is None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    else:
        monkeypatch.setenv("GITHUB_ACTIONS", github_actions)
    message = render_message(
        outcome="ok", asof="2026-09-26", failed_step="", notice={}, url=RUN_URL
    )
    assert message.splitlines()[1] == f"executor: {expected}"
    assert message.count("executor:") == 1


def test_render_message_separates_an_unmeasured_delta_from_an_empty_one(tmp_path: Path) -> None:
    empty = load_notice(_notice(tmp_path / "empty.json", entered=[], exited=[]))
    unmeasured = load_notice(
        _notice(
            tmp_path / "unmeasured.json",
            delta_measured=False,
            delta_unmeasured_reason="view_unreadable",
        )
    )

    assert "🆕 新規 Review Set 入り: なし" in render_message(
        outcome="ok", asof="2026-08-26", failed_step="", notice=empty, url=RUN_URL
    )
    assert "🆕 新規 Review Set 入り: 計測なし（view_unreadable）" in render_message(
        outcome="ok", asof="2026-08-26", failed_step="", notice=unmeasured, url=RUN_URL
    )


def test_render_message_has_no_delta_lines_on_a_skipped_day_or_without_a_notice(
    tmp_path: Path,
) -> None:
    skipped = load_notice(_notice(tmp_path / "skipped.json", skipped=True))

    assert "longlist" not in render_message(
        outcome="skipped", asof="2026-08-26", failed_step="", notice=skipped, url=RUN_URL
    )
    assert "longlist" not in render_message(
        outcome="failed", asof="", failed_step="pre-batch", notice={}, url=RUN_URL
    )


def test_render_message_caps_the_named_tickers_and_says_how_many_are_left(tmp_path: Path) -> None:
    entered = [f"{1000 + i} Name{i} E[r]+{9 - i}.0%" for i in range(7)]
    notice = load_notice(_notice(tmp_path / "n.json", entered=entered))

    line = render_message(
        outcome="ok", asof="2026-08-26", failed_step="", notice=notice, url=RUN_URL
    ).splitlines()[2]

    assert "全7件・銘柄コード順で5件表示" in line
    assert line.endswith("（他2件）")
    assert line.count(" / ") == 4


def test_render_message_keeps_a_multiline_entry_from_splitting_the_message(
    tmp_path: Path,
) -> None:
    notice = load_notice(_notice(tmp_path / "n.json", entered=["1001 Evil\nname {x}"]))

    message = render_message(
        outcome="ok", asof="2026-08-26", failed_step="", notice=notice, url=RUN_URL
    )

    assert len(message.splitlines()) == 5
    assert "1001 Evil name x" in message


def test_render_message_is_bounded_for_huge_output(tmp_path: Path) -> None:
    notice = load_notice(_notice(tmp_path / "n.json", entered=["x" * 4000] * 50))

    message = render_message(
        outcome="ok", asof="2026-08-26", failed_step="", notice=notice, url=RUN_URL
    )

    assert len(message) <= discord.MESSAGE_MAX_CHARS


def test_load_notice_reads_anything_unreadable_as_absent(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    (tmp_path / "list.json").write_text("[]", encoding="utf-8")

    assert load_notice(None) == {}
    assert load_notice(tmp_path / "missing.json") == {}
    assert load_notice(broken) == {}
    assert load_notice(tmp_path / "list.json") == {}


def test_sanitize_one_line_collapses_whitespace_and_strips_braces() -> None:
    assert sanitize_one_line("  a\n b\t{c}  ", 5) == "a b c"


# --- main -----------------------------------------------------------------


def _run_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *extra: str,
    notice: Path | None = None,
    webhook: str = WEBHOOK,
    transport: FakeTransport | None = None,
) -> tuple[int, FakeTransport]:
    used = transport if transport is not None else FakeTransport()
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/baibai-loop")
    monkeypatch.setenv("GITHUB_RUN_ID", "1")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    if webhook:
        monkeypatch.setenv(discord.WEBHOOK_ENV_VAR, webhook)
    else:
        monkeypatch.delenv(discord.WEBHOOK_ENV_VAR, raising=False)
    argv = list(extra)
    if notice is not None:
        argv += ["--notice-path", str(notice)]
    return discord.main(argv, transport=used), used


def _sent(transport: FakeTransport) -> str:
    assert len(transport.calls) == 1
    return str(json.loads(transport.calls[0][1].decode("utf-8"))["content"])


def test_main_delivers_the_ok_message_with_the_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    notice = _notice(tmp_path / "notice.json")
    argv = ["--batch-exit-code", "0"]
    for step in discord.STEP_ORDER:
        argv += [f"--{step}-outcome", "success"]

    exit_code, transport = _run_main(tmp_path, monkeypatch, *argv, notice=notice)

    assert exit_code == 0
    message = _sent(transport)
    assert message.startswith(
        "[OK] as-of 2026-08-26\nexecutor: Local\n🆕 新規 Review Set 入り: 1001 Alpha"
    )
    assert message.endswith(f"run: {RUN_URL}")
    # The message is also printed so the run log carries it.
    assert message in capsys.readouterr().out


def test_main_reports_degraded_for_exit_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    notice = _notice(tmp_path / "notice.json", failed_stage="macro-refresh-30d")

    _exit_code, transport = _run_main(
        tmp_path, monkeypatch, "--batch-exit-code", "3", notice=notice
    )

    assert _sent(transport).startswith(
        "[DEGRADED] as-of 2026-08-26 — failed step: macro-refresh-30d"
    )


def test_main_names_the_batch_stage_when_the_batch_itself_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notice = _notice(tmp_path / "notice.json", failed_stage="screening-run")

    _exit_code, transport = _run_main(
        tmp_path, monkeypatch, "--batch-exit-code", "1", notice=notice
    )

    assert _sent(transport).startswith("[FAILED] as-of 2026-08-26 — failed step: screening-run")


def test_main_names_the_workflow_step_that_failed_before_the_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _exit_code, transport = _run_main(
        tmp_path, monkeypatch, "--setup-outcome", "success", "--sync-outcome", "failure"
    )

    assert _sent(transport).startswith("[FAILED] as-of - — failed step: sync")


def test_main_blames_pre_batch_when_no_tracked_step_failed_and_the_batch_never_ran(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _exit_code, transport = _run_main(tmp_path, monkeypatch, "--setup-outcome", "success")

    assert "failed step: pre-batch" in _sent(transport)


def test_main_reports_cancelled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _exit_code, transport = _run_main(tmp_path, monkeypatch, "--cancelled", "true")

    assert _sent(transport).startswith("[CANCELLED]")


def test_main_exits_0_when_delivery_fails_and_says_so_on_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code, transport = _run_main(
        tmp_path, monkeypatch, "--batch-exit-code", "0", transport=FakeTransport(status=500)
    )

    assert exit_code == 0
    assert len(transport.calls) == 1
    assert "discord notification failed: delivery failed: http 500" in capsys.readouterr().err


def test_main_exits_0_without_a_webhook_and_never_calls_the_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code, transport = _run_main(tmp_path, monkeypatch, "--batch-exit-code", "0", webhook="")

    assert exit_code == 0
    assert transport.calls == []
    assert "not configured" in capsys.readouterr().err
