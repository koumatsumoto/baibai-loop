import pytest

from baibai_batch.observability import source_discord


@pytest.mark.parametrize(
    ("acquired", "staged", "expected_calls"),
    [
        ("success", "success", 0),
        ("skipped", "success", 0),
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
