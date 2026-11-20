import pytest

from baibai_jobs.observability import source_discord


@pytest.mark.parametrize(
    ("acquired", "staged", "expected_calls"),
    [
        ("success", "success", 0),
        ("skipped", "success", 0),
        ("skipped", "failure", 1),
        ("failure", "failure", 1),
        ("failure", "success", 1),
        ("cancelled", "skipped", 1),
        ("success", "failure", 1),
        ("success", "cancelled", 1),
    ],
)
def test_source_notice_reports_receipts_not_publication(
    monkeypatch, mocker, capsys, acquired, staged, expected_calls
):
    monkeypatch.setenv("SOURCE_GROUP", "economic-boj")
    monkeypatch.setenv("ASOF", "2026-10-24")
    monkeypatch.setenv("ACQUIRE_RESULT", acquired)
    monkeypatch.setenv("STAGE_RESULT", staged)
    deliver = mocker.patch.object(source_discord, "deliver", return_value=None)
    assert source_discord.main() == 0
    assert deliver.call_count == expected_calls
    text = capsys.readouterr().out
    assert "情報取得（economic-boj）" in text
    assert "対象日: 2026-10-24" in text
    assert (
        f"取得ジョブ: {source_discord.step_label(acquired)} / "
        f"private受領: {source_discord.step_label(staged)}"
    ) in text
    assert "正本・表示への反映は別スケジュールの共通統合で実施" in text


@pytest.mark.parametrize(
    ("job", "collected", "ready", "label"),
    [
        ("failure", "skipped", "skipped", "FAILED"),  # setup/resolve failed
        ("failure", "success", "unknown", "FAILED"),  # job ended before reporting ready
        ("cancelled", "unknown", "unknown", "CANCELLED"),  # runner returned no outputs
    ],
)
def test_source_job_failure_notifies_without_inventing_step_results(
    monkeypatch, mocker, job, collected, ready, label
):
    monkeypatch.setenv("SOURCE_GROUP", "external-monex")
    monkeypatch.setenv("SOURCE_JOB_RESULT", job)
    monkeypatch.setenv("ACQUIRE_RESULT", collected)
    monkeypatch.setenv("STAGE_RESULT", ready)
    deliver = mocker.patch.object(source_discord, "deliver", return_value=None)
    assert source_discord.main() == 0
    deliver.assert_called_once()
    notice = deliver.call_args.args[1]
    assert f"[{label}]" in notice
    assert f"実行ジョブ: {source_discord.step_label(job)}" in notice
    assert (
        f"取得ジョブ: {source_discord.step_label(collected)} / private受領: {source_discord.step_label(ready)}"
        in notice
    )


def test_notification_failure_is_visible_but_does_not_replace_acquisition_result(
    monkeypatch, mocker, capsys
):
    monkeypatch.setenv("ACQUIRE_RESULT", "failure")
    monkeypatch.setenv("STAGE_RESULT", "success")
    mocker.patch.object(source_discord, "deliver", return_value="delivery failed: http 500")
    assert source_discord.main() == 0
    assert "source notification failed: delivery failed: http 500" in capsys.readouterr().err


def test_source_failure_has_batch_identity_and_suppresses_run_preview(monkeypatch, mocker):
    monkeypatch.setenv("SOURCE_GROUP", "external-minkabu")
    monkeypatch.setenv("ASOF", "2026-10-25")
    monkeypatch.setenv("ACQUIRE_RESULT", "failure")
    monkeypatch.setenv("STAGE_RESULT", "success")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "12")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    deliver = mocker.patch.object(source_discord, "deliver", return_value=None)
    assert source_discord.main() == 0
    text = deliver.call_args.args[1]
    assert text.startswith("**[FAILED] 情報取得（external-minkabu）**")
    assert "Executor: GitHub Actions" in text
    assert "取得ジョブ: 失敗 / private受領: 成功" in text
    assert text.endswith("Run: <https://github.com/owner/repo/actions/runs/12/attempts/2>")


@pytest.mark.parametrize(
    ("source", "acquired", "staged", "flag", "quiet"),
    [
        ("economic-spglobal_pmi", "failure", "success", "true", True),
        ("economic-tradingeconomics_pmi", "failure", "success", "true", True),
        ("economic-spglobal_pmi", "failure", "failure", "true", False),
        ("economic-spglobal_pmi", "cancelled", "success", "true", False),
        ("economic-spglobal_pmi", "failure", "success", "false", False),
        ("economic-fred_csv", "failure", "success", "true", False),
    ],
)
def test_only_confirmed_pmi_refusal_with_saved_receipts_is_quiet(
    monkeypatch, mocker, capsys, source, acquired, staged, flag, quiet
):
    monkeypatch.setenv("SOURCE_GROUP", source)
    monkeypatch.setenv("ACQUIRE_RESULT", acquired)
    monkeypatch.setenv("STAGE_RESULT", staged)
    monkeypatch.setenv("EXPECTED_UNAVAILABLE", flag)
    deliver = mocker.patch.object(source_discord, "deliver", return_value=None)
    assert source_discord.main() == 0
    assert deliver.call_count == (0 if quiet else 1)
    assert ("[UNAVAILABLE]" in capsys.readouterr().out) is quiet
