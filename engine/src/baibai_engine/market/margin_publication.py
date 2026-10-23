"""Fixed publication boundaries for JPX margin-balance datasets."""

from __future__ import annotations

from bisect import bisect_right
from datetime import date

# The weekly all-issues publication ends with the 2026-09-18 balance, published
# on 2026-09-25. The replacement dataset starts with the 2026-09-25 balance and
# is first published on 2026-09-28. Keeping the two balance-date domains disjoint
# prevents a daily row from silently changing the meaning of the historical
# weekly series.
LEGACY_WEEKLY_LAST_BALANCE_DATE = date(2026, 9, 18)
LEGACY_WEEKLY_LAST_PUBLICATION_DATE = date(2026, 9, 25)
ALL_ISSUES_DAILY_FIRST_BALANCE_DATE = date(2026, 9, 25)


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
    "LEGACY_WEEKLY_LAST_BALANCE_DATE",
    "LEGACY_WEEKLY_LAST_PUBLICATION_DATE",
    "require_all_issues_daily_balance_date",
    "require_legacy_weekly_balance_date",
]


# The legacy weekly series is published on the second trading day after its
# balance date; the replacement daily series is published on the next trading day.
MARGIN_PUBLICATION_TRADING_DAYS = 2
ALL_ISSUES_DAILY_PUBLICATION_TRADING_DAYS = 1


def _publication_trading_day(
    trading_days: list[date], balance_date: date, *, trading_day_lag: int
) -> date | None:
    publication = bisect_right(trading_days, balance_date) + trading_day_lag - 1
    return trading_days[publication] if publication < len(trading_days) else None


def _publication_has_happened(
    trading_days: list[date], balance_date: date, *, asof: date, trading_day_lag: int
) -> bool:
    """Whether the balance date's publication day is a trading day strictly before `asof`.

    Same comparison for both cadences: the publication lands in the late afternoon
    while a decision prices at the close, so a balance published on `asof` is not
    yet usable at `asof`'s price.
    """
    publication_day = _publication_trading_day(
        trading_days, balance_date, trading_day_lag=trading_day_lag
    )
    return publication_day is not None and publication_day < asof
