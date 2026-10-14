from datetime import date

import pytest

from baibai_engine.screening.earnings_lag import (
    build_earnings_lag,
    interpreted_next_earnings_status,
)


@pytest.mark.parametrize(
    ("scheduled", "disclosed", "expected_status", "expected_stale"),
    [
        (date(2026, 10, 14), None, "scheduled", None),
        (date(2026, 10, 13), None, "scheduled_date_reached", None),
        (date(2026, 10, 10), None, "scheduled_date_reached", None),
        (date(2026, 10, 10), date(2026, 10, 10), "scheduled_date_reached", False),
        (date(2026, 10, 10), date(2026, 9, 1), "scheduled_date_reached", True),
        (None, None, "unknown", None),
    ],
)
def test_calendar_date_does_not_claim_a_confirmed_disclosure(
    scheduled: date | None,
    disclosed: date | None,
    expected_status: str,
    expected_stale: bool | None,
) -> None:
    lag = build_earnings_lag(
        asof=date(2026, 10, 13),
        latest_financial_disclosure_date=disclosed,
        announcement_date=scheduled,
        summaries=(),
    )
    assert lag.next_earnings_status == expected_status
    assert lag.stale_fin_flag is expected_stale
    assert lag.latest_financial_disclosure_date == disclosed


def test_legacy_announced_is_only_a_date_claim_on_current_reads() -> None:
    assert interpreted_next_earnings_status("announced") == "scheduled_date_reached"
    assert interpreted_next_earnings_status("scheduled") == "scheduled"
    assert interpreted_next_earnings_status(None) is None
