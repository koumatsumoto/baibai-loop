from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from baibai_batch.observability import external_assessment_discord as notify

SUCCESS = (
    "--plan-result",
    "success",
    "--acquire-result",
    "success",
    "--publish-result",
    "success",
    "--expected-pairs",
    "8",
    "--typed-pairs",
    "8",
    "--known-failure-pairs",
    "0",
    "--normal-null-pairs",
    "2",
    "--unattempted-pairs",
    "0",
    "--rows-added",
    "8",
)


def invoke(monkeypatch, *args):
    calls = []
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/id/private-token")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("EXTERNAL_ASSESSMENT_BATCH_ID", "private-batch")
    monkeypatch.setenv("BROKER_URL", "https://private.example/body-secret")

    def transport(url, body, timeout):
        calls.append((url, json.loads(body), timeout))
        return 200

    assert notify.main(list(args), transport=transport) == 0
    assert len(calls) == 1
    url, payload, timeout = calls[0]
    assert url.endswith("?wait=true")
    assert timeout == notify.DEFAULT_TIMEOUT_SECONDS
    assert payload["allowed_mentions"] == {"parse": []}
    assert "private-token" not in payload["content"]
    assert "private-batch" not in payload["content"]
    assert "private.example" not in payload["content"]
    assert len(payload["content"]) < 2000
    return payload["content"]


def test_terminal_success_notifies_once_with_normal_null_and_run_context(monkeypatch):
    message = invoke(monkeypatch, *SUCCESS)
    assert message.startswith("[OK]")
    assert "固定 pair=8; typed pair=8; 既知失敗 pair=0; 全評価値 null pair=2; 追加 row=8" in message
    assert "executor: GitHub Actions" in message
    assert "run: https://github.com/example/repo/actions/runs/123/attempts/2" in message


def test_publication_with_known_failures_is_degraded(monkeypatch):
    message = invoke(
        monkeypatch,
        *SUCCESS,
        "--typed-pairs",
        "6",
        "--known-failure-pairs",
        "2",
        "--rows-added",
        "6",
    )
    assert message.startswith("[DEGRADED]")
    assert "公開確認済み・既知の取得/parse 失敗あり" in message
    assert "既知失敗 pair=2" in message


def test_success_with_missing_aggregate_is_degraded_and_unknown_not_zero(monkeypatch):
    message = invoke(monkeypatch, "--publish-result", "success")
    assert message.startswith("[DEGRADED]")
    assert message.count("不明") == 7
    assert "pair=0" not in message


@pytest.mark.parametrize(
    "counts",
    [
        ("--normal-null-pairs", "unknown"),
        ("--expected-pairs", "7"),
        ("--normal-null-pairs", "9"),
        ("--rows-added", "9"),
        (
            "--expected-pairs",
            "0",
            "--typed-pairs",
            "0",
            "--normal-null-pairs",
            "0",
            "--rows-added",
            "0",
        ),
    ],
)
def test_missing_or_inconsistent_publication_counts_cannot_report_ok(monkeypatch, counts):
    message = invoke(monkeypatch, *SUCCESS, *counts)
    assert message.startswith("[DEGRADED]")
    assert "品質件数不明" in message


def test_explicit_partial_publication_reports_uncertain_and_unattempted_as_degraded(monkeypatch):
    message = invoke(
        monkeypatch,
        *SUCCESS,
        "--publication-mode",
        "confirmed_partial",
        "--expected-pairs",
        "4",
        "--typed-pairs",
        "2",
        "--normal-null-pairs",
        "1",
        "--rows-added",
        "2",
        "--unattempted-pairs",
        "2",
    )
    assert message.startswith("[DEGRADED]")
    assert "確定観測のみ部分公開確認済み" in message
    assert "未記録 pair=2" in message


@pytest.mark.parametrize("status", ["already_published", "already_partially_published"])
def test_prior_publication_is_skipped_without_claiming_complete_acquisition(monkeypatch, status):
    message = invoke(
        monkeypatch,
        "--plan-result",
        "success",
        "--terminal-status",
        status,
        "--expected-pairs",
        "4",
        "--typed-pairs",
        "2",
        "--rows-added",
        "0",
    )
    assert message.startswith("[SKIPPED]")
    assert (
        "以前の部分公開確認済み"
        if status == "already_partially_published"
        else "以前の公開確認済み"
    ) in message
    assert "今回の再取得/再公開なし" in message
    assert "固定 pair=4; typed pair=2" in message
    assert "既知失敗 pair=不明" in message
    assert "完全" not in message
    assert "追加 row=0" in message


@pytest.mark.parametrize("reason", ["access_denied", "rate_limited", "wall_clock_limit"])
def test_aggregate_acquisition_failure_does_not_guess_a_blocked_reason_or_counts(
    monkeypatch, reason
):
    monkeypatch.setenv("EXTERNAL_ASSESSMENT_STOP_REASON", reason)
    message = invoke(
        monkeypatch,
        "--plan-result",
        "success",
        "--acquire-result",
        "failure",
        "--typed-pairs",
        "999",
        "--known-failure-pairs",
        "999",
    )
    assert message.startswith("[FAILED]")
    assert "取得停止/未完了・この run の公開なし・停止原因未確認" in message
    assert reason not in message
    assert "999" not in message
    assert "固定 pair=不明" in message


@pytest.mark.parametrize(
    ("args", "label", "detail"),
    [
        (("--plan-result", "failure"), "[FAILED]", "plan 失敗・この run の公開なし"),
        (("--publish-result", "failure"), "[FAILED]", "publish 失敗・公開確認未完了"),
        ((), "[SKIPPED]", "この run の公開なし"),
        (("--acquire-result", "cancelled"), "[CANCELLED]", "取消・この run の公開確認未完了"),
        (("--cancelled", "true"), "[CANCELLED]", "取消・この run の公開確認未完了"),
        ((*SUCCESS, "--cancelled", "true"), "[CANCELLED]", "取消・公開確認済み"),
    ],
)
def test_fixed_terminal_states_are_distinct(monkeypatch, args, label, detail):
    message = invoke(monkeypatch, *args)
    assert message.startswith(label)
    assert detail in message


