from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from baibai_batch.jobs import external_assessment_daily as daily
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
def test_prior_publication_is_skipped_and_retains_complete_or_partial_identity(monkeypatch, status):
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
        else "以前の完全公開確認済み"
    ) in message
    assert "今回の再取得/再公開なし" in message
    assert "固定 pair=4; typed pair=2" in message
    assert "既知失敗 pair=不明" in message
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


def test_publication_outputs_allow_only_measured_nonnegative_aggregate_counters(
    tmp_path, monkeypatch
):
    path = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(path))
    daily.write_publication_outputs(
        {
            "expected_pairs": 8,
            "observations": 6,
            "rows_added": 6,
            "state_counts": {
                "success": 4,
                "normal_null": 2,
                "failure": 2,
                "unattempted": 0,
            },
            "ticker": "private-ticker",
            "private_payload": "private-body",
            "run_id": "private-batch",
        }
    )
    assert path.read_text() == (
        "external_assessment_expected_pairs=8\nexternal_assessment_typed_pairs=6\nexternal_assessment_known_failure_pairs=2\n"
        "external_assessment_normal_null_pairs=2\nexternal_assessment_unattempted_pairs=0\nexternal_assessment_rows_added=6\n"
    )


def test_legacy_terminal_missing_counters_remain_unknown_without_reconstruction(
    tmp_path, monkeypatch
):
    path = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(path))
    daily.write_publication_outputs(
        {"expected_pairs": 8, "observations": 6, "readback": "verified"}
    )
    assert path.read_text() == (
        "external_assessment_expected_pairs=8\nexternal_assessment_typed_pairs=6\nexternal_assessment_known_failure_pairs=unknown\n"
        "external_assessment_normal_null_pairs=unknown\nexternal_assessment_unattempted_pairs=unknown\nexternal_assessment_rows_added=unknown\n"
    )


def test_invalid_report_count_shape_cannot_leak_into_outputs(tmp_path, monkeypatch):
    path = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(path))
    daily.write_publication_outputs(
        {
            "expected_pairs": True,
            "observations": "private-secret",
            "rows_added": -1,
            "state_counts": {"failure": "private-body", "normal_null": False},
        }
    )
    assert path.read_text().count("=unknown\n") == 6


@pytest.mark.parametrize("has_states", [True, False])
def test_publish_main_emits_actual_report_only_after_existing_publisher_returns(
    tmp_path, monkeypatch, has_states
):
    from baibai_batch.jobs import external_assessment_publication as external_assessment_publication
    from baibai_batch.storage import publication_lease

    p = daily.make_plan(
        run_id="synthetic-publish",
        tickers=("7203",),
        universe_binding="synthetic",
        collector_binding=daily.external_assessment_collector_binding(),
    )
    report = {"expected_pairs": 2, "observations": 1, "readback": "verified"}
    if has_states:
        report.update(rows_added=1, state_counts={"success": 0, "normal_null": 1, "failure": 1})
    called = []

    def publish(*args, **kwargs):
        called.append(True)
        return report

    monkeypatch.setattr(daily, "Boto3R2Store", lambda **kwargs: object())
    monkeypatch.setattr(daily, "read_plan", lambda *args: p)
    monkeypatch.setattr(daily, "read_terminal", lambda *args: None)
    monkeypatch.setattr(external_assessment_publication, "publish_and_verify", publish)
    monkeypatch.setattr(publication_lease, "_load_handle", lambda *args: object())
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert (
        daily.main(
            [
                "publish",
                "--run-id",
                p.run_id,
                "--directory",
                str(tmp_path / "publish"),
                "--lease-handle",
                str(tmp_path / "lease"),
            ]
        )
        == 0
    )
    assert called == [True]
    written = output.read_text()
    assert "external_assessment_expected_pairs=2\nexternal_assessment_typed_pairs=1\n" in written
    assert (
        f"external_assessment_known_failure_pairs={'1' if has_states else 'unknown'}\n" in written
    )
    assert "synthetic-publish" not in written


