"""配分工程で現在の quote・確認済み資本から一時的な価格数量案を産む。"""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

from baibai_engine.appdb.paths import database_path
from baibai_engine.appdb.read import connect_read_only
from baibai_engine.foundation.time import JST
from baibai_engine.position.market_source import next_order_session
from baibai_engine.position.policy import PORTFOLIO_POLICY
from baibai_engine.position.store import load_ledger_in_transaction
from baibai_engine.position.valuation import current_portfolio
from baibai_engine.research.capital_allocation import CapitalAllocationAssessment
from baibai_engine.research.capital_inputs import (
    evaluate_allocation_in_transaction,
)
from baibai_engine.research.decimal_number import decimal_to_number
from baibai_engine.research.valuation import finite_decimal


def plan_limit(
    *,
    capital_allocation_assessment_id: str,
    db_path: Path | None,
    sqlite_path: Path,
    target_session: date,
    budget_min_yen: int,
    budget_max_yen: int,
    now: datetime,
) -> dict[str, object]:
    now = now.astimezone(JST)
    order_session = next_order_session(sqlite_path=sqlite_path, now=now)
    with closing(connect_read_only(database_path(db_path))) as connection:
        connection.execute("BEGIN")
        row = connection.execute(
            "SELECT payload FROM capital_allocation_assessment "
            "WHERE capital_allocation_assessment_id = ?",
            (capital_allocation_assessment_id,),
        ).fetchone()
        if row is None:
            raise ValueError("allocation assessment is unavailable")
        assessment = CapitalAllocationAssessment.model_validate(json.loads(str(row[0])))
        if assessment.result != "allocate":
            raise ValueError("assessment is not an allocate decision")
        alternative = next(
            item for item in assessment.alternatives if item.disposition == "allocate"
        )
        pair, entry, quote, cash = evaluate_allocation_in_transaction(
            connection,
            alternative=alternative,
            assessment_id=capital_allocation_assessment_id,
            as_of=now.date(),
            now=now,
            sqlite_path=sqlite_path,
            budget_max_yen=budget_max_yen,
        )
        liquidity_row = connection.execute(
            "SELECT as_of, json_extract(payload, '$.entries') FROM research_triage "
            "WHERE research_triage_id=?",
            (assessment.research_triage_id,),
        ).fetchone()
        liquidity_as_of = None if liquidity_row is None else str(liquidity_row[0])
        liquidity_entries = [] if liquidity_row is None else json.loads(str(liquidity_row[1]))
        liquidity: dict[str, object] = next(
            (
                item.get("candidate_snapshot", {}).get("analysis", {}).get("identity_liquidity", {})
                for item in liquidity_entries
                if item.get("ticker") == alternative.ticker
            ),
            {},
        )
        adv_yen: Decimal | None
        try:
            adv_yen = finite_decimal(liquidity.get("avg_turnover_oku")) * 100000000
            if adv_yen <= 0:
                adv_yen = None
        except ValueError:
            adv_yen = None
        ledger, _ = load_ledger_in_transaction(connection)
    if order_session is None or target_session != order_session:
        entry = replace(
            entry,
            eligible=False,
            quantity=0,
            reasons=(
                *entry.reasons,
                "order_session_unknown"
                if order_session is None
                else "target_session_not_next_available",
            ),
        )
    price = None if quote is None else Decimal(str(quote.close_yen))
    notional = Decimal(0) if price is None else price * entry.quantity
    warnings: list[str] = []
    if entry.eligible:
        if notional > budget_max_yen:
            warnings.append("budget_guide_exceeded")
        if notional < budget_min_yen:
            warnings.append("budget_guide_under")
    participation = None if adv_yen is None else notional * 100 / adv_yen
    if participation is None:
        warnings.append("adv_participation_unassessed")
    elif participation > Decimal(str(PORTFOLIO_POLICY["risk_budget"]["max_adv_participation_pct"])):
        warnings.append("adv_participation_exceeds_warning")
    portfolio = current_portfolio(ledger, sqlite_path=sqlite_path, now=now)
    metadata = {item.ticker: (item.sector, item.common_factors) for item in portfolio.holdings}
    nav = None if portfolio.total_capital_yen is None else Decimal(portfolio.total_capital_yen)
    values = {
        item.ticker: Decimal(item.market_value_yen)
        for item in portfolio.holdings
        if item.market_value_yen is not None
    }
    warnings.extend(
        f"portfolio_valuation_unknown:{item.ticker}"
        for item in portfolio.holdings
        if item.market_value_yen is None
    )
    unclassified = sorted(
        {item.ticker for item in portfolio.holdings if not item.common_factors}
        | {item.ticker for item in portfolio.active_reservations if not item.common_factors}
        | (
            {alternative.ticker}
            if entry.eligible and not pair.document.input_snapshot.common_factors
            else set()
        )
    )
    if unclassified:
        warnings.append("portfolio_exposure_common_factor_coverage_incomplete")
    exposure: dict[str, object] = {
        "total_capital_yen": None if nav is None else decimal_to_number(nav),
        "ticker": None,
        "sector": None,
        "common_factors": [],
        "common_factor_unclassified_tickers": unclassified,
    }
    if nav is None or nav <= 0:
        warnings.append("concentration_and_dry_powder_unassessed")
    elif entry.eligible:
        sector = pair.document.input_snapshot.sector
        factors = pair.document.input_snapshot.common_factors

        def value_for(scope: str, key: str) -> Decimal:
            def matches(ticker: str, item_sector: str, item_factors: tuple[str, ...]) -> bool:
                return (
                    (scope == "ticker" and ticker == key)
                    or (scope == "sector" and item_sector == key)
                    or (scope == "common_factor" and key in item_factors)
                )

            total = sum(
                (value for ticker, value in values.items() if matches(ticker, *metadata[ticker])),
                Decimal(0),
            )
            total += sum(
                (
                    Decimal(item.remaining_quantity) * item.price_guard_yen
                    for item in portfolio.active_reservations
                    if matches(item.ticker, item.sector, item.common_factors)
                ),
                Decimal(0),
            )
            return total

        for scope, keys, threshold in (
            (
                "ticker",
                (alternative.ticker,),
                PORTFOLIO_POLICY["risk_budget"]["max_ticker_concentration_pct"],
            ),
            ("sector", (sector,), PORTFOLIO_POLICY["risk_budget"]["max_sector_concentration_pct"]),
            (
                "common_factor",
                factors,
                PORTFOLIO_POLICY["risk_budget"]["max_common_factor_concentration_pct"],
            ),
        ):
            rows = []
            for key in keys:
                ratio = (value_for(scope, key) + notional) * 100 / nav
                rows.append({"key": key, "prospective_pct": float(ratio), "warning_pct": threshold})
                if ratio > threshold:
                    warnings.append(f"prospective_{scope}_concentration_exceeds_warning:{key}")
            exposure["common_factors" if scope == "common_factor" else scope] = (
                rows if scope == "common_factor" else rows[0]
            )
        if (
            Decimal(cash) - notional
            < nav
            * Decimal(str(PORTFOLIO_POLICY["cash_management"]["dry_powder_warning_pct"]))
            / 100
        ):
            warnings.append("dry_powder_below_floor")
    return {
        "status": "planned_limit" if entry.eligible else "defer",
        "ticker": alternative.ticker,
        "decision_reference": capital_allocation_assessment_id,
        "thesis_id": pair.thesis_id,
        "thesis_core_sha256": pair.core_sha256,
        "thesis_review_id": pair.review.review_id,
        "valuation_as_of": pair.document.input_snapshot.as_of.isoformat(),
        "price_as_of": None if quote is None else quote.price_as_of.isoformat(),
        "price_basis": "last_close_unadjusted",
        "source_ref": "jquants_daily_bars",
        "close_yen": None if price is None else decimal_to_number(price),
        "current_price_projection": entry.current_price_projection,
        "max_acceptable_price_yen": None
        if entry.maximum_price_yen is None
        else decimal_to_number(entry.maximum_price_yen),
        "limit_price_yen": None
        if not entry.eligible or price is None
        else decimal_to_number(price),
        "quantity": entry.quantity,
        "notional_yen": decimal_to_number(notional),
        "available_cash_yen": cash,
        "board_lot": PORTFOLIO_POLICY["order_constraints"]["board_lot"],
        "budget_min_yen": budget_min_yen,
        "budget_max_yen": budget_max_yen,
        "portfolio_exposure": exposure,
        "liquidity_context": {
            "research_triage_id": assessment.research_triage_id,
            "as_of": liquidity_as_of,
            "adv_yen": None if adv_yen is None else decimal_to_number(adv_yen),
            "participation_pct": None if participation is None else float(participation),
        },
        "judgment_as_of": now.date().isoformat(),
        "target_session": target_session.isoformat(),
        "next_order_session": None if order_session is None else order_session.isoformat(),
        "expires_at": None
        if not entry.eligible
        else datetime.combine(target_session, time(15, 30), tzinfo=JST).isoformat(),
        "warnings": warnings,
        "defer_reasons": list(entry.reasons),
    }
