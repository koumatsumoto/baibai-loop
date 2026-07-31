"""Read helpers that pull screening fundamentals/regulation inputs from SQLite.

The functions are deliberately permissive: a missing SQLite file or a source
that has not been fetched yet returns `None` so bootstrap/fetch commands can
populate the missing coverage. `screening run` performs a separate preflight
coverage check and must not fall back to provider APIs. Price/calendar reads
live in `baibai_engine.market.store`; this module owns master / fin summaries /
earnings calendar / EDINET / JPX.
"""

from __future__ import annotations

import json
import sqlite3
from bisect import bisect_right
from collections.abc import Mapping, Sequence
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
from .edinet_revision import has_hard_metric_failure
from .providers.edinet import EdinetMetricRecord, normalize_metric_record
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


class EDINETMetricBaselineError(RuntimeError):
    """Raised when the newest successful baseline claims an inconsistent snapshot."""


@dataclass(frozen=True, slots=True)
class EDINETMetricBaselineRow:
    record: EdinetMetricRecord
    extractor_revision: str | None
    source_document_revision: str | None


@dataclass(frozen=True, slots=True)
class EDINETMetricBaseline:
    asof_date: date
    rows: Mapping[str, EDINETMetricBaselineRow]


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
        week_ends = [
            date.fromisoformat(str(row[0]))
            for row in conn.execute(
                "SELECT DISTINCT week_end FROM jquants_weekly_margin "
                "WHERE week_end <= ? ORDER BY week_end",
                (asof.isoformat(),),
            )
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


# Balance dates are weekly, so 26 of them is the half year the delta axis measures.
MARGIN_DELTA_WEEKS = 26


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
    return sorted(last_of_week.values())


def read_margin_supply_demand_inputs(
    sqlite_path: Path, asof: date
) -> tuple[dict[str, JQuantsWeeklyMargin], dict[str, JQuantsWeeklyMargin]]:
    """The published balance dates a cohort at `asof` may use: latest, and 26 back.

    Both are keyed by ticker. Empty mappings mean the store holds no published
    balance date for this as-of, which is what a store without the weekly source
    looks like and yields unset axes rather than wrong ones.
    """
    week_ends = published_margin_week_ends(sqlite_path, asof)
    if not week_ends:
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
            "dps_actual_annual, dps_forecast_annual "
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
                )
            )
        except (TypeError, ValueError) as exc:
            raise JQuantsProviderError(
                f"corrupt SQLite row in jquants_fin_summaries for {ticker}: {exc}"
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
        rejected_record_count=0,
    )


