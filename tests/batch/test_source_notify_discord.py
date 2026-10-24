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
    assert f"acquisition={acquired}; private_receipts={staged}" in text
    assert "canonical/serving integration runs on its separate schedule" in text


def test_notification_failure_is_visible_but_does_not_replace_acquisition_result(
    monkeypatch, mocker, capsys
):
    monkeypatch.setenv("ACQUIRE_RESULT", "failure")
    monkeypatch.setenv("STAGE_RESULT", "success")
    mocker.patch.object(source_discord, "deliver", return_value="delivery failed: http 500")
    assert source_discord.main() == 0
    assert "source notification failed: delivery failed: http 500" in capsys.readouterr().err
