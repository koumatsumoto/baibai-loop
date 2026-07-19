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
from collections.abc import Mapping
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
from .providers.edinet import EdinetMetricRecord, normalize_metric_record
from .providers.jpx import (
    JPXEarningsCalendarEntry,
    JPXEarningsCalendarSnapshot,
    JPXRegulationSnapshot,
)
from .providers.jquants import (
    JQuantsFinancialSummary,
    JQuantsProviderError,
)
from .schema import SecurityMaster


@dataclass(frozen=True, slots=True)
class MasterSnapshotRead:
    """A point-in-time security-master read with its source snapshot status."""

    masters: tuple[SecurityMaster, ...]
    snapshot_date: date | None
    status: str


def read_eq_master_asof(sqlite_path: Path, asof: date) -> MasterSnapshotRead:
    """Read only the newest master snapshot at or before ``asof``.

    This intentionally never falls forward to the current snapshot: calibration
    needs the historical membership that was knowable at the cohort date.
    """
    if not sqlite_path.exists():
        return MasterSnapshotRead((), None, "unavailable")
    conn = connect_current(sqlite_path)
    if conn is None:
        return MasterSnapshotRead((), None, "unavailable")
    try:
        snapshot = conn.execute(
            "SELECT MAX(snapshot_date) FROM jquants_master_snapshots WHERE snapshot_date <= ?",
            (asof.isoformat(),),
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
        "exact_date" if snapshot_date == asof else "prior_snapshot",
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
            "SELECT doc_id, sec_code, doc_type_code, csv_flag, xbrl_flag, legal_status, "
            "disclosure_status, withdrawal_status, submit_datetime, doc_description, "
            "period_start, period_end "
            "FROM edinet_documents WHERE doc_date = ? ORDER BY doc_id",
            (on_date.isoformat(),),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return [
        {
            "docID": doc_id,
            "secCode": sec_code,
            "docTypeCode": doc_type_code,
            "csvFlag": csv_flag,
            "xbrlFlag": xbrl_flag,
            "legalStatus": legal_status,
            "disclosureStatus": disclosure_status,
            "withdrawalStatus": withdrawal_status,
            "submitDateTime": submit_datetime,
            "docDescription": doc_description,
            "periodStart": period_start,
            "periodEnd": period_end,
        }
        for (
            doc_id,
            sec_code,
            doc_type_code,
            csv_flag,
            xbrl_flag,
            legal_status,
            disclosure_status,
            withdrawal_status,
            submit_datetime,
            doc_description,
            period_start,
            period_end,
        ) in rows
    ]


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
        record = normalize_metric_record(payload)
        records[record.ticker] = record
    return records


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
