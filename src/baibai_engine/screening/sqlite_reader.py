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
from typing import Any

from baibai_engine.foundation.date_utils import weekday_distance
from baibai_engine.foundation.time import JST
from baibai_engine.market.sqlite import (
    connect_current,
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
from .providers.jpx import (
    JPXEarningsCalendarEntry,
    JPXEarningsCalendarSnapshot,
    JPXRegulationSnapshot,
)
from .providers.jquants import (
    JQuantsFinancialSummary,
    JQuantsProviderError,
    JQuantsWeeklyMargin,
)
from .schema import SecurityMaster
from .sqlite_cache.jquants import WEEKLY_MARGIN_SOURCE, weekly_margin_coverage_key


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


def read_eq_master(sqlite_path: Path) -> list[SecurityMaster] | None:
    """Return only the globally latest operational master snapshot.

    A ticker absent from the latest snapshot is not backfilled from an older
    date. Historical membership belongs to :func:`read_eq_master_asof`.
    """
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        if not _has_any_import(conn, "jquants_master_snapshots"):
            return None
        snapshot = conn.execute(
            "SELECT MAX(snapshot_date) FROM jquants_master_snapshots "
            "WHERE snapshot_date != 'unknown'"
        ).fetchone()[0]
        if snapshot is None:
            return None
        rows = conn.execute(
            "SELECT ticker, name, market, sector_33, is_common_stock "
            "FROM jquants_master_snapshots WHERE snapshot_date = ? ORDER BY ticker",
            (str(snapshot),),
        ).fetchall()
    finally:
        conn.close()

    return _materialize_masters(rows)


def read_weekly_margin(sqlite_path: Path, week_end: date) -> list[JQuantsWeeklyMargin] | None:
    """Return one balance date's rows, or None when it has not been examined.

    A week the exchange skipped is stored as a coverage row with no rows behind
    it, so an empty list and None mean different things: the first says the week
    has no balance date, the second says nobody has looked. Only the second is a
    reason to call the provider.
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


def _opt_float_value(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


# The exchange publishes a week's margin balances on the second trading day after
# the balance date, around 16:30 JST. Counting trading days rather than adding a
# fixed offset is what makes the rule survive the balance dates that land on a
# Thursday or Wednesday because the week's later days were closed.
MARGIN_PUBLICATION_TRADING_DAYS = 2


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
    # the list exactly when it has already happened. The publication itself lands in
    # the late afternoon while a decision prices at the close, so a balance date
    # published on `asof` is not yet usable at `asof`'s price; the strict comparison
    # costs a week of freshness on a weekly series and removes that overlap.
    published: list[date] = []
    for week_end in week_ends:
        publication = bisect_right(trading_days, week_end) + MARGIN_PUBLICATION_TRADING_DAYS - 1
        if publication < len(trading_days) and trading_days[publication] < asof:
            published.append(week_end)
    return published


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


def weekly_margin_candidate_dates(sqlite_path: Path, start: date, end: date) -> list[date]:
    """The last stored trading day of each week in `[start, end]`, ascending.

    The exchange's balance date is that day in most weeks and an earlier one when
    the week's later days were closed — and some weeks have no balance date at all,
    even weeks the market traded. Rather than encode that calendar, this proposes
    one candidate per week and lets the fetch record an empty answer as the week's
    fact, so a week without a balance date is asked for once.
    """
    if not sqlite_path.exists() or start > end:
        return []
    conn = connect_current(sqlite_path)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT DISTINCT traded_at FROM jquants_daily_bars "
            "WHERE traded_at BETWEEN ? AND ? ORDER BY traded_at",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        conn.close()
    last_of_week: dict[tuple[int, int], date] = {}
    for (value,) in rows:
        try:
            day = date.fromisoformat(str(value))
        except ValueError:
            continue
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
        and end.isocalendar()[:2] == stored_latest.isocalendar()[:2]
    ):
        last_of_week.pop(newest.isocalendar()[:2], None)
    return sorted(last_of_week.values())


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


def read_margin_supply_demand_inputs(
    sqlite_path: Path, asof: date
) -> tuple[dict[str, JQuantsWeeklyMargin], dict[str, JQuantsWeeklyMargin]]:
    """The published balance dates a cohort at `asof` may use: latest, and 26 back.

    Both are keyed by ticker. Empty mappings mean the store holds no published
    balance date for this as-of, which is what a store without the weekly source
    looks like and yields unset axes rather than wrong ones.
    """
    week_ends = published_margin_week_ends(sqlite_path, asof)
    if not week_ends or (asof - week_ends[-1]).days > MARGIN_MAX_STALE_DAYS:
        return {}, {}
    latest = {row.ticker: row for row in read_weekly_margin(sqlite_path, week_ends[-1]) or ()}
    prior: dict[str, JQuantsWeeklyMargin] = {}
    if len(week_ends) > MARGIN_DELTA_WEEKS:
        prior = {
            row.ticker: row
            for row in read_weekly_margin(sqlite_path, week_ends[-1 - MARGIN_DELTA_WEEKS]) or ()
        }
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
            "treasury_shares, equity_to_asset_ratio "
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
