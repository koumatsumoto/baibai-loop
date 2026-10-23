"""Stored margin and short-sale observations, selected by publication time."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

from baibai_engine.market.jquants_contract import (
    ALL_ISSUES_DAILY_MARGIN_SOURCE,
    MARGIN_ALERT_SOURCE,
    SHORT_SALE_REPORT_SOURCE,
    WEEKLY_MARGIN_SOURCE,
    all_issues_daily_margin_coverage_key,
    weekly_margin_coverage_key,
)
from baibai_engine.market.jquants_models import (
    JQuantsAllIssuesDailyMargin,
    JQuantsMarginAlert,
    JQuantsShortSaleReport,
    JQuantsWeeklyMargin,
)
from baibai_engine.market.margin_publication import (
    ALL_ISSUES_DAILY_PUBLICATION_TRADING_DAYS,
    MARGIN_PUBLICATION_TRADING_DAYS,
    _publication_has_happened,
)
from baibai_engine.storage.observations.convert import optional_date, optional_float
from baibai_engine.storage.observations.coverage import range_covered
from baibai_engine.storage.observations.schema import connect_current

MarginCadence = Literal["weekly", "daily"]


def read_short_sale_reports(
    sqlite_path: Path, start: date, end: date
) -> list[JQuantsShortSaleReport] | None:
    """Read a disclosure-date range only when canonical coverage spans it."""
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        if not range_covered(conn, SHORT_SALE_REPORT_SOURCE, start, end):
            return None
        rows = conn.execute(
            "SELECT disclosed_at, source_ordinal, calculated_at, ticker, short_seller_name, "
            "discretionary_investment_contractor_name, investment_fund_name, "
            "short_ratio, short_shares, short_trading_units, previous_reported_at, "
            "previous_short_ratio, notes, short_seller_address, "
            "discretionary_investment_contractor_address "
            "FROM jquants_short_sale_reports "
            "WHERE disclosed_at BETWEEN ? AND ? "
            "ORDER BY disclosed_at, source_ordinal",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        conn.close()
    return [
        JQuantsShortSaleReport(
            disclosed_at=date.fromisoformat(str(row[0])),
            source_ordinal=int(row[1]),
            calculated_at=date.fromisoformat(str(row[2])),
            ticker=str(row[3]),
            short_seller_name=str(row[4]),
            discretionary_investment_contractor_name=str(row[5]),
            investment_fund_name=str(row[6]),
            short_ratio=float(row[7]) if row[7] is not None else None,
            short_shares=int(row[8]) if row[8] is not None else None,
            short_trading_units=int(row[9]) if row[9] is not None else None,
            previous_reported_at=optional_date(row[10]),
            previous_short_ratio=optional_float(row[11]),
            notes=str(row[12]) if row[12] is not None else None,
            short_seller_address=str(row[13]) if row[13] is not None else None,
            discretionary_investment_contractor_address=(
                str(row[14]) if row[14] is not None else None
            ),
        )
        for row in rows
    ]


def read_weekly_margin(sqlite_path: Path, week_end: date) -> list[JQuantsWeeklyMargin] | None:
    """Return one balance date's rows, or None when it has not been examined.

    A week the exchange skipped is stored as a coverage row with no rows behind
    it, so an empty list and None mean different things: the first says a fetch
    returned no balance date, the second says nobody has looked. The bootstrap
    planner separately checks whether an empty fetch happened before publication
    and must be refreshed.
    """
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    iso = week_end.isoformat()
    try:
        coverage = conn.execute(
            "SELECT status FROM source_coverage WHERE source = ? AND coverage_key = ?",
            (WEEKLY_MARGIN_SOURCE, weekly_margin_coverage_key(week_end)),
        ).fetchone()
        if coverage is None or coverage[0] != "ok":
            return None
        rows = conn.execute(
            "SELECT ticker, long_vol, short_vol, long_std_vol, long_neg_vol, "
            "short_std_vol, short_neg_vol, issue_type "
            "FROM jquants_weekly_margin WHERE week_end = ? ORDER BY ticker",
            (iso,),
        ).fetchall()
    finally:
        conn.close()
    return [
        JQuantsWeeklyMargin(
            ticker=str(row[0]),
            week_end=week_end,
            long_vol=_opt_float_value(row[1]),
            short_vol=_opt_float_value(row[2]),
            long_std_vol=_opt_float_value(row[3]),
            long_neg_vol=_opt_float_value(row[4]),
            short_std_vol=_opt_float_value(row[5]),
            short_neg_vol=_opt_float_value(row[6]),
            issue_type=str(row[7]) if row[7] is not None else None,
        )
        for row in rows
    ]


def read_margin_alerts(
    sqlite_path: Path, start: date, end: date
) -> list[JQuantsMarginAlert] | None:
    """Read designated-issue daily facts only for a covered publication range."""
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        if not range_covered(conn, MARGIN_ALERT_SOURCE, start, end):
            return None
        rows = conn.execute(
            "SELECT publication_date, ticker, applied_date, publication_reason, "
            "short_outstanding, short_change, short_ratio, long_outstanding, long_change, "
            "long_ratio, short_long_ratio, short_negotiable_outstanding, "
            "short_negotiable_change, short_standard_outstanding, short_standard_change, "
            "long_negotiable_outstanding, long_negotiable_change, long_standard_outstanding, "
            "long_standard_change, tse_margin_regulation_classification "
            "FROM jquants_margin_alerts WHERE publication_date BETWEEN ? AND ? "
            "ORDER BY publication_date, ticker, applied_date",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        conn.close()
    return [
        JQuantsMarginAlert(
            publication_date=date.fromisoformat(str(row[0])),
            ticker=str(row[1]),
            applied_date=date.fromisoformat(str(row[2])),
            publication_reason=str(row[3]) if row[3] is not None else None,
            short_outstanding=_opt_float_value(row[4]),
            short_change=_opt_float_value(row[5]),
            short_ratio=_opt_float_value(row[6]),
            long_outstanding=_opt_float_value(row[7]),
            long_change=_opt_float_value(row[8]),
            long_ratio=_opt_float_value(row[9]),
            short_long_ratio=_opt_float_value(row[10]),
            short_negotiable_outstanding=_opt_float_value(row[11]),
            short_negotiable_change=_opt_float_value(row[12]),
            short_standard_outstanding=_opt_float_value(row[13]),
            short_standard_change=_opt_float_value(row[14]),
            long_negotiable_outstanding=_opt_float_value(row[15]),
            long_negotiable_change=_opt_float_value(row[16]),
            long_standard_outstanding=_opt_float_value(row[17]),
            long_standard_change=_opt_float_value(row[18]),
            tse_margin_regulation_classification=(str(row[19]) if row[19] is not None else None),
        )
        for row in rows
    ]


def read_all_issues_daily_margin(
    sqlite_path: Path, balance_date: date
) -> list[JQuantsAllIssuesDailyMargin] | None:
    """Read one all-issues daily balance, distinct from legacy weekly history."""
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        coverage = conn.execute(
            "SELECT status, record_count FROM source_coverage "
            "WHERE source = ? AND coverage_key = ?",
            (
                ALL_ISSUES_DAILY_MARGIN_SOURCE,
                all_issues_daily_margin_coverage_key(balance_date),
            ),
        ).fetchone()
        if coverage is None or coverage[0] != "ok" or int(coverage[1] or 0) == 0:
            return None
        rows = conn.execute(
            "SELECT ticker, long_vol, short_vol, long_std_vol, long_neg_vol, "
            "short_std_vol, short_neg_vol, issue_type "
            "FROM jquants_all_issues_daily_margin WHERE balance_date = ? ORDER BY ticker",
            (balance_date.isoformat(),),
        ).fetchall()
    finally:
        conn.close()
    return [
        JQuantsAllIssuesDailyMargin(
            ticker=str(row[0]),
            balance_date=balance_date,
            long_vol=_opt_float_value(row[1]),
            short_vol=_opt_float_value(row[2]),
            long_std_vol=_opt_float_value(row[3]),
            long_neg_vol=_opt_float_value(row[4]),
            short_std_vol=_opt_float_value(row[5]),
            short_neg_vol=_opt_float_value(row[6]),
            issue_type=str(row[7]) if row[7] is not None else None,
        )
        for row in rows
    ]


def _opt_float_value(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def published_margin_week_ends(sqlite_path: Path, asof: date) -> list[date]:
    """Balance dates whose publication had already happened by `asof`, ascending.

    A balance date is not usable on the day it describes: the exchange publishes it
    days later, so joining on the balance date alone would read Friday's positioning
    into Friday's decision. Trading days come from the stored bar dates. Missing
    trading dates can delay inferred publication; this does not certify the
    historical calendar as complete.
    """
    if not sqlite_path.exists():
        return []
    conn = connect_current(sqlite_path)
    if conn is None:
        return []
    try:
        # Only balance dates the reader can actually serve. `read_weekly_margin`
        # refuses a date whose coverage is not `ok`, so listing one here would put
        # an unreadable date at the head of the list and blank the whole cohort
        # rather than falling back to the week before it.
        # Selected by the reader's own predicate, the coverage key, so the two
        # cannot disagree about which balance date a coverage row describes.
        readable = {
            str(row[0])
            for row in conn.execute(
                "SELECT coverage_key FROM source_coverage "
                "WHERE source = ? AND status = 'ok' AND record_count > 0",
                (WEEKLY_MARGIN_SOURCE,),
            )
        }
        # Not filtered on `asof` here: the publication-lag test below is the one that
        # decides usability, and a balance date the market has not reached has no
        # publication day inside `trading_days`, which stops at `asof`. Filtering twice
        # would put the point-in-time rule in two places that could disagree.
        week_ends = [
            date.fromisoformat(str(row[0]))
            for row in conn.execute(
                "SELECT DISTINCT week_end FROM jquants_weekly_margin ORDER BY week_end"
            )
            if weekly_margin_coverage_key(date.fromisoformat(str(row[0]))) in readable
        ]
        if not week_ends:
            return []
        trading_days = [
            date.fromisoformat(str(row[0]))
            for row in conn.execute(
                "SELECT DISTINCT traded_at FROM jquants_daily_bars "
                "WHERE traded_at > ? AND traded_at <= ? ORDER BY traded_at",
                (week_ends[0].isoformat(), asof.isoformat()),
            )
        ]
    finally:
        conn.close()
    # `trading_days` stops at `asof`, so a balance date's publication day is inside
    # the list once observed, but same-day publications remain excluded at the close.
    return [
        week_end
        for week_end in week_ends
        if _publication_has_happened(
            trading_days, week_end, asof=asof, trading_day_lag=MARGIN_PUBLICATION_TRADING_DAYS
        )
    ]


def published_margin_balance_dates(
    sqlite_path: Path,
    asof: date,
) -> list[tuple[date, MarginCadence]]:
    """Every published balance date usable at `asof`, ascending, tagged by series.

    The exchange stops publishing the weekly balance after 2026-09-18 and starts an
    all-issues daily balance on 2026-09-25, so the axes see one column that changes
    cadence rather than two series. The two are disjoint by construction — the
    balance-date domains are enforced on the way in — so the union needs no
    tie-break and the six-day gap between them is simply a gap.

    """
    weekly = published_margin_week_ends(sqlite_path, asof)
    daily = _published_all_issues_daily_balance_dates(sqlite_path, asof)
    tagged: list[tuple[date, MarginCadence]] = [(day, "weekly") for day in weekly]
    tagged.extend((day, "daily") for day in daily)
    tagged.sort()
    return tagged


def _published_all_issues_daily_balance_dates(sqlite_path: Path, asof: date) -> list[date]:
    """Daily balance dates whose next-business-day publication is already past."""
    if not sqlite_path.exists():
        return []
    conn = connect_current(sqlite_path)
    if conn is None:
        return []
    try:
        readable = {
            str(row[0])
            for row in conn.execute(
                "SELECT coverage_key FROM source_coverage "
                "WHERE source = ? AND status = 'ok' AND record_count > 0",
                (ALL_ISSUES_DAILY_MARGIN_SOURCE,),
            )
        }
        balance_dates = [
            date.fromisoformat(str(row[0]))
            for row in conn.execute(
                "SELECT DISTINCT balance_date FROM jquants_all_issues_daily_margin "
                "WHERE balance_date <= ? ORDER BY balance_date",
                (asof.isoformat(),),
            )
            if all_issues_daily_margin_coverage_key(date.fromisoformat(str(row[0]))) in readable
        ]
        if not balance_dates:
            return []
        trading_days = [
            date.fromisoformat(str(row[0]))
            for row in conn.execute(
                "SELECT DISTINCT traded_at FROM jquants_daily_bars "
                "WHERE traded_at > ? AND traded_at <= ? ORDER BY traded_at",
                (balance_dates[0].isoformat(), asof.isoformat()),
            )
        ]
    finally:
        conn.close()
    return [
        balance_date
        for balance_date in balance_dates
        if _publication_has_happened(
            trading_days,
            balance_date,
            asof=asof,
            trading_day_lag=ALL_ISSUES_DAILY_PUBLICATION_TRADING_DAYS,
        )
    ]