@pytest.mark.parametrize("status", [403, 429, 500])
def test_delivery_failure_is_sanitized_and_nonfatal(monkeypatch, capsys, status):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/id/private-token")
    assert notify.main(list(SUCCESS), transport=lambda *_: status) == 0
    captured = capsys.readouterr()
    assert f"delivery failed: http {status}" in captured.err
    assert "private-token" not in captured.out + captured.err


def test_delivery_exception_does_not_expose_exception_body_or_token(monkeypatch, capsys):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/id/private-token")

    def transport(*args):
        raise RuntimeError("private-token private-body https://private.example")

    assert notify.main(list(SUCCESS), transport=transport) == 0
    captured = capsys.readouterr()
    assert "delivery failed: RuntimeError" in captured.err
    for private in ("private-token", "private-body", "private.example"):
        assert private not in captured.out + captured.err


def test_unconfigured_webhook_makes_zero_transport_calls_and_keeps_exit_zero(monkeypatch, capsys):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    calls = []
    assert notify.main(list(SUCCESS), transport=lambda *args: calls.append(args)) == 0
    assert calls == []
    assert "webhook URL is not configured" in capsys.readouterr().err


@pytest.mark.parametrize("counter", ["", "unknown"])
def test_missing_counter_inputs_remain_unknown(monkeypatch, counter):
    message = invoke(monkeypatch, "--publish-result", "success", "--rows-added", counter)
    assert "追加 row=不明" in message


@pytest.mark.parametrize("counter", ["-1", "private-body", "１２", "1\nsecret"])
def test_counter_parser_rejects_invalid_values_without_echoing_them(capsys, counter):
    with pytest.raises(SystemExit) as error:
        notify.build_parser().parse_args(["--rows-added", counter])
    assert error.value.code == 2
    assert counter not in capsys.readouterr().err


def test_notification_imports_with_system_python_without_site_packages():
    repository = Path(__file__).resolve().parents[2]
    environment = {**os.environ, "PYTHONPATH": str(repository / "batch/src")}
    result = subprocess.run(
        ["python3", "-S", "-c", "import baibai_batch.observability.external_assessment_discord"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_source_notifier_runs_after_lease_release_and_uses_only_step_secret():
    repository = Path(__file__).resolve().parents[2]
    workflow = yaml.load(
        (repository / ".github/workflows/cloud-external-assessment.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    steps = workflow["jobs"]["publish"]["steps"]
    release = next(i for i, step in enumerate(steps) if step.get("id") == "lease-release")
    notice = next(i for i, step in enumerate(steps) if "--sources-report" in step.get("run", ""))
    assert release < notice
    assert steps[notice]["if"] == "${{ always() }}"
    assert steps[notice]["env"]["DISCORD_WEBHOOK_URL"] == "${{ secrets.DISCORD_WEBHOOK_URL }}"


def test_status_function_is_only_evaluated_in_step_condition_for_source_notice():
    repository = Path(__file__).resolve().parents[2]
    workflow = yaml.load(
        (repository / ".github/workflows/cloud-external-assessment.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    for job in ("publish", "notify"):
        steps = workflow["jobs"][job]["steps"]
        cancellation = next(step for step in steps if step.get("id") == "cancellation")
        assert cancellation["if"] == "${{ cancelled() }}"
        assert "cancelled=true" in cancellation["run"]
        notice = next(step for step in steps if "--cancelled" in step.get("run", ""))
        assert "steps.cancellation.outputs.cancelled || 'false'" in notice["run"]
        assert all("cancelled()" not in step.get("run", "") for step in steps)


def test_source_notice_marks_attempt_before_post_even_when_response_is_lost(tmp_path, monkeypatch):
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv(
        "DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/id/synthetic-secret"
    )
    calls = []

    def transport(*args):
        assert output.read_text() == "attempted=true\n"
        calls.append(True)
        raise TimeoutError("synthetic response lost")

    assert notify.main(["--publish-result", "cancelled"], transport=transport) == 0
    assert calls == [True]
    assert output.read_text() == "attempted=true\n"


def test_source_render_keeps_private_payload_and_invalid_counts_out_of_notice(tmp_path):
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "source": "monex",
                        "status": "published",
                        "expected_pairs": 2,
                        "rows_added": None,
                        "ticker": "private-ticker",
                        "private_payload": "private-body",
                        "state_counts": {
                            "success": True,
                            "normal_null": "private-count",
                            "failure": 1,
                        },
                    }
                ]
            }
        )
    )
    text = notify.render_sources(report, lease_release="success", cancelled="false")
    assert "private" not in text
    assert "typed=不明" in text
    assert "正常null=不明" in text
    assert "追加row=不明" in text


def test_fallback_only_runs_when_internal_notice_has_not_attempted_delivery():
    repository = Path(__file__).resolve().parents[2]
    workflow = yaml.load(
        (repository / ".github/workflows/cloud-external-assessment.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    publisher = workflow["jobs"]["publish"]
    assert publisher["outputs"]["notification_attempted"] == "${{ steps.notice.outputs.attempted }}"
    assert any(step.get("id") == "notice" for step in publisher["steps"])
    fallback = workflow["jobs"]["notify"]["if"]
    assert "always()" in fallback
    assert "needs.publish.outputs.notification_attempted != 'true'" in fallback
    assert "needs.publish.result == 'skipped'" not in fallback
