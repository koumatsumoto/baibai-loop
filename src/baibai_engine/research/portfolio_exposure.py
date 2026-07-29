"""Prospective portfolio exposure for one planned purchase.

Concentration is a ratio, so its denominator decides whether a warning fires. Every
holding is revalued at the same session's raw close rather than at whatever price the
ledger happened to record per ticker, which keeps the whole portfolio on one price
basis and matches the planning-only limit the proposal is built from. This module is
the single implementation: the planning path and the execution decision must not
disagree about whether a purchase breaches a concentration cap.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from baibai_engine.position.ledger import PortfolioSnapshot
from baibai_engine.position.policy import PORTFOLIO_POLICY

from .close_source import resolve_holding_close_on_basis


def _decimal_to_number(value: Decimal) -> float | int:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def portfolio_annotations(snapshot: PortfolioSnapshot, *, ticker: str) -> list[str]:
    annotations: list[str] = []
    if any(holding.ticker == ticker for holding in snapshot.holdings):
        annotations.append("already_held")
    if any(reservation.ticker == ticker for reservation in snapshot.active_reservations):
        annotations.append("active_reservation")
    for warning in snapshot.warnings:
        annotations.append(f"ledger_warning:{warning.code}")
    return annotations


def portfolio_warnings(
    snapshot: PortfolioSnapshot, *, notional_yen: Decimal, total_capital_yen: int
) -> list[str]:
    # Cash / dry powder shortfalls are human-decision warnings only; they never
    # downgrade the investment ranking or auto-switch to a cheaper next candidate.
    warnings: list[str] = []
    if notional_yen > snapshot.available_cash_yen:
        warnings.append("available_cash_below_notional")
    dry_powder_pct = Decimal(str(PORTFOLIO_POLICY["cash_management"]["dry_powder_warning_pct"]))
    dry_powder_floor = Decimal(total_capital_yen) * dry_powder_pct / 100
    if Decimal(snapshot.available_cash_yen) - notional_yen < dry_powder_floor:
        warnings.append("dry_powder_below_floor")
    return warnings


def portfolio_exposure(
    snapshot: PortfolioSnapshot,
    *,
    sqlite_path: Path,
    price_as_of: date,
    ticker: str,
    sector: str,
    common_factors: Sequence[str],
    order_notional_yen: int,
    market_connection: sqlite3.Connection | None = None,
) -> tuple[dict[str, object], list[str], int]:
    """Derive prospective concentration with disclosed common-factor coverage.

    Candidate holdings/reservations use the thesis's current factor classification.
    Other tickers retain ledger classifications; empty classifications are reported,
    so common-factor exposure remains an explicit lower bound rather than a silent
    claim of complete portfolio coverage.
    """
    holding_values: dict[str, int] = {}
    fallback_tickers: list[str] = []
    for holding in snapshot.holdings:
        resolved = resolve_holding_close_on_basis(
            sqlite_path=sqlite_path,
            ticker=holding.ticker,
            ledger_price_observed_on=holding.market_price_observed_at.date(),
            basis_as_of=price_as_of,
            connection=market_connection,
        )
        market_value = (
            Decimal(str(resolved.close_yen)) * holding.quantity if resolved is not None else None
        )
        if market_value is None or market_value != market_value.to_integral_value():
            holding_values[holding.ticker] = holding.market_value_yen
            fallback_tickers.append(holding.ticker)
        else:
            holding_values[holding.ticker] = int(market_value)

    total_capital_yen = (
        snapshot.available_cash_yen + snapshot.reserved_cash_yen + sum(holding_values.values())
    )
    risk_policy = PORTFOLIO_POLICY["risk_budget"]
    ticker_warning_pct = Decimal(str(risk_policy["max_ticker_concentration_pct"]))
    sector_warning_pct = Decimal(str(risk_policy["max_sector_concentration_pct"]))
    factor_warning_pct = Decimal(str(risk_policy["max_common_factor_concentration_pct"]))

    def current_exposure(*, scope: str, key: str) -> int:
        holding_yen = sum(
            holding_values[holding.ticker]
            for holding in snapshot.holdings
            if (
                (scope == "ticker" and holding.ticker == key)
                or (scope == "sector" and holding.sector == key)
                or (
                    scope == "common_factor"
                    and (
                        key in holding.common_factors
                        or (holding.ticker == ticker and key in common_factors)
                    )
                )
            )
        )
        reservation_yen = sum(
            reservation.reserved_yen
            for reservation in snapshot.active_reservations
            if (
                (scope == "ticker" and reservation.ticker == key)
                or (scope == "sector" and reservation.sector == key)
                or (
                    scope == "common_factor"
                    and (
                        key in reservation.common_factors
                        or (reservation.ticker == ticker and key in common_factors)
                    )
                )
            )
        )
        return holding_yen + reservation_yen

    def exposure_row(*, scope: str, key: str, warning_pct: Decimal) -> dict[str, object]:
        current_yen = current_exposure(scope=scope, key=key)
        prospective_yen = current_yen + order_notional_yen
        prospective_pct = (Decimal(prospective_yen) * 100 / Decimal(total_capital_yen)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        return {
            "key": key,
            "current_and_reserved_yen": current_yen,
            "prospective_yen": prospective_yen,
            "prospective_pct": float(prospective_pct),
            "warning_pct": _decimal_to_number(warning_pct),
        }

    ticker_current_yen = current_exposure(scope="ticker", key=ticker)
    sector_current_yen = current_exposure(scope="sector", key=sector)
    factor_current_yen = {
        factor: current_exposure(scope="common_factor", key=factor) for factor in common_factors
    }
    ticker_row = exposure_row(scope="ticker", key=ticker, warning_pct=ticker_warning_pct)
    sector_row = exposure_row(scope="sector", key=sector, warning_pct=sector_warning_pct)
    factor_rows = [
        exposure_row(scope="common_factor", key=factor, warning_pct=factor_warning_pct)
        for factor in common_factors
    ]
    common_factor_empty_tickers = sorted(
        {
            holding.ticker
            for holding in snapshot.holdings
            if holding.ticker != ticker and not holding.common_factors
        }
        | {
            reservation.ticker
            for reservation in snapshot.active_reservations
            if reservation.ticker != ticker and not reservation.common_factors
        }
    )
    warnings = [
        f"portfolio_exposure_ledger_fallback:{fallback_ticker}"
        for fallback_ticker in sorted(fallback_tickers)
    ]
    if common_factor_empty_tickers:
        warnings.append("portfolio_exposure_common_factor_coverage_incomplete")
    if (
        Decimal(ticker_current_yen + order_notional_yen) * 100 / Decimal(total_capital_yen)
        > ticker_warning_pct
    ):
        warnings.append("prospective_ticker_concentration_exceeds_warning")
    if (
        Decimal(sector_current_yen + order_notional_yen) * 100 / Decimal(total_capital_yen)
        > sector_warning_pct
    ):
        warnings.append("prospective_sector_concentration_exceeds_warning")
    for factor in common_factors:
        if Decimal(factor_current_yen[factor] + order_notional_yen) * 100 / Decimal(
            total_capital_yen
        ) > (factor_warning_pct):
            warnings.append(f"prospective_common_factor_concentration_exceeds_warning:{factor}")
    return (
        {
            "price_as_of": price_as_of.isoformat(),
            "price_basis": "last_close_unadjusted",
            "total_capital_yen": total_capital_yen,
            "holding_valuation_status": (
                "mixed_with_ledger_fallback" if fallback_tickers else "same_asof_raw_close"
            ),
            "ledger_fallback_tickers": sorted(fallback_tickers),
            "common_factor_empty_tickers": common_factor_empty_tickers,
            "ticker": ticker_row,
            "sector": sector_row,
            "common_factors": factor_rows,
        },
        warnings,
        total_capital_yen,
    )


__all__ = [
    "portfolio_annotations",
    "portfolio_exposure",
    "portfolio_warnings",
]