def test_acquire_main_does_not_write_publication_outputs(tmp_path, monkeypatch):
    p = daily.make_plan(
        run_id="synthetic-acquire",
        tickers=("7203",),
        universe_binding="synthetic",
        collector_binding=daily.external_assessment_collector_binding(),
    )
    monkeypatch.setattr(daily, "Boto3R2Store", lambda **kwargs: object())
    monkeypatch.setattr(daily, "read_plan", lambda *args: p)
    monkeypatch.setattr(daily, "acquire_cohort", lambda *args: {"status": "already_published"})
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert daily.main(["acquire", "--run-id", p.run_id, "--cohort", "0"]) == 0
    assert not output.exists()


def test_workflow_has_one_terminal_notifier_outside_matrix_with_step_only_existing_secret():
    repository = Path(__file__).resolve().parents[2]
    workflow = yaml.load(
        (repository / ".github/workflows/cloud-external-assessment.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert set(workflow["on"]) == {"workflow_dispatch", "schedule"}
    assert workflow["on"]["workflow_dispatch"]["inputs"]["operation"]["options"] == [
        "collect_and_publish",
        "publish_confirmed_partial",
    ]
    jobs = workflow["jobs"]
    notifiers = {
        name: job
        for name, job in jobs.items()
        if any(
            "baibai_batch.observability.external_assessment_discord" in step.get("run", "")
            for step in job["steps"]
        )
    }
    assert len(notifiers) == 1
    notify_job = next(iter(notifiers.values()))
    assert notify_job["needs"] == ["plan", "acquire", "publish"]
    assert "strategy" not in notify_job
    assert "always()" in notify_job["if"]
    assert "workflow_dispatch" in notify_job["if"]
    assert "refs/heads/main" in notify_job["if"]
    assert "DISCORD_WEBHOOK_URL" not in workflow.get("env", {})
    assert "DISCORD_WEBHOOK_URL" not in notify_job.get("env", {})
    notify_steps = [
        step
        for step in notify_job["steps"]
        if "baibai_batch.observability.external_assessment_discord" in step.get("run", "")
    ]
    assert len(notify_steps) == 1
    step = notify_steps[0]
    assert step["env"]["DISCORD_WEBHOOK_URL"] == "${{ secrets.DISCORD_WEBHOOK_URL }}"
    assert step["env"]["PYTHONPATH"] == "batch/src"
    serialized = json.dumps(notify_job)
    for private in (
        "R2_ACCOUNT_ID",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "batch_id",
        "master_asof",
    ):
        assert private not in serialized
    assert "python3 -m baibai_batch.observability.external_assessment_discord" in step["run"]
    assert "--terminal-status" in step["run"]
    assert "--publication-mode" in step["run"]
    assert "needs.plan.outputs.terminal_status" in step["run"]
    assert "needs.plan.outputs.terminal_status == 'pending'" in jobs["publish"]["if"]
    assert "!cancelled()" in jobs["publish"]["if"]
    assert "needs.plan.outputs.terminal_status == 'pending'" in jobs["acquire"]["if"]
    assert "needs.plan.outputs.operation == 'collect_and_publish'" in jobs["acquire"]["if"]
    for counter in notify.COUNTERS:
        assert f"--{counter.replace('_', '-')}" in step["run"]
        assert (
            f"steps.publish.outputs.external_assessment_{counter}"
            in jobs["publish"]["outputs"][counter]
        )
    cancellation = next(step for step in notify_job["steps"] if step.get("id") == "cancellation")
    assert cancellation["if"] == "${{ cancelled() }}"
    assert "steps.cancellation.outputs.cancelled" in step["run"]
    for job in jobs.values():
        for job_step in job["steps"]:
            assert not re.search(
                r"\$\{\{[^}]*\b(?:always|success|failure|cancelled)\s*\(",
                job_step.get("run", ""),
            )
