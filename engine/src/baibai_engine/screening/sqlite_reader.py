"""Read helpers that pull screening fundamentals/regulation inputs from SQLite.

The functions are deliberately permissive: a missing SQLite file or a source
that has not been fetched yet returns `None` so bootstrap/fetch commands can
populate the missing coverage. `screening run` performs a separate preflight
coverage check and must not fall back to provider APIs. Price/calendar reads
live in `baibai_engine.market.store`; this module owns master / fin summaries /
earnings calendar / EDINET / JPX.
"""

from __future__ import annotations

import sqlite3
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from baibai_engine.foundation.date_utils import weekday_distance
from baibai_engine.foundation.time import JST
from baibai_engine.market.sqlite import (
    connect_current,
    covered_intervals,
    optional_date,
    optional_float,
    range_covered,
)

from . import master_snapshot as master_contract
from .edinet_store import (
    EDINETMetricBaseline as EDINETMetricBaseline,
)
from .edinet_store import (
    EDINETMetricBaselineError as EDINETMetricBaselineError,
)
from .edinet_store import (
    EDINETMetricBaselineRow as EDINETMetricBaselineRow,
)
from .edinet_store import (
    read_edinet_documents as read_edinet_documents,
)
from .edinet_store import (
    read_edinet_metric_baseline as read_edinet_metric_baseline,
)
from .edinet_store import (
    read_edinet_metrics as read_edinet_metrics,
)
from .edinet_store import (
    read_unfinalized_edinet_document_dates as read_unfinalized_edinet_document_dates,
)
from .margin_metrics import MarginBalance
from .margin_publication import (
    ALL_ISSUES_DAILY_FIRST_BALANCE_DATE,
    ALL_ISSUES_DAILY_PUBLICATION_CONFIRMED,
    LEGACY_WEEKLY_LAST_BALANCE_DATE,
)
from .providers.jpx import (
    JPXEarningsCalendarEntry,
    JPXEarningsCalendarSnapshot,
    JPXRegulationSnapshot,
)
from .providers.jquants import (
    JQuantsAllIssuesDailyMargin,
    JQuantsFinancialSummary,
    JQuantsMarginAlert,
    JQuantsProviderError,
    JQuantsShortSaleReport,
    JQuantsWeeklyMargin,
)
from .schema import SecurityMaster
from .sqlite_cache.jquants import (
    ALL_ISSUES_DAILY_MARGIN_SOURCE,
    MARGIN_ALERT_SOURCE,
    SHORT_SALE_REPORT_SOURCE,
    WEEKLY_MARGIN_SOURCE,
    all_issues_daily_margin_coverage_key,
    weekly_margin_coverage_key,
)

SHORT_SALE_REPORT_DATASET_FLOOR = date(2013, 11, 7)
SHORT_SALE_REPORTING_THRESHOLD = 0.005


@dataclass(frozen=True, slots=True)
class ReportedShortMetric:
    ratio: float
    breadth: int
    latest_disclosed_at: date


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
            "previous_short_ratio, is_cancellation, notes "
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
            is_cancellation=bool(row[12]),
            notes=str(row[13]) if row[13] is not None else None,
        )
        for row in rows
    ]


