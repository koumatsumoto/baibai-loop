"""Field-population coverage for financial facts consumed by screening.

Date coverage proves that a provider window was fetched.  It does not prove that a
column added by a schema migration was populated in existing rows.  This module checks
the exact as-of common-stock population and plans only the disclosure dates that can
repair a blocking field population.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from .shared import CacheCoverageIssue

REQUIRED_FIELD_MIN_POPULATION_RATIO = 0.75

_REQUIRED_FIELD_VALUES_CTE = (
    "WITH p AS (SELECT ticker FROM jquants_master_snapshots "
    "WHERE snapshot_date = ?1 AND is_common_stock = 1), x AS ("
    "SELECT p.ticker, "
    "EXISTS (SELECT 1 FROM jquants_fin_summaries s WHERE s.ticker = p.ticker "
    "AND s.disclosed_at BETWEEN ?2 AND ?3) AS has_summary, "
    "(SELECT s.shares_outstanding FROM jquants_fin_summaries s "
    "WHERE s.ticker = p.ticker AND s.disclosed_at BETWEEN ?2 AND ?3 "
    "AND s.shares_outstanding IS NOT NULL ORDER BY s.disclosed_at DESC LIMIT 1) AS shares, "
    "(SELECT s.disclosed_at FROM jquants_fin_summaries s "
    "WHERE s.ticker = p.ticker AND s.disclosed_at BETWEEN ?2 AND ?3 "
    "AND s.shares_outstanding IS NOT NULL ORDER BY s.disclosed_at DESC LIMIT 1) AS shares_date, "
    "(SELECT s.treasury_shares FROM jquants_fin_summaries s "
    "WHERE s.ticker = p.ticker AND s.disclosed_at BETWEEN ?2 AND ?3 "
    "AND s.treasury_shares IS NOT NULL ORDER BY s.disclosed_at DESC LIMIT 1) AS treasury, "
    "(SELECT s.disclosed_at FROM jquants_fin_summaries s "
    "WHERE s.ticker = p.ticker AND s.disclosed_at BETWEEN ?2 AND ?3 "
    "AND s.treasury_shares IS NOT NULL ORDER BY s.disclosed_at DESC LIMIT 1) AS treasury_date, "
    "(SELECT s.equity_to_asset_ratio FROM jquants_fin_summaries s "
    "WHERE s.ticker = p.ticker AND s.disclosed_at BETWEEN ?2 AND ?3 "
    "AND s.equity_to_asset_ratio IS NOT NULL ORDER BY s.disclosed_at DESC LIMIT 1) "
    "AS equity_ratio FROM p), y AS (SELECT x.*, "
    "shares / COALESCE((SELECT EXP(SUM(LN(b.adjustment_factor))) "
    "FROM jquants_daily_bars b WHERE b.ticker = x.ticker "
    "AND b.traded_at > x.shares_date AND b.traded_at <= ?1 "
    "AND b.adjustment_factor NOT IN (0.0, 1.0)), 1.0) AS shares_asof, "
    "treasury / COALESCE((SELECT EXP(SUM(LN(b.adjustment_factor))) "
    "FROM jquants_daily_bars b WHERE b.ticker = x.ticker "
    "AND b.traded_at > x.treasury_date AND b.traded_at <= ?1 "
    "AND b.adjustment_factor NOT IN (0.0, 1.0)), 1.0) AS treasury_asof FROM x) "
)

_MARKET_CAP_FIELDS_USABLE = (
    "shares_asof IS NOT NULL AND treasury_asof IS NOT NULL AND shares_asof > treasury_asof"
)


@dataclass(frozen=True, slots=True, kw_only=True)
class RequiredFieldCoverage:
    asof: date
    start: date
    population_tickers: int
    minimum_tickers: int
    summary_tickers: int
    shares_outstanding_tickers: int
    treasury_shares_tickers: int
    equity_to_asset_ratio_tickers: int
    market_cap_required_fields_tickers: int
    valuation_required_fields_tickers: int

    @property
    def blocking_fields(self) -> tuple[str, ...]:
        counts = (
            ("summary_rows", self.summary_tickers),
            ("shares_outstanding", self.shares_outstanding_tickers),
            ("treasury_shares", self.treasury_shares_tickers),
            ("equity_to_asset_ratio", self.equity_to_asset_ratio_tickers),
            ("market_cap_required_fields", self.market_cap_required_fields_tickers),
            ("valuation_required_fields", self.valuation_required_fields_tickers),
        )
        return tuple(name for name, count in counts if count < self.minimum_tickers)

    def summary_line(self) -> str:
        return (
            f"asof={self.asof.isoformat()} population={self.population_tickers} "
            f"minimum={self.minimum_tickers} summary_rows={self.summary_tickers} "
            f"shares_outstanding={self.shares_outstanding_tickers} "
            f"treasury_shares={self.treasury_shares_tickers} "
            f"equity_to_asset_ratio={self.equity_to_asset_ratio_tickers} "
            f"market_cap_required_fields={self.market_cap_required_fields_tickers} "
            f"valuation_required_fields={self.valuation_required_fields_tickers}"
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class RequiredFieldRepairPlan:
    coverage: RequiredFieldCoverage
    ranges: tuple[tuple[date, date], ...]


def append_required_field_coverage_issues(
    conn: sqlite3.Connection,
    issues: list[CacheCoverageIssue],
    *,
    start: date,
    asof: date,
) -> RequiredFieldCoverage:
    coverage = required_field_coverage(conn, start=start, asof=asof)
    counts = {
        "summary_rows": coverage.summary_tickers,
        "shares_outstanding": coverage.shares_outstanding_tickers,
        "treasury_shares": coverage.treasury_shares_tickers,
        "equity_to_asset_ratio": coverage.equity_to_asset_ratio_tickers,
        "market_cap_required_fields": coverage.market_cap_required_fields_tickers,
        "valuation_required_fields": coverage.valuation_required_fields_tickers,
    }
    for field in coverage.blocking_fields:
        issues.append(
            CacheCoverageIssue(
                source="jquants_fin_summaries",
                requirement=f"required-field:{field}@{asof.isoformat()}",
                reason=(
                    f"required-field ticker population is {counts[field]}/"
                    f"{coverage.population_tickers}; minimum {coverage.minimum_tickers}; "
                    "run field-aware bootstrap repair"
                ),
            )
        )
    return coverage


def read_required_field_coverage(
    sqlite_path: Path, *, start: date, asof: date
) -> RequiredFieldCoverage | None:
    if not sqlite_path.exists():
        return None
    connection = sqlite3.connect(f"file:{sqlite_path.as_posix()}?mode=ro", uri=True)
    try:
        return required_field_coverage(connection, start=start, asof=asof)
    except sqlite3.OperationalError:
        return None
    finally:
        connection.close()


def plan_required_field_repair(
    sqlite_path: Path, *, start: date, asof: date
) -> RequiredFieldRepairPlan | None:
    if not sqlite_path.exists():
        return None
    connection = sqlite3.connect(f"file:{sqlite_path.as_posix()}?mode=ro", uri=True)
    try:
        coverage = required_field_coverage(connection, start=start, asof=asof)
        if not coverage.blocking_fields:
            return RequiredFieldRepairPlan(coverage=coverage, ranges=())
        if "summary_rows" in coverage.blocking_fields:
            return RequiredFieldRepairPlan(coverage=coverage, ranges=((start, asof),))
        missing_predicates: list[str] = []
        blocking = set(coverage.blocking_fields)
        if "shares_outstanding" in blocking:
            missing_predicates.append("shares IS NULL")
        if "treasury_shares" in blocking:
            missing_predicates.append("treasury IS NULL")
        if "equity_to_asset_ratio" in blocking:
            missing_predicates.append("equity_ratio IS NULL")
        if "market_cap_required_fields" in blocking:
            missing_predicates.append(f"NOT ({_MARKET_CAP_FIELDS_USABLE})")
        if "valuation_required_fields" in blocking:
            missing_predicates.append(f"NOT ({_MARKET_CAP_FIELDS_USABLE}) OR equity_ratio IS NULL")
        predicate = " OR ".join(missing_predicates)
        rows = connection.execute(
            _REQUIRED_FIELD_VALUES_CTE + " , missing AS ("
            f"SELECT ticker FROM y WHERE {predicate}) "  # nosec B608: fixed predicates above
            "SELECT DISTINCT s.disclosed_at FROM jquants_fin_summaries s "
            "JOIN missing m ON m.ticker = s.ticker "
            "WHERE s.disclosed_at BETWEEN ?4 AND ?5 ORDER BY s.disclosed_at",
            (
                asof.isoformat(),
                start.isoformat(),
                asof.isoformat(),
                start.isoformat(),
                asof.isoformat(),
            ),
        ).fetchall()
        dates = tuple(date.fromisoformat(str(row[0])) for row in rows)
        ranges = _consecutive_date_ranges(dates) or ((start, asof),)
        return RequiredFieldRepairPlan(coverage=coverage, ranges=ranges)
    except sqlite3.OperationalError:
        return None
    finally:
        connection.close()


def required_field_coverage(
    conn: sqlite3.Connection, *, start: date, asof: date
) -> RequiredFieldCoverage:
    params = (asof.isoformat(), start.isoformat(), asof.isoformat())
    row = conn.execute(
        _REQUIRED_FIELD_VALUES_CTE + "SELECT COUNT(*), COALESCE(SUM(has_summary), 0), "
        "COALESCE(SUM(shares IS NOT NULL), 0), "
        "COALESCE(SUM(treasury IS NOT NULL), 0), "
        "COALESCE(SUM(equity_ratio IS NOT NULL), 0), "
        f"COALESCE(SUM({_MARKET_CAP_FIELDS_USABLE}), 0), "
        f"COALESCE(SUM(({_MARKET_CAP_FIELDS_USABLE}) AND equity_ratio IS NOT NULL), 0) "
        "FROM y",
        params,
    ).fetchone()
    population = int(row[0] or 0)
    minimum = math.ceil(population * REQUIRED_FIELD_MIN_POPULATION_RATIO)
    return RequiredFieldCoverage(
        asof=asof,
        start=start,
        population_tickers=population,
        minimum_tickers=minimum,
        summary_tickers=int(row[1] or 0),
        shares_outstanding_tickers=int(row[2] or 0),
        treasury_shares_tickers=int(row[3] or 0),
        equity_to_asset_ratio_tickers=int(row[4] or 0),
        market_cap_required_fields_tickers=int(row[5] or 0),
        valuation_required_fields_tickers=int(row[6] or 0),
    )


def _consecutive_date_ranges(dates: tuple[date, ...]) -> tuple[tuple[date, date], ...]:
    if not dates:
        return ()
    ranges: list[tuple[date, date]] = []
    range_start = range_end = dates[0]
    for value in dates[1:]:
        if value <= range_end + timedelta(days=1):
            range_end = value
            continue
        ranges.append((range_start, range_end))
        range_start = range_end = value
    ranges.append((range_start, range_end))
    return tuple(ranges)
