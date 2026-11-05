"""Fixed publication boundaries for JPX margin-balance datasets."""

from __future__ import annotations

from bisect import bisect_right
from datetime import date

# 全銘柄の週次と日次は残高日で分離し、後発の日次で旧週次の意味を変えない。
# 日々公表subsetのmargin-alertは、この全銘柄残高の公表契約に含めない。
from baibai_engine.data_collection.providers.jquants.market_rules import (
    ALL_ISSUES_DAILY_FIRST_BALANCE_DATE,
    LEGACY_WEEKLY_LAST_BALANCE_DATE,
    LEGACY_WEEKLY_LAST_PUBLICATION_DATE,
)


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
    trading_days: list[date],
    balance_date: date,
    *,
    asof: date,
    trading_day_lag: int,
    publication_date: date | None = None,
) -> bool:
    """引け後公表の全銘柄残高を、同日終値時点の既知値へ遡及させない。

    明示PubDateを優先し、欠ける場合だけ営業日lagで近似する。
    """
    if publication_date is not None:
        return publication_date < asof
    publication_day = _publication_trading_day(
        trading_days, balance_date, trading_day_lag=trading_day_lag
    )
    return publication_day is not None and publication_day < asof
