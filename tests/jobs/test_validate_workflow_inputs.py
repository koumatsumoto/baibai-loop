from __future__ import annotations

import pytest

from baibai_jobs.validation.workflow_inputs import (
    WorkflowInputError,
    main,
    validate_daily_input,
)


@pytest.mark.parametrize("asof", ["", "2026-08-01"])
def test_daily_accepts_schedule_and_exact_manual_date(asof: str) -> None:
    validate_daily_input(asof=asof)


@pytest.mark.parametrize(
    "asof",
    [
        "20260801",
        "2026-8-1",
        "2026-02-30",
        "$(printf injected)",
        "`printf injected`",
        "2026-08-01\nprintf injected",
    ],
)
def test_daily_rejects_non_exact_or_shell_shaped_input(asof: str) -> None:
    with pytest.raises(WorkflowInputError):
        validate_daily_input(asof=asof)


def test_cli_reports_contract_without_echoing_malicious_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    malicious = "2026-08-01\nprintf injected"

    code = main(["daily", "--asof", malicious])

    captured = capsys.readouterr()
    assert code == 2
    assert "asof must use YYYY-MM-DD" in captured.err
    assert malicious not in captured.err
