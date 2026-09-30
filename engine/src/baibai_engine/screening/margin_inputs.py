"""Compose published source balances into Screening's current and prior inputs."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from baibai_engine.market.jquants_models import (
    JQuantsAllIssuesDailyMargin,
    JQuantsWeeklyMargin,
)
from baibai_engine.market.sqlite.reader import (
    MarginCadence,
    published_margin_balance_dates,
    read_all_issues_daily_margin,
    read_weekly_margin,
)
from baibai_engine.screening.margin_metrics import MarginBalance

# Balance dates the delta axis reaches back over. They are weekly, so this is about
# half a year — 26 to 28 calendar weeks in practice, because the exchange skips
# some weeks and this counts stored dates rather than calendar distance.
MARGIN_DELTA_WEEKS = 26

# How stale the newest published balance date may be before the axes decline to
# answer. The cadence is weekly and the publication lag is two trading days, so a
# healthy store sits inside three weeks even across a long closure. Beyond this the
# balance describes a market the as-of no longer resembles, and a silent stale join
# is worse than an unset axis.
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
    sqlite_path: Path, balance_date: date, cadence: MarginCadence
) -> dict[str, MarginBalance]:
    """One balance date's rows as the axis sees them, from whichever series holds it."""
    rows: list[JQuantsWeeklyMargin] | list[JQuantsAllIssuesDailyMargin] | None = (
        read_weekly_margin(sqlite_path, balance_date)
        if cadence == "weekly"
        else read_all_issues_daily_margin(sqlite_path, balance_date)
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
    `prior` is 26 weeks back in the sampled column, so the delta axis keeps comparing
    across half a year rather than across however many rows the cadence produced.
    """
    published = published_margin_balance_dates(sqlite_path, asof)
    if not published or (asof - published[-1][0]).days > MARGIN_MAX_STALE_DAYS:
        return {}, {}
    latest = _read_margin_balances(sqlite_path, *published[-1])
    prior: dict[str, MarginBalance] = {}
    weekly_steps = _last_balance_date_of_each_week(published)
    if len(weekly_steps) > MARGIN_DELTA_WEEKS:
        prior = _read_margin_balances(sqlite_path, *weekly_steps[-1 - MARGIN_DELTA_WEEKS])
    return latest, prior
