"""Fixed publication boundaries for JPX margin-balance datasets."""

from __future__ import annotations

from datetime import date

# The weekly all-issues publication ends with the 2026-09-18 balance, published
# on 2026-09-24. The replacement dataset starts with the 2026-09-25 balance and
# is first published on 2026-09-28. Keeping the two balance-date domains disjoint
# prevents a daily row from silently changing the meaning of the historical
# weekly series.
LEGACY_WEEKLY_LAST_BALANCE_DATE = date(2026, 9, 18)
LEGACY_WEEKLY_LAST_PUBLICATION_DATE = date(2026, 9, 24)
ALL_ISSUES_DAILY_FIRST_BALANCE_DATE = date(2026, 9, 25)
ALL_ISSUES_DAILY_FIRST_PUBLICATION_DATE = date(2026, 9, 28)

# The calendar is not authority for whether the migration actually happened.
# This remains false until the official 2026-09-27 go/no-go announcement and the
# executable ClientV2 payload contract are both verified. The U4 activation
# change records that evidence together with the first continuity report.
ALL_ISSUES_DAILY_PUBLICATION_CONFIRMED = False


def require_legacy_weekly_balance_date(balance_date: date) -> None:
    if balance_date > LEGACY_WEEKLY_LAST_BALANCE_DATE:
        raise ValueError(
            "jquants_weekly_margin accepts only the legacy weekly series through "
            f"{LEGACY_WEEKLY_LAST_BALANCE_DATE.isoformat()}; got {balance_date.isoformat()}"
        )


def require_all_issues_daily_balance_date(balance_date: date) -> None:
    if balance_date < ALL_ISSUES_DAILY_FIRST_BALANCE_DATE:
        raise ValueError(
            "jquants_all_issues_daily_margin accepts only the daily all-issues series from "
            f"{ALL_ISSUES_DAILY_FIRST_BALANCE_DATE.isoformat()}; got {balance_date.isoformat()}"
        )


__all__ = [
    "ALL_ISSUES_DAILY_FIRST_BALANCE_DATE",
    "ALL_ISSUES_DAILY_FIRST_PUBLICATION_DATE",
    "ALL_ISSUES_DAILY_PUBLICATION_CONFIRMED",
    "LEGACY_WEEKLY_LAST_BALANCE_DATE",
    "LEGACY_WEEKLY_LAST_PUBLICATION_DATE",
    "require_all_issues_daily_balance_date",
    "require_legacy_weekly_balance_date",
]
