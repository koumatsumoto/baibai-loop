from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest

from baibai_jobs.observability import discord
from baibai_jobs.observability.discord import (
    DeliveryError,
    decide_outcome,
    deliver,
    derive_failed_steps,
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
        "failed_stage": None,
        "models_ready": True,
        "company_count": 3686,
        "model_count": 18431,
        "candidate_count": 35,
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


def test_derive_failed_steps_keeps_every_unsuccessful_step_in_order() -> None:
    outcomes = {"smoke": "success", "setup": "success", "pull": "failure", "hydrate": "failure"}
    assert derive_failed_steps(outcomes) == ["pull", "hydrate"]


def test_derive_failed_steps_keeps_cancelled_steps_but_not_skipped_steps() -> None:
    assert derive_failed_steps({"upload-stores": "cancelled"}) == ["upload-stores"]
    assert derive_failed_steps({"upload-stores": "skipped"}) == []


@pytest.mark.parametrize(
    ("exit_code", "failed_steps", "cancelled", "expected"),
    [
        ("0", [], False, "ok"),
        ("3", [], False, "failed"),
        ("1", ["model-integration"], False, "failed"),
        ("0", ["upload-stores"], False, "failed"),
        ("3", ["source-seal"], False, "failed"),
        ("", ["pre-batch"], False, "failed"),
        ("0", [], True, "cancelled"),
        ("1", ["portfolio-views"], False, "failed"),
        ("0", ["portfolio-views", "lease-release"], True, "cancelled"),
    ],
)
def test_decide_outcome_table(
    exit_code: str, failed_steps: list[str], cancelled: bool, expected: str
) -> None:
    assert (
        decide_outcome(batch_exit_code=exit_code, failed_steps=failed_steps, cancelled=cancelled)
        == expected
    )


# --- message --------------------------------------------------------------


def test_render_message_is_one_headline_generation_counts_and_the_run(tmp_path: Path) -> None:
    notice = load_notice(_notice(tmp_path / "n.json"))

    message = render_message(
        outcome="ok", asof="2026-08-26", failed_steps=[], notice=notice, url=RUN_URL
    )

    assert message.splitlines() == [
        "**[OK] 共通統合（cloud-daily-batch）**",
        "対象日: 2026-08-26",
        "Executor: Local",
        "",
        "- モデル生成: 3686社 / 18431件 / 候補35社",
        "",
        f"Run: <{RUN_URL}>",
    ]


def test_render_message_names_the_failed_step_separately() -> None:
    message = render_message(
        outcome="failed", asof="", failed_steps=["hydrate"], notice={}, url=RUN_URL
    )

    assert message.splitlines() == [
        "**[FAILED] 共通統合（cloud-daily-batch）**",
        "対象日: 未確定",
        "Executor: Local",
        "",
        "- 要確認工程: hydrate",
        "",
        f"Run: <{RUN_URL}>",
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
        outcome="ok", asof="2026-09-26", failed_steps=[], notice={}, url=RUN_URL
    )
    assert message.splitlines()[2] == f"Executor: {expected}"
    assert message.count("Executor:") == 1


def test_render_message_does_not_invent_generation_or_delta_without_a_notice() -> None:
    message = render_message(
        outcome="failed", asof="", failed_steps=["pre-batch"], notice={}, url=RUN_URL
    )
    assert "候補入り" not in message
    assert "候補退出" not in message
    assert "モデル生成" not in message


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


def test_main_delivers_the_ok_message_with_generation_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    notice = _notice(tmp_path / "notice.json")
    argv = ["--batch-exit-code", "0"]
    for step in discord.STEP_ORDER:
        argv += [f"--{step}-outcome", "success"]

    exit_code, transport = _run_main(tmp_path, monkeypatch, *argv, notice=notice)

    assert exit_code == 0
    message = _sent(transport)
    assert message.startswith("**[OK] 共通統合（cloud-daily-batch）**")
    assert "対象日: 2026-08-26" in message
    assert "- 工程結果: Lake公開 成功 / Store反映 成功" in message
    assert "- モデル生成: 3686社 / 18431件 / 候補35社" in message
    assert message.endswith(f"Run: <{RUN_URL}>")
    # The message is also printed so the run log carries it.
    assert message in capsys.readouterr().out


def test_main_names_the_batch_stage_when_the_batch_itself_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notice = _notice(tmp_path / "notice.json", failed_stage="model-integration")

    _exit_code, transport = _run_main(
        tmp_path, monkeypatch, "--batch-exit-code", "1", notice=notice
    )

    assert _sent(transport).startswith("**[FAILED]")
    assert "要確認工程: model-integration" in _sent(transport)


def test_main_names_the_workflow_step_that_failed_before_the_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _exit_code, transport = _run_main(
        tmp_path, monkeypatch, "--setup-outcome", "success", "--sync-outcome", "failure"
    )

    assert _sent(transport).startswith("**[FAILED]")
    assert "対象日: 未確定" in _sent(transport)
    assert "要確認工程: sync" in _sent(transport)


def test_main_blames_pre_batch_when_no_tracked_step_failed_and_the_batch_never_ran(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _exit_code, transport = _run_main(tmp_path, monkeypatch, "--setup-outcome", "success")

    assert "要確認工程: pre-batch" in _sent(transport)


def test_main_reports_cancelled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _exit_code, transport = _run_main(tmp_path, monkeypatch, "--cancelled", "true")

    assert _sent(transport).startswith("**[CANCELLED]")


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


@pytest.mark.parametrize(
    "url",
    ["https://[private-token/api/webhooks/1/a", "https://private-token／host/api/webhooks/1/a"],
)
def test_malformed_url_is_a_sanitized_delivery_failure(url):
    transport = FakeTransport()
    assert deliver(url, "hello", transport=transport) == "webhook URL is malformed"
    assert transport.calls == []


def test_successful_integration_notice_keeps_deferred_input_and_real_source_day():
    message = render_message(
        outcome=discord.OUTCOME_OK,
        asof="2026-10-24",
        failed_steps=[],
        notice={"models_ready": True, "company_count": 2, "model_count": 11, "candidate_count": 0},
        url=RUN_URL,
        integration_report={
            "extractions": {
                "edinet-metrics": {
                    "input_asof": "2026-10-23",
                    "deferred_inputs": ["latest_document_list_delayed"],
                }
            }
        },
    )
    assert "[OK]" in message
    assert "入力延期: edinet-metrics" in message
    assert "保存一覧日=2026-10-23" in message
    assert "モデル生成: 2社 / 11件 / 候補0社" in message


@pytest.mark.parametrize(
    ("view_outcome", "lease_outcome", "label", "failed_steps"),
    [
        ("failure", "success", "DEGRADED", "portfolio-views"),
        ("success", "failure", "FAILED", "lease-release"),
        ("failure", "failure", "FAILED", "portfolio-views / lease-release"),
    ],
)
def test_main_reports_all_post_publication_failures_without_losing_core_success(
    tmp_path, monkeypatch, view_outcome, lease_outcome, label, failed_steps
):
    notice = _notice(tmp_path / "notice.json")
    _, transport = _run_main(
        tmp_path,
        monkeypatch,
        "--batch-exit-code",
        "0",
        "--publish-lake-outcome",
        "success",
        "--upload-stores-outcome",
        "success",
        "--portfolio-views-outcome",
        view_outcome,
        "--lease-release-outcome",
        lease_outcome,
        notice=notice,
    )
    message = _sent(transport)
    assert message.startswith(f"**[{label}]")
    assert f"- 要確認工程: {failed_steps}" in message.splitlines()
    assert "工程結果: Lake公開 成功 / Store反映 成功" in message
    assert f"保有・運用画面更新 {discord.step_label(view_outcome)}" in message
    assert "モデル生成: 3686社 / 18431件 / 候補35社" in message


def test_long_details_keep_a_complete_preview_suppressed_run_link() -> None:
    message = discord.render_run_notice(
        label="[FAILED]", batch="test", asof="2026-10-25", details=["x" * 4000], url=RUN_URL
    )
    assert len(message) <= discord.MESSAGE_MAX_CHARS
    assert "…" in message
    assert message.endswith(f"Run: <{RUN_URL}>")


def test_local_execution_without_run_identity_does_not_invent_a_github_url() -> None:
    assert discord.run_url({}) == ""
    message = discord.render_run_notice(
        label="[OK]", batch="test", asof="2026-10-25", details=[], url=""
    )
    assert "Run: なし" in message
    assert "local/local" not in message


@pytest.mark.parametrize("field", ["company_count", "model_count", "candidate_count"])
@pytest.mark.parametrize("value", [None, True, -1, "35", 35.0, [], {}])
def test_invalid_generation_count_is_unknown_not_zero(field, value):
    notice = {
        "models_ready": True,
        "company_count": 3686,
        "model_count": 18431,
        "candidate_count": 35,
    }
    notice[field] = value
    assert discord.render_generation(notice) == ["モデル生成: 完了 / 件数未確認"]


def test_generation_readiness_does_not_reuse_stale_counts_or_legacy_delta():
    notice = {"models_ready": False, "company_count": 2, "model_count": 11, "candidate_count": 1}
    assert discord.render_generation(notice) == ["モデル統合: 未完了"]
    assert (
        discord.render_generation({"delta_measured": True, "entered": ["old"], "exited": []}) == []
    )
    assert discord.render_generation({"models_ready": "true"}) == []


def test_successful_generation_does_not_hide_a_failed_store_upload():
    notice = {"models_ready": True, "company_count": 2, "model_count": 11, "candidate_count": 0}
    outcomes = {"publish-lake": "success", "upload-stores": "failure"}
    failed = derive_failed_steps(outcomes)
    message = render_message(
        outcome=decide_outcome(batch_exit_code="0", failed_steps=failed, cancelled=False),
        asof="2026-10-25",
        failed_steps=failed,
        notice=notice,
        url=RUN_URL,
        step_outcomes=outcomes,
    )
    assert "[FAILED]" in message
    assert "Store反映 失敗" in message
    assert "モデル生成: 2社 / 11件 / 候補0社" in message


def test_unrecognized_nonzero_batch_exit_is_failure_not_legacy_degraded(tmp_path, monkeypatch):
    notice = _notice(tmp_path / "notice.json", models_ready=False, failed_stage="model-integration")
    _, transport = _run_main(tmp_path, monkeypatch, "--batch-exit-code", "3", notice=notice)
    assert "[FAILED]" in _sent(transport)
    assert "モデル統合: 未完了" in _sent(transport)


@pytest.mark.parametrize("exit_code", ["", "1", "2", "3"])
def test_failed_or_unstarted_batch_does_not_replay_a_successful_notice(
    tmp_path, monkeypatch, exit_code
):
    notice = _notice(tmp_path / "stale.json")
    _, transport = _run_main(tmp_path, monkeypatch, "--batch-exit-code", exit_code, notice=notice)
    message = _sent(transport)
    assert "18431" not in message
    assert "モデル統合: 未完了" in message


def test_cancelled_batch_does_not_claim_success_from_stale_notice(tmp_path, monkeypatch):
    notice = _notice(tmp_path / "stale.json")
    _, transport = _run_main(
        tmp_path, monkeypatch, "--batch-exit-code", "0", "--cancelled", "true", notice=notice
    )
    assert "モデル生成" not in _sent(transport)
    assert "モデル統合" not in _sent(transport)


def test_failed_lake_stage_suppresses_stale_success_count():
    message = render_message(
        outcome="failed",
        asof="2026-10-25",
        failed_steps=["publish-lake"],
        url=RUN_URL,
        notice={"models_ready": True, "company_count": 2, "model_count": 11, "candidate_count": 0},
        step_outcomes={"publish-lake": "failure"},
    )
    assert "モデル統合: 未完了" in message
    assert "11件" not in message


def test_deferred_input_text_is_single_line_and_keeps_the_run_link():
    message = render_message(
        outcome="ok",
        asof="2026-10-25",
        failed_steps=[],
        notice={},
        url=RUN_URL,
        integration_report={
            "extractions": {
                "edinet\n{extra}": {
                    "deferred_inputs": ["x" * 4000],
                    "input_asof": "2026-10-23\n{date}",
                }
            }
        },
    )
    assert len(message) <= discord.MESSAGE_MAX_CHARS
    assert message.count("入力延期:") == 1
    assert "edinet extra" in message
    assert "2026-10-23 date" in message
    assert message.endswith(f"Run: <{RUN_URL}>")
