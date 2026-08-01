from __future__ import annotations

import pytest
from tools.cloud.validate_workflow_inputs import (
    WorkflowInputError,
    main,
    validate_backfill_inputs,
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


def test_backfill_accepts_ordered_window_and_optional_master_start() -> None:
    validate_backfill_inputs(
        start="2020-01-01",
        end="2026-08-01",
        master_month_end_from="2019-01-01",
    )
    validate_backfill_inputs(
        start="2020-01-01",
        end="2026-08-01",
        master_month_end_from="",
    )


@pytest.mark.parametrize(
    ("start", "end", "master"),
    [
        ("", "2026-08-01", ""),
        ("2026-01-01", "", ""),
        ("2026-08-02", "2026-08-01", ""),
        ("2026-01-01", "2026-08-01", "2026-08-02"),
        ("$(printf injected)", "2026-08-01", ""),
        ("2026-01-01", "`printf injected`", ""),
        ("2026-01-01", "2026-08-01", "2026-08-01\nprintf injected"),
    ],
)
def test_backfill_rejects_invalid_dates_and_order(
    start: str,
    end: str,
    master: str,
) -> None:
    with pytest.raises(WorkflowInputError):
        validate_backfill_inputs(
            start=start,
            end=end,
            master_month_end_from=master,
        )


def test_cli_reports_contract_without_echoing_malicious_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    malicious = "2026-08-01\nprintf injected"

    code = main(["daily", "--asof", malicious])

    captured = capsys.readouterr()
    assert code == 2
    assert "asof must use YYYY-MM-DD" in captured.err
    assert malicious not in captured.err