def read_reported_short_metrics(
    sqlite_path: Path, asof: date
) -> dict[str, ReportedShortMetric | None] | None:
    """Aggregate each reporter's latest disclosed state at a cohort boundary.

    ``None`` means the source cannot prove continuous coverage from the official
    dataset floor. A mapping means coverage is complete; a ticker absent from it
    is therefore an explicit below-threshold/no-report observation.
    """
    if not sqlite_path.exists() or asof < SHORT_SALE_REPORT_DATASET_FLOOR:
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        if not range_covered(conn, SHORT_SALE_REPORT_SOURCE, SHORT_SALE_REPORT_DATASET_FLOOR, asof):
            return None
        ambiguous_rows = conn.execute(
            """
            WITH dated AS (
              SELECT ticker, short_seller_name,
                     discretionary_investment_contractor_name,
                     investment_fund_name,
                     DENSE_RANK() OVER (
                       PARTITION BY ticker, short_seller_name,
                                    discretionary_investment_contractor_name,
                                    investment_fund_name
                       ORDER BY calculated_at DESC, disclosed_at DESC
                     ) AS recency
              FROM jquants_short_sale_reports
              WHERE disclosed_at <= ? AND calculated_at <= ?
            )
            SELECT ticker
            FROM dated
            WHERE recency = 1
            GROUP BY ticker, short_seller_name,
                     discretionary_investment_contractor_name,
                     investment_fund_name
            HAVING COUNT(*) > 1
            """,
            (asof.isoformat(), asof.isoformat()),
        ).fetchall()
        rows = conn.execute(
            """
            WITH ranked AS (
              SELECT ticker, short_seller_name,
                     discretionary_investment_contractor_name,
                     investment_fund_name, short_ratio, disclosed_at,
                     ROW_NUMBER() OVER (
                       PARTITION BY ticker, short_seller_name,
                                    discretionary_investment_contractor_name,
                                    investment_fund_name
                       ORDER BY calculated_at DESC, disclosed_at DESC, source_ordinal DESC
                     ) AS recency
              FROM jquants_short_sale_reports
              WHERE disclosed_at <= ? AND calculated_at <= ?
            )
            SELECT ticker, SUM(short_ratio), COUNT(*), MAX(disclosed_at)
            FROM ranked
            WHERE recency = 1 AND short_ratio >= ?
            GROUP BY ticker
            ORDER BY ticker
            """,
            (asof.isoformat(), asof.isoformat(), SHORT_SALE_REPORTING_THRESHOLD),
        ).fetchall()
    finally:
        conn.close()
    metrics: dict[str, ReportedShortMetric | None] = {
        str(row[0]): ReportedShortMetric(
            ratio=float(row[1]),
            breadth=int(row[2]),
            latest_disclosed_at=date.fromisoformat(str(row[3])),
        )
        for row in rows
    }
    for (ticker,) in ambiguous_rows:
        metrics[str(ticker)] = None
    return metrics


@dataclass(frozen=True, slots=True)
class MasterSnapshotRead:
    """A point-in-time security-master read with its source snapshot status."""

    masters: tuple[SecurityMaster, ...]
    snapshot_date: date | None
    status: str


def read_eq_master_asof(sqlite_path: Path, asof: date) -> MasterSnapshotRead:
    """Read the newest master snapshot at or before ``asof``.

    Snapshot collection starts at the first locally stored date, so cohorts
    before it can never have a point-in-time snapshot. For those dates this
    falls back to the earliest stored snapshot and labels the read
    ``future_snapshot``: the approximation keeps historical cohorts measurable
    for diagnostics, while the calibration authority contract excludes every
    non-``exact_date`` read from production evidence.
    """
    if not sqlite_path.exists():
        return MasterSnapshotRead((), None, "unavailable")
    conn = connect_current(sqlite_path)
    if conn is None:
        return MasterSnapshotRead((), None, "unavailable")
    try:
        status = "prior_snapshot"
        snapshot = conn.execute(
            "SELECT MAX(snapshot_date) FROM jquants_master_snapshots WHERE snapshot_date <= ?",
            (asof.isoformat(),),
        ).fetchone()[0]
        if snapshot is None:
            status = "future_snapshot"
            snapshot = conn.execute(
                "SELECT MIN(snapshot_date) FROM jquants_master_snapshots"
            ).fetchone()[0]
        if snapshot is None:
            return MasterSnapshotRead((), None, "unavailable")
        rows = conn.execute(
            "SELECT ticker, name, market, sector_33, is_common_stock "
            "FROM jquants_master_snapshots WHERE snapshot_date = ? ORDER BY ticker",
            (str(snapshot),),
        ).fetchall()
    finally:
        conn.close()
    snapshot_date = date.fromisoformat(str(snapshot))
    return MasterSnapshotRead(
        tuple(
            SecurityMaster(
                code=ticker,
                name=str(name or ""),
                market_segment=str(market or ""),
                sector_33=str(sector_33 or ""),
                is_common_stock=bool(is_common),
            )
            for ticker, name, market, sector_33, is_common in rows
        ),
        snapshot_date,
        "exact_date" if snapshot_date == asof else status,
    )


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


def final_legacy_week_requires_refresh(sqlite_path: Path) -> bool:
    """Whether the final weekly balance still lacks a non-empty clean snapshot."""
    if not sqlite_path.exists():
        return True
    conn = connect_current(sqlite_path)
    if conn is None:
        return True
    try:
        row = conn.execute(
            "SELECT status, record_count FROM source_coverage "
            "WHERE source = ? AND coverage_key = ?",
            (
                WEEKLY_MARGIN_SOURCE,
                weekly_margin_coverage_key(LEGACY_WEEKLY_LAST_BALANCE_DATE),
            ),
        ).fetchone()
    finally:
        conn.close()
    return row is None or row[0] != "ok" or int(row[1] or 0) == 0


