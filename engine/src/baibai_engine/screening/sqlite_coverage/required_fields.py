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

from baibai_engine.market.bars import JQuantsAdjustmentFactorEvent
from baibai_engine.market.jquants_models import JQuantsFinancialSummary
from baibai_engine.storage.observations.reader import read_financial_documents
from baibai_engine.valuation.metrics.capital import (
    _normalize_summaries_with_status,
    _resolve_capital_basis,
)
from baibai_engine.valuation.metrics.periods import _latest_non_null_row

from .common_stock import COMMON_STOCK_SNAPSHOT_SQL, scope_parameters
from .shared import CacheCoverageIssue

REQUIRED_FIELD_MIN_POPULATION_RATIO = 0.75


@dataclass(frozen=True, slots=True)
class _RequiredFieldValues:
    has_summary: bool
    shares: bool
    treasury: bool
    equity_ratio: bool
    market_cap: bool
    dates: tuple[date, ...]


def _required_field_values(
    conn: sqlite3.Connection, *, start: date, asof: date
) -> dict[str, _RequiredFieldValues]:
    population = {
        str(row[0]) for row in conn.execute(COMMON_STOCK_SNAPSHOT_SQL, scope_parameters(asof))
    }
    summaries: dict[str, list[JQuantsFinancialSummary]] = {}
    for row in read_financial_documents(conn, start=start, end=asof):
        if row.ticker in population:
            summaries.setdefault(row.ticker, []).append(row)
    events: dict[str, list[JQuantsAdjustmentFactorEvent]] = {}
    for ticker, traded_at, factor, ex_right_type in conn.execute(
        "SELECT ticker, traded_at, adjustment_factor, ex_right_type FROM jquants_daily_bars "
        "WHERE traded_at BETWEEN ? AND ? AND adjustment_factor != 0.0 "
        "AND (adjustment_factor != 1.0 OR ex_right_type IS NOT NULL)",
        (start.isoformat(), asof.isoformat()),
    ):
        if ticker in population:
            events.setdefault(ticker, []).append(
                JQuantsAdjustmentFactorEvent(
                    ticker=ticker,
                    traded_at=date.fromisoformat(traded_at),
                    adjustment_factor=factor,
                    ex_right_type=ex_right_type,
                )
            )
    result = {}
    for ticker in population:
        rows = summaries.get(ticker, [])
        normalized = _normalize_summaries_with_status(rows, events.get(ticker, []), asof)
        capital = _resolve_capital_basis(
            normalized.summaries, capital_basis_barrier=normalized.capital_basis_barrier
        )
        ratio_row = _latest_non_null_row(normalized.summaries, "equity_to_asset_ratio")
        result[ticker] = _RequiredFieldValues(
            has_summary=bool(rows),
            shares=capital.issued is not None,
            treasury=capital.treasury is not None,
            equity_ratio=ratio_row is not None,
            market_cap=capital.shares_ex_treasury is not None,
            dates=tuple(sorted({row.disclosed_at for row in rows})),
        )
    return result


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
    # Missing is a normal pre-bootstrap state; errors in an existing store are not.
    if not sqlite_path.exists():
        return None
    connection = sqlite3.connect(f"{sqlite_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return required_field_coverage(connection, start=start, asof=asof)
    finally:
        connection.close()


def plan_required_field_repair(
    sqlite_path: Path, *, start: date, asof: date
) -> RequiredFieldRepairPlan | None:
    # Missing is a normal pre-bootstrap state; errors in an existing store are not.
    if not sqlite_path.exists():
        return None
    connection = sqlite3.connect(f"{sqlite_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        coverage = required_field_coverage(connection, start=start, asof=asof)
        if not coverage.blocking_fields:
            return RequiredFieldRepairPlan(coverage=coverage, ranges=())
        if "summary_rows" in coverage.blocking_fields:
            return RequiredFieldRepairPlan(coverage=coverage, ranges=((start, asof),))
        blocking = set(coverage.blocking_fields)
        dates = tuple(
            sorted(
                {
                    day
                    for value in _required_field_values(connection, start=start, asof=asof).values()
                    if (
                        ("shares_outstanding" in blocking and not value.shares)
                        or ("treasury_shares" in blocking and not value.treasury)
                        or ("equity_to_asset_ratio" in blocking and not value.equity_ratio)
                        or ("market_cap_required_fields" in blocking and not value.market_cap)
                        or (
                            "valuation_required_fields" in blocking
                            and not (value.market_cap and value.equity_ratio)
                        )
                    )
                    for day in value.dates
                }
            )
        )
        ranges = _consecutive_date_ranges(dates) or ((start, asof),)
        return RequiredFieldRepairPlan(coverage=coverage, ranges=ranges)
    finally:
        connection.close()


def required_field_coverage(
    conn: sqlite3.Connection, *, start: date, asof: date
) -> RequiredFieldCoverage:
    values = tuple(_required_field_values(conn, start=start, asof=asof).values())
    population = len(values)
    return RequiredFieldCoverage(
        asof=asof,
        start=start,
        population_tickers=population,
        minimum_tickers=math.ceil(population * REQUIRED_FIELD_MIN_POPULATION_RATIO),
        summary_tickers=sum(value.has_summary for value in values),
        shares_outstanding_tickers=sum(value.shares for value in values),
        treasury_shares_tickers=sum(value.treasury for value in values),
        equity_to_asset_ratio_tickers=sum(value.equity_ratio for value in values),
        market_cap_required_fields_tickers=sum(value.market_cap for value in values),
        valuation_required_fields_tickers=sum(
            value.market_cap and value.equity_ratio for value in values
        ),
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