def read_edinet_documents(sqlite_path: Path, on_date: date) -> list[dict[str, Any]] | None:
    """Return raw EDINET document records for `on_date` from SQLite, or
    `None` if the cache cannot serve the date.
    """
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        if not _date_imported(conn, "edinet_documents", on_date):
            return None
        rows = conn.execute(
            "SELECT sequence_number, doc_id, sec_code, doc_type_code, csv_flag, xbrl_flag, "
            "legal_status, disclosure_status, withdrawal_status, doc_info_edit_status, "
            "parent_doc_id, operation_datetime, submit_datetime, doc_description, "
            "period_start, period_end "
            "FROM edinet_documents WHERE doc_date = ? ORDER BY sequence_number",
            (on_date.isoformat(),),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return [
        {
            "doc_date": on_date.isoformat(),
            "seqNumber": sequence_number,
            "docID": doc_id,
            "secCode": sec_code,
            "docTypeCode": doc_type_code,
            "csvFlag": csv_flag,
            "xbrlFlag": xbrl_flag,
            "legalStatus": legal_status,
            "disclosureStatus": disclosure_status,
            "withdrawalStatus": withdrawal_status,
            "docInfoEditStatus": doc_info_edit_status,
            "parentDocID": parent_doc_id,
            "opeDateTime": operation_datetime,
            "submitDateTime": submit_datetime,
            "docDescription": doc_description,
            "periodStart": period_start,
            "periodEnd": period_end,
        }
        for (
            sequence_number,
            doc_id,
            sec_code,
            doc_type_code,
            csv_flag,
            xbrl_flag,
            legal_status,
            disclosure_status,
            withdrawal_status,
            doc_info_edit_status,
            parent_doc_id,
            operation_datetime,
            submit_datetime,
            doc_description,
            period_start,
            period_end,
        ) in rows
    ]


def read_unfinalized_edinet_document_dates(sqlite_path: Path, *, before: date) -> tuple[date, ...]:
    """Return fetched EDINET file dates that still require a final refresh."""
    if not sqlite_path.exists():
        return ()
    conn = connect_current(sqlite_path)
    if conn is None:
        return ()
    try:
        rows = conn.execute(
            "SELECT doc_date FROM edinet_document_lists "
            "WHERE is_final = 0 AND doc_date < ? ORDER BY doc_date",
            (before.isoformat(),),
        ).fetchall()
    except sqlite3.OperationalError:
        return ()
    finally:
        conn.close()
    return tuple(date.fromisoformat(str(row[0])) for row in rows)


def read_edinet_metrics(
    sqlite_path: Path, asof_date: date
) -> Mapping[str, EdinetMetricRecord] | None:
    """Return EDINET metric records keyed by ticker for `asof_date`, or
    `None` if the cache cannot serve the date.
    """
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        if not _date_imported(conn, "edinet_metrics", asof_date):
            return None
        rows = conn.execute(
            "SELECT ticker, sales_ttm, ocf_ttm, debt, cash, ebitda_ttm, "
            "consolidation_basis, ttm_quality_ev_ebitda, ttm_quality_p_s, ttm_quality_pcfr, "
            "operating_profit_ttm, depreciation_and_amortization_ttm, capex_ttm, fcf_ttm, "
            "net_cash, equity, total_assets, ttm_quality_fcf, ttm_quality_net_cash, "
            "source_doc_id, document_type, source_submit_datetime, source_period_start, "
            "source_period_end, capex_source, failure_reasons "
            "FROM edinet_metrics WHERE asof_date = ?",
            (asof_date.isoformat(),),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()

    # Reuse the provider's normalize step so SQLite-backed records pick up
    # the same TTMQuality coercion / shape as the JSON path. Pass an
    # already-normalized payload that the function expects (typed columns
    # already match its key set).
    records: dict[str, EdinetMetricRecord] = {}
    for row in rows:
        record = _normalize_edinet_metric_sql_row(row)
        records[record.ticker] = record
    return records


def read_edinet_metric_baseline(
    sqlite_path: Path,
    target_asof: date,
) -> EDINETMetricBaseline | None:
    """Read the newest eligible snapshot at or before ``target_asof``."""
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        coverage_rows = conn.execute(
            "SELECT coverage_key, coverage_start, coverage_end, record_count, status, error "
            "FROM source_coverage WHERE source = 'edinet_metrics' AND coverage_key <= ? "
            "ORDER BY coverage_key DESC",
            (target_asof.isoformat(),),
        ).fetchall()
        for (
            coverage_key,
            coverage_start,
            coverage_end,
            record_count,
            status,
            error,
        ) in coverage_rows:
            key = str(coverage_key)
            if status == "failed":
                continue
            if status != "ok":
                raise EDINETMetricBaselineError(
                    f"EDINET metric baseline {key} has unsupported status {status!r}"
                )
            try:
                baseline_asof = date.fromisoformat(key)
            except ValueError as exc:
                raise EDINETMetricBaselineError(
                    f"EDINET metric baseline has invalid coverage_key {key!r}"
                ) from exc
            if (
                baseline_asof > target_asof
                or coverage_start != key
                or coverage_end != key
                or error is not None
                or not isinstance(record_count, int)
                or record_count <= 0
            ):
                raise EDINETMetricBaselineError(
                    f"EDINET metric baseline coverage is inconsistent for {key}"
                )
            rows = conn.execute(
                "SELECT ticker, sales_ttm, ocf_ttm, debt, cash, ebitda_ttm, "
                "consolidation_basis, ttm_quality_ev_ebitda, ttm_quality_p_s, "
                "ttm_quality_pcfr, operating_profit_ttm, "
                "depreciation_and_amortization_ttm, capex_ttm, fcf_ttm, net_cash, "
                "equity, total_assets, ttm_quality_fcf, ttm_quality_net_cash, "
                "source_doc_id, document_type, source_submit_datetime, "
                "source_period_start, source_period_end, capex_source, failure_reasons, "
                "extractor_revision, source_document_revision "
                "FROM edinet_metrics WHERE asof_date = ? ORDER BY ticker",
                (key,),
            ).fetchall()
            if len(rows) != record_count:
                raise EDINETMetricBaselineError(
                    f"EDINET metric baseline row count mismatch for {key}: "
                    f"coverage={record_count} rows={len(rows)}"
                )
            baseline_rows: dict[str, EDINETMetricBaselineRow] = {}
            try:
                for row in rows:
                    record = _normalize_edinet_metric_sql_row(row)
                    if has_hard_metric_failure(record.failure_reasons):
                        raise EDINETMetricBaselineError(
                            f"EDINET metric baseline contains a hard parser failure for "
                            f"{key}/{record.ticker}"
                        )
                    baseline_rows[record.ticker] = EDINETMetricBaselineRow(
                        record=record,
                        extractor_revision=str(row[26]) if row[26] is not None else None,
                        source_document_revision=(str(row[27]) if row[27] is not None else None),
                    )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise EDINETMetricBaselineError(
                    f"EDINET metric baseline row is invalid for {key}: {type(exc).__name__}"
                ) from exc
            if len(baseline_rows) != record_count:
                raise EDINETMetricBaselineError(
                    f"EDINET metric baseline ticker identity mismatch for {key}"
                )
            return EDINETMetricBaseline(asof_date=baseline_asof, rows=baseline_rows)
    except sqlite3.OperationalError as exc:
        raise EDINETMetricBaselineError(f"EDINET metric baseline read failed: {exc}") from exc
    finally:
        conn.close()
    return None


def _normalize_edinet_metric_sql_row(row: Sequence[Any]) -> EdinetMetricRecord:
    payload = {
        "ticker": row[0],
        "sales_ttm": row[1],
        "ocf_ttm": row[2],
        "debt": row[3],
        "cash": row[4],
        "ebitda_ttm": row[5],
        "consolidation_basis": row[6],
        "ttm_quality_ev_ebitda": row[7],
        "ttm_quality_p_s": row[8],
        "ttm_quality_pcfr": row[9],
        "operating_profit_ttm": row[10],
        "depreciation_and_amortization_ttm": row[11],
        "capex_ttm": row[12],
        "fcf_ttm": row[13],
        "net_cash": row[14],
        "equity": row[15],
        "total_assets": row[16],
        "ttm_quality_fcf": row[17],
        "ttm_quality_net_cash": row[18],
        "source_doc_id": row[19],
        "document_type": row[20],
        "source_submit_datetime": row[21],
        "source_period_start": row[22],
        "source_period_end": row[23],
        "capex_source": row[24],
        "failure_reasons": json.loads(row[25]) if row[25] else [],
    }
    return normalize_metric_record(payload)


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