def weekly_margin_empty_requires_refresh(
    sqlite_path: Path,
    week_end: date,
    *,
    asof: date,
) -> bool:
    """Whether an empty weekly snapshot was fetched before publication was due.

    Empty is a valid final answer for a week the exchange skipped, but only after
    the second-trading-day publication point.  Older code could ask sooner and
    persist that temporary empty response as clean coverage.  The fetch timestamp
    distinguishes that state from an empty response observed after publication.
    """
    if not sqlite_path.exists():
        return False
    conn = connect_current(sqlite_path)
    if conn is None:
        return False
    try:
        coverage = conn.execute(
            "SELECT status, record_count, fetched_at_utc FROM source_coverage "
            "WHERE source = ? AND coverage_key = ?",
            (WEEKLY_MARGIN_SOURCE, weekly_margin_coverage_key(week_end)),
        ).fetchone()
        if coverage is None or coverage[0] != "ok" or int(coverage[1] or 0) != 0:
            return False
        trading_days = [
            date.fromisoformat(str(row[0]))
            for row in conn.execute(
                "SELECT DISTINCT traded_at FROM jquants_daily_bars "
                "WHERE traded_at > ? AND traded_at <= ? ORDER BY traded_at",
                (week_end.isoformat(), asof.isoformat()),
            )
        ]
    finally:
        conn.close()
    publication_day = _publication_trading_day(
        trading_days,
        week_end,
        trading_day_lag=MARGIN_PUBLICATION_TRADING_DAYS,
    )
    if publication_day is None or publication_day > asof:
        return False
    try:
        fetched_at = datetime.fromisoformat(str(coverage[2]))
    except (TypeError, ValueError):
        return True
    if fetched_at.tzinfo is None:
        return True
    return fetched_at.astimezone(JST).date() <= publication_day


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
            "ORDER BY publication_date, ticker",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        conn.close()
    return [
        JQuantsMarginAlert(
            publication_date=date.fromisoformat(str(row[0])),
            ticker=str(row[1]),
            applied_date=optional_date(row[2]),
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


# The exchange publishes a week's margin balances on the second trading day after
# the balance date, around 16:30 JST. Counting trading days rather than adding a
# fixed offset is what makes the rule survive the balance dates that land on a
# Thursday or Wednesday because the week's later days were closed.
MARGIN_PUBLICATION_TRADING_DAYS = 2


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


def published_margin_week_ends(sqlite_path: Path, asof: date) -> list[date]:
    """Balance dates whose publication had already happened by `asof`, ascending.

    A balance date is not usable on the day it describes: the exchange publishes it
    days later, so joining on the balance date alone would read Friday's positioning
    into Friday's decision. Trading days come from the bar rows, which are the only
    complete record of which days the market was open across the stored history.
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
        week_ends = [
            date.fromisoformat(str(row[0]))
            for row in conn.execute(
                "SELECT DISTINCT week_end FROM jquants_weekly_margin "
                "WHERE week_end <= ? ORDER BY week_end",
                (asof.isoformat(),),
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
    # the list exactly when it has already happened.
    return [
        week_end
        for week_end in week_ends
        if _publication_has_happened(
            trading_days, week_end, asof=asof, trading_day_lag=MARGIN_PUBLICATION_TRADING_DAYS
        )
    ]


# The all-issues daily balance is published on the next business day, one trading
# day sooner than the weekly series waits. The lag is per cadence rather than per
# store because both series are read through the same column.
ALL_ISSUES_DAILY_PUBLICATION_TRADING_DAYS = 1

MarginCadence = Literal["weekly", "daily"]


def published_margin_balance_dates(
    sqlite_path: Path,
    asof: date,
    *,
    publication_confirmed: bool = ALL_ISSUES_DAILY_PUBLICATION_CONFIRMED,
) -> list[tuple[date, MarginCadence]]:
    """Every published balance date usable at `asof`, ascending, tagged by series.

    The exchange stops publishing the weekly balance after 2026-09-18 and starts an
    all-issues daily balance on 2026-09-25, so the axes see one column that changes
    cadence rather than two series. The two are disjoint by construction — the
    balance-date domains are enforced on the way in — so the union needs no
    tie-break and the six-day gap between them is simply a gap.

    `publication_confirmed` is the transition's activation flag. While it is false
    this returns exactly the weekly dates, which is what the whole daily half is
    inert behind.
    """
    weekly = published_margin_week_ends(sqlite_path, asof)
    if not publication_confirmed:
        return [(day, "weekly") for day in weekly]
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


def weekly_margin_candidate_dates(
    sqlite_path: Path,
    start: date,
    end: date,
    *,
    publication_asof: date | None = None,
) -> list[date]:
    """The last stored trading day of each week in `[start, end]`, ascending.

    The exchange's balance date is that day in most weeks and an earlier one when
    the week's later days were closed — and some weeks have no balance date at all,
    even weeks the market traded. Rather than encode that calendar, this proposes
    one candidate per week and lets the fetch record an empty answer as the week's
    fact. When `publication_asof` is supplied, candidates whose second-trading-day
    publication is still in the future are withheld from the daily bootstrap.
    """
    balance_end = min(end, LEGACY_WEEKLY_LAST_BALANCE_DATE)
    if not sqlite_path.exists() or start > balance_end:
        return []
    calendar_end = max(balance_end, publication_asof or balance_end)
    conn = connect_current(sqlite_path)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT DISTINCT traded_at FROM jquants_daily_bars "
            "WHERE traded_at BETWEEN ? AND ? ORDER BY traded_at",
            (start.isoformat(), calendar_end.isoformat()),
        ).fetchall()
    finally:
        conn.close()
    trading_days: list[date] = []
    for (value,) in rows:
        try:
            day = date.fromisoformat(str(value))
        except ValueError:
            continue
        trading_days.append(day)
    last_of_week: dict[tuple[int, int], date] = {}
    for day in (candidate for candidate in trading_days if candidate <= balance_end):
        year, week, _ = day.isocalendar()
        last_of_week[(year, week)] = day
    # A week that is still running has a last stored trading day that moves forward
    # each day, so proposing it spends one call per run on a date that is not a
    # balance date; nothing is lost by waiting, because the publication lag means a
    # balance date is not usable until two trading days after the week closes. The
    # week is in progress only when `end` reaches the newest day the store knows —
    # a window that ends in the past has a complete final week and must keep it.
    newest = max(last_of_week.values(), default=None)
    stored_latest = _latest_stored_trading_day(conn_path=sqlite_path)
    if (
        newest is not None
        and stored_latest is not None
        and balance_end.isocalendar()[:2] == stored_latest.isocalendar()[:2]
    ):
        last_of_week.pop(newest.isocalendar()[:2], None)
    candidates = sorted(last_of_week.values())
    if publication_asof is None:
        return candidates
    return [
        candidate
        for candidate in candidates
        if (
            publication_day := _publication_trading_day(
                trading_days,
                candidate,
                trading_day_lag=MARGIN_PUBLICATION_TRADING_DAYS,
            )
        )
        is not None
        and publication_day <= publication_asof
    ]


def all_issues_daily_margin_candidate_dates(
    sqlite_path: Path,
    start: date,
    asof: date,
    *,
    publication_confirmed: bool = ALL_ISSUES_DAILY_PUBLICATION_CONFIRMED,
) -> list[date]:
    """Unfetched daily balance dates that have a later trading-day publication."""
    if not publication_confirmed:
        return []
    start = max(start, ALL_ISSUES_DAILY_FIRST_BALANCE_DATE)
    if not sqlite_path.exists() or start >= asof:
        return []
    conn = connect_current(sqlite_path)
    if conn is None:
        return []
    try:
        trading_days = [
            date.fromisoformat(str(row[0]))
            for row in conn.execute(
                "SELECT DISTINCT traded_at FROM jquants_daily_bars "
                "WHERE traded_at BETWEEN ? AND ? ORDER BY traded_at",
                (start.isoformat(), asof.isoformat()),
            )
        ]
        covered = {
            str(row[0])
            for row in conn.execute(
                "SELECT coverage_key FROM source_coverage "
                "WHERE source = ? AND status = 'ok' AND record_count > 0",
                (ALL_ISSUES_DAILY_MARGIN_SOURCE,),
            )
        }
    finally:
        conn.close()
    # Publication is on the following business day, so the newest trading day has
    # not yet become available. An empty all-issues response cannot describe a
    # complete business-day snapshot and remains a retry target.
    return [
        day for day in trading_days[:-1] if all_issues_daily_margin_coverage_key(day) not in covered
    ]


def all_issues_daily_margin_backfill_candidate_dates(
    sqlite_path: Path,
    start: date,
    end: date,
    *,
    publication_confirmed: bool = ALL_ISSUES_DAILY_PUBLICATION_CONFIRMED,
) -> list[date]:
    """Every uncovered balance date in an inclusive historical backfill window."""
    if not publication_confirmed:
        return []
    start = max(start, ALL_ISSUES_DAILY_FIRST_BALANCE_DATE)
    if not sqlite_path.exists() or start > end:
        return []
    conn = connect_current(sqlite_path)
    if conn is None:
        return []
    try:
        trading_days = [
            date.fromisoformat(str(row[0]))
            for row in conn.execute(
                "SELECT DISTINCT traded_at FROM jquants_daily_bars "
                "WHERE traded_at BETWEEN ? AND ? ORDER BY traded_at",
                (start.isoformat(), end.isoformat()),
            )
        ]
        covered = {
            str(row[0])
            for row in conn.execute(
                "SELECT coverage_key FROM source_coverage "
                "WHERE source = ? AND status = 'ok' AND record_count > 0",
                (ALL_ISSUES_DAILY_MARGIN_SOURCE,),
            )
        }
    finally:
        conn.close()
    return [day for day in trading_days if all_issues_daily_margin_coverage_key(day) not in covered]


def _latest_stored_trading_day(*, conn_path: Path) -> date | None:
    conn = connect_current(conn_path)
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT MAX(traded_at) FROM jquants_daily_bars").fetchone()
    finally:
        conn.close()
    if row is None or row[0] is None:
        return None
    try:
        return date.fromisoformat(str(row[0]))
    except ValueError:
        return None


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
    *,
    publication_confirmed: bool = ALL_ISSUES_DAILY_PUBLICATION_CONFIRMED,
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
    published = published_margin_balance_dates(
        sqlite_path, asof, publication_confirmed=publication_confirmed
    )
    if not published or (asof - published[-1][0]).days > MARGIN_MAX_STALE_DAYS:
        return {}, {}
    latest = _read_margin_balances(sqlite_path, *published[-1])
    prior: dict[str, MarginBalance] = {}
    weekly_steps = _last_balance_date_of_each_week(published)
    if len(weekly_steps) > MARGIN_DELTA_WEEKS:
        prior = _read_margin_balances(sqlite_path, *weekly_steps[-1 - MARGIN_DELTA_WEEKS])
    return latest, prior


def read_eq_master_exact(sqlite_path: Path, asof: date) -> list[SecurityMaster] | None:
    """Return an exact snapshot only when its canonical coverage also agrees."""
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    iso = asof.isoformat()
    try:
        coverage_rows = conn.execute(
            "SELECT coverage_start, coverage_end, record_count, status, error "
            "FROM source_coverage WHERE source = ? AND coverage_key = ?",
            (master_contract.MASTER_SOURCE, master_contract.master_coverage_key(asof)),
        ).fetchall()
        if len(coverage_rows) != 1:
            return None
        coverage_start, coverage_end, record_count, status, error = coverage_rows[0]
        if coverage_start != iso or coverage_end != iso or status != "ok" or error is not None:
            return None
        expected_count = master_contract.master_coverage_count(record_count)
        if expected_count is None:
            return None
        rows = conn.execute(
            "SELECT ticker, name, market, sector_33, is_common_stock "
            "FROM jquants_master_snapshots WHERE snapshot_date = ? ORDER BY ticker",
            (iso,),
        ).fetchall()
        if len(rows) != expected_count or expected_count <= 0:
            return None
        common_count = sum(row[4] == 1 for row in rows)
        if common_count < master_contract.MIN_COMMON_STOCK_MASTER_ROWS or common_count != len(rows):
            return None
        if any(not str(value or "").strip() for row in rows for value in row[:4]):
            return None
    except (sqlite3.OperationalError, TypeError, ValueError):
        return None
    finally:
        conn.close()
    try:
        return _materialize_masters(rows)
    except (TypeError, ValueError):
        return None


def _materialize_masters(rows: list[tuple[Any, ...]]) -> list[SecurityMaster]:
    return [
        SecurityMaster(
            code=ticker,
            name=str(name or ""),
            market_segment=str(market or ""),
            sector_33=str(sector_33 or ""),
            is_common_stock=bool(is_common),
        )
        for ticker, name, market, sector_33, is_common in rows
    ]


def fin_summaries_covered(sqlite_path: Path, start: date, end: date) -> bool:
    """Answer whether the cache can serve `[start, end]` without building the rows.

    Deliberately the same predicate `read_fin_summaries` gates on, asked without
    the materialisation: a chunk-skip test and a fetch planner need the boolean,
    not the summaries. Sharing `range_covered` is what keeps them agreeing with
    the reader — a window recorded `ok` with zero rows is not a claim, and a
    planner that treated it as one would leave the reader unable to serve the
    range it just fetched.
    """
    if not sqlite_path.exists():
        return False
    conn = connect_current(sqlite_path)
    if conn is None:
        return False
    try:
        return range_covered(conn, "jquants_fin_summaries", start, end)
    finally:
        conn.close()


def fin_summaries_readable_from(sqlite_path: Path, asof: date) -> date | None:
    """Where the store's **continuous** history ending at `asof` begins.

    The rows the store holds and the range it may serve are two different facts, and
    the subscription window moves. A filing fetched while the window still reached
    that far back keeps its row after the window passes it, so the oldest row can sit
    outside the coverage. A caller that takes its floor from the oldest row then asks
    for a range `read_fin_summaries` refuses, and a handful of rows at the far edge of
    the history takes every read down.

    Answering only for a continuous history is what keeps that relief from covering a
    real hole. A store that simply does not reach further back has one window and
    gets its floor; a store missing months in the middle has an older window too, and
    gets None so the caller keeps its own floor and the read reports the range it
    cannot serve. Silently starting after a gap would turn an outage into a quietly
    shorter history, which no diagnostic distinguishes from a young store.

    None also covers "no window holds `asof`" -- the same outage seen from the other
    end.
    """
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        intervals = covered_intervals(conn, "jquants_fin_summaries")
    finally:
        conn.close()
    for index, (interval_start, interval_end) in enumerate(intervals):
        if interval_start <= asof <= interval_end:
            return interval_start if index == 0 else None
    return None


def count_fin_summaries(sqlite_path: Path, start: date, end: date) -> int:
    """Count stored summary rows in `[start, end]`; 0 when the store cannot be read."""
    if not sqlite_path.exists():
        return 0
    conn = connect_current(sqlite_path)
    if conn is None:
        return 0
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM jquants_fin_summaries WHERE disclosed_at BETWEEN ? AND ?",
            (start.isoformat(), end.isoformat()),
        ).fetchone()
    finally:
        conn.close()
    return int(row[0] or 0) if row is not None else 0


def read_fin_summaries(
    sqlite_path: Path, start: date, end: date
) -> list[JQuantsFinancialSummary] | None:
    """Return financial summaries for `[start, end]` from SQLite, or `None`
    when the cache cannot serve the full range.
    """
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        if not range_covered(conn, "jquants_fin_summaries", start, end):
            return None
        rows = conn.execute(
            "SELECT ticker, disclosed_at, forecast_eps, eps_ttm, bps, "
            "shares_outstanding, sales, cfo, cash_eq, total_assets, equity, "
            "operating_profit, ordinary_profit, profit, "
            "forecast_profit, forecast_ordinary_profit, "
            "fiscal_period, fiscal_year_end, period_start, period_end, "
            "dps_actual_annual, dps_forecast_annual, "
            "treasury_shares, equity_to_asset_ratio, "
            "dividend_q1, dividend_interim, dividend_q3, dividend_year_end, "
            "dividend_total_annual, average_shares "
            "FROM jquants_fin_summaries WHERE disclosed_at BETWEEN ? AND ? "
            "ORDER BY ticker, disclosed_at",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        conn.close()

    summaries: list[JQuantsFinancialSummary] = []
    for row in rows:
        (
            ticker,
            disclosed_at,
            forecast_eps,
            eps_ttm,
            bps,
            shares_outstanding,
            sales,
            cfo,
            cash_eq,
            total_assets,
            equity,
            operating_profit,
            ordinary_profit,
            profit,
            forecast_profit,
            forecast_ordinary_profit,
            fiscal_period,
            fiscal_year_end,
            period_start,
            period_end,
            dps_actual_annual,
            dps_forecast_annual,
            treasury_shares,
            equity_to_asset_ratio,
            dividend_q1,
            dividend_interim,
            dividend_q3,
            dividend_year_end,
            dividend_total_annual,
            average_shares,
        ) = row
        try:
            summaries.append(
                JQuantsFinancialSummary(
                    ticker=str(ticker),
                    disclosed_at=date.fromisoformat(disclosed_at),
                    forecast_eps=optional_float(forecast_eps),
                    eps_ttm=optional_float(eps_ttm),
                    bps=optional_float(bps),
                    shares_outstanding=optional_float(shares_outstanding),
                    sales=optional_float(sales),
                    cfo=optional_float(cfo),
                    cash_eq=optional_float(cash_eq),
                    total_assets=optional_float(total_assets),
                    equity=optional_float(equity),
                    operating_profit=optional_float(operating_profit),
                    ordinary_profit=optional_float(ordinary_profit),
                    profit=optional_float(profit),
                    forecast_profit=optional_float(forecast_profit),
                    forecast_ordinary_profit=optional_float(forecast_ordinary_profit),
                    fiscal_period=fiscal_period if fiscal_period else None,
                    fiscal_year_end=optional_date(fiscal_year_end),
                    period_start=optional_date(period_start),
                    period_end=optional_date(period_end),
                    dps_actual_annual=optional_float(dps_actual_annual),
                    dps_forecast_annual=optional_float(dps_forecast_annual),
                    treasury_shares=optional_float(treasury_shares),
                    equity_to_asset_ratio=optional_float(equity_to_asset_ratio),
                    dividend_q1=optional_float(dividend_q1),
                    dividend_interim=optional_float(dividend_interim),
                    dividend_q3=optional_float(dividend_q3),
                    dividend_year_end=optional_float(dividend_year_end),
                    dividend_total_annual=optional_float(dividend_total_annual),
                    average_shares=optional_float(average_shares),
                )
            )
        except (TypeError, ValueError) as exc:
            raise JQuantsProviderError(
                f"corrupt SQLite row in jquants_fin_summaries for {ticker}: {exc}"
            ) from exc
    return summaries


def read_fy_summaries(
    sqlite_path: Path, start: date, end: date
) -> list[JQuantsFinancialSummary] | None:
    """Read only FY EPS rows over a fully covered financial-summary range."""
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        if not range_covered(conn, "jquants_fin_summaries", start, end):
            return None
        rows = conn.execute(
            "SELECT ticker, disclosed_at, eps_ttm, fiscal_period, fiscal_year_end "
            "FROM jquants_fin_summaries WHERE disclosed_at BETWEEN ? AND ? "
            "AND fiscal_period = 'FY' ORDER BY ticker, disclosed_at",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        conn.close()

    summaries: list[JQuantsFinancialSummary] = []
    for ticker, disclosed_at, eps_ttm, fiscal_period, fiscal_year_end in rows:
        try:
            summaries.append(
                JQuantsFinancialSummary(
                    ticker=str(ticker),
                    disclosed_at=date.fromisoformat(disclosed_at),
                    eps_ttm=optional_float(eps_ttm),
                    fiscal_period=str(fiscal_period),
                    fiscal_year_end=optional_date(fiscal_year_end),
                )
            )
        except (TypeError, ValueError) as exc:
            raise JQuantsProviderError(
                f"corrupt FY summary row in jquants_fin_summaries for {ticker}: {exc}"
            ) from exc
    return summaries


def read_jpx_earnings_calendar_snapshot(
    sqlite_path: Path,
    asof_date: date,
    *,
    allow_stale: bool = False,
) -> JPXEarningsCalendarSnapshot | None:
    """Read the fresh JPX schedule snapshot from compatibility storage."""
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        coverage = conn.execute(
            "SELECT coverage_key, coverage_start, coverage_end, fetched_at_utc, "
            "record_count, status FROM source_coverage "
            "WHERE source = 'jpx_earnings_calendar' LIMIT 1"
        ).fetchone()
        if coverage is None:
            return None
        coverage_key, coverage_start, coverage_end, fetched_at_text, record_count, status = coverage
        if (
            coverage_key != "get_earnings_calendar_snapshot:current"
            or status != "ok"
            or not coverage_start
            or not coverage_end
        ):
            return None
        try:
            recorded_start = date.fromisoformat(str(coverage_start))
            recorded_end = date.fromisoformat(str(coverage_end))
            fetched_at = datetime.fromisoformat(str(fetched_at_text).replace("Z", "+00:00"))
        except ValueError:
            return None
        if not allow_stale and weekday_distance(asof_date, fetched_at.astimezone(JST).date()) > 7:
            return None
        rows = conn.execute(
            "SELECT ticker, announcement_date FROM jquants_earnings_calendar "
            "ORDER BY announcement_date, ticker"
        ).fetchall()
        if not rows or len(rows) != int(record_count or 0):
            return None
        try:
            actual_start = date.fromisoformat(str(rows[0][1]))
            actual_end = date.fromisoformat(str(rows[-1][1]))
        except ValueError:
            return None
        if actual_start != recorded_start or actual_end != recorded_end or actual_end < asof_date:
            return None
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    try:
        entries = tuple(
            JPXEarningsCalendarEntry(
                ticker=str(ticker), announcement_date=date.fromisoformat(str(announcement_date))
            )
            for ticker, announcement_date in rows
        )
    except (TypeError, ValueError):
        return None
    return JPXEarningsCalendarSnapshot(
        entries=entries,
        source_urls=(),
        raw_record_count=len(entries),
        excluded_record_count=0,
        superseded_record_count=None,
    )


def read_jpx_regulations(sqlite_path: Path, asof_date: date) -> JPXRegulationSnapshot | None:
    """Return the JPX regulation snapshot for `asof_date`, or `None` if the
    cache has not imported a snapshot for that date.
    """
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        if not _date_imported(conn, "jpx_regulation_flags", asof_date):
            return None
        rows = conn.execute(
            "SELECT source_name, ticker, flag FROM jpx_regulation_flags "
            "WHERE asof_date = ? ORDER BY ticker, flag",
            (asof_date.isoformat(),),
        ).fetchall()
        source_rows = conn.execute(
            "SELECT source_name FROM jpx_regulation_sources "
            "WHERE asof_date = ? ORDER BY source_name",
            (asof_date.isoformat(),),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()

    flags: dict[str, list[str]] = {}
    source_names: set[str] = {str(source_name) for (source_name,) in source_rows}
    for source_name, ticker, flag in rows:
        flags.setdefault(ticker, []).append(flag)
        source_names.add(source_name)
    return JPXRegulationSnapshot(
        flags_by_ticker={ticker: tuple(values) for ticker, values in flags.items()},
        source_names=tuple(sorted(source_names)),
    )


def has_jpx_regulation_data(sqlite_path: Path, asof_date: date) -> bool:
    """Return True when `asof_date` has an imported JPX regulation snapshot.

    A valid snapshot may have zero flagged tickers for a source such as
    取引停止. Treat `source_coverage` as the canonical cache coverage marker so
    stale backfill gating does not force a refetch just because a required
    source returned an empty source-specific table on that date.
    """
    if not sqlite_path.exists():
        return False
    conn = connect_current(sqlite_path)
    if conn is None:
        return False
    try:
        try:
            cur = conn.execute(
                "SELECT 1 FROM source_coverage WHERE source = ? "
                "AND coverage_start <= ? AND coverage_end >= ? AND status = 'ok' LIMIT 1",
                ("jpx_regulation_flags", asof_date.isoformat(), asof_date.isoformat()),
            )
        except sqlite3.OperationalError:
            return False
        return cur.fetchone() is not None
    finally:
        conn.close()


def _has_any_import(conn: sqlite3.Connection, source: str) -> bool:
    try:
        cur = conn.execute(
            "SELECT 1 FROM source_coverage WHERE source = ? "
            "AND status = 'ok' AND record_count > 0 LIMIT 1",
            (source,),
        )
    except sqlite3.OperationalError:
        return False
    return cur.fetchone() is not None


def _date_imported(conn: sqlite3.Connection, source: str, on_date: date) -> bool:
    """True when `source_coverage` records `source` for `on_date`."""
    iso = on_date.isoformat()
    try:
        cur = conn.execute(
            "SELECT 1 FROM source_coverage WHERE source = ? "
            "AND coverage_start <= ? AND coverage_end >= ? AND status = 'ok' LIMIT 1",
            (source, iso, iso),
        )
    except sqlite3.OperationalError:
        return False
    return cur.fetchone() is not None
