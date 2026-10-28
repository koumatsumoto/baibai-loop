"""Compose published source balances into Screening's current and prior inputs."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from baibai_engine.machine_analysis.models.market_context.margin import MarginBalance
from baibai_engine.machine_analysis.observations.trading_activity.models import (
    JQuantsAllIssuesDailyMargin,
    JQuantsWeeklyMargin,
)
from baibai_engine.machine_analysis.observations.trading_activity.margin_reader import (
    MarginCadence,
    published_margin_balance_dates,
    read_all_issues_daily_margin,
    read_weekly_margin,
)

# The delta axis reaches back 26 observed ISO weeks. Daily observations are sampled
# to one balance date per week; missing weeks can make the calendar span longer.
MARGIN_DELTA_WEEKS = 26

# Maximum age of the newest published balance date accepted by these axes, across
# both the historical weekly and current daily series. This coarse upper bound is
# not a guarantee that the latest scheduled publication has been acquired.
MARGIN_MAX_STALE_DAYS = 35


def _last_balance_date_of_each_week(
    published: list[tuple[date, MarginCadence]],
) -> list[tuple[date, MarginCadence]]:
    """One entry per ISO week, the week's last published balance date.

    `MARGIN_DELTA_WEEKS` counts weeks, and it has to keep counting weeks after the
    cadence changes: reaching 26 entries back through a daily column would compare
    balances five weeks apart and call the result a half-year change. Sampling the
    week's last balance date leaves the weekly era untouched — the exchange publishes
    one balance date per week, so each week already contributes exactly one entry.
    """
    last_of_week: dict[tuple[int, int], tuple[date, MarginCadence]] = {}
    for entry in published:
        last_of_week[entry[0].isocalendar()[:2]] = entry
    return [last_of_week[key] for key in sorted(last_of_week)]


def _read_margin_balances(
    sqlite_path: Path, balance_date: date, cadence: MarginCadence, *, asof: date
) -> dict[str, MarginBalance]:
    """One balance date's rows as the axis sees them, from whichever series holds it."""
    rows: list[JQuantsWeeklyMargin] | list[JQuantsAllIssuesDailyMargin] | None = (
        read_weekly_margin(sqlite_path, balance_date)
        if cadence == "weekly"
        else read_all_issues_daily_margin(sqlite_path, balance_date, publication_asof=asof)
    )
    return {
        row.ticker: MarginBalance(
            balance_date=balance_date,
            issue_type=row.issue_type,
            long_vol=row.long_vol,
            short_vol=row.short_vol,
            long_std_vol=row.long_std_vol,
        )
        for row in rows or ()
    }


def read_margin_supply_demand_inputs(
    sqlite_path: Path,
    asof: date,
) -> tuple[dict[str, MarginBalance], dict[str, MarginBalance]]:
    """The published balance dates a cohort at `asof` may use: latest, and 26 weeks back.

    Both are keyed by ticker. Empty mappings mean the store holds no published
    balance date for this as-of, which is what a store without a margin source looks
    like and yields unset axes rather than wrong ones.

    `latest` is the newest published balance date whichever series published it, so
    the level axes keep describing the most recent balance the exchange has stated.
    `prior` is 26 observed ISO weeks back in the sampled column. Missing weeks
    can make the calendar interval longer; this is not a fixed half-year duration.
    """
    published = published_margin_balance_dates(sqlite_path, asof)
    if not published or (asof - published[-1][0]).days > MARGIN_MAX_STALE_DAYS:
        return {}, {}
    latest = _read_margin_balances(sqlite_path, *published[-1], asof=asof)
    prior: dict[str, MarginBalance] = {}
    weekly_steps = _last_balance_date_of_each_week(published)
    if len(weekly_steps) > MARGIN_DELTA_WEEKS:
        prior = _read_margin_balances(
            sqlite_path, *weekly_steps[-1 - MARGIN_DELTA_WEEKS], asof=asof
        )
    return latest, prior
