"""配分工程で現在の quote・確認済み資本から一時的な価格数量案を産む。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

from baibai_engine.appdb.paths import database_path
from baibai_engine.appdb.read import connect_read_only
from baibai_engine.foundation.time import JST
from baibai_engine.position.ledger import ExecutionEvent, replay_events_through
from baibai_engine.position.policy import PORTFOLIO_POLICY
from baibai_engine.position.store import load_ledger_in_transaction
from baibai_engine.research.capital_allocation import (
    AllocationAlternative,
    CapitalAllocationAssessment,
)
from baibai_engine.research.decimal_number import decimal_to_number
from baibai_engine.research.entry_policy import EntryResult, evaluate_entry
from baibai_engine.research.market_close_source import (
    UnadjustedCloseObservation,
    read_holding_unadjusted_close_on_basis,
    read_unadjusted_close,
)
from baibai_engine.research.thesis_store import (
    ReviewedThesis,
    latest_thesis_id,
    load_reviewed_thesis,
)
from baibai_engine.research.valuation import finite_decimal


def evaluate_allocation_in_transaction(
    connection: sqlite3.Connection,
    *,
    alternative: AllocationAlternative,
    assessment_id: str,
    as_of: date,
    now: datetime,
    sqlite_path: Path,
    budget_max_yen: int,
) -> tuple[ReviewedThesis, EntryResult, UnadjustedCloseObservation | None, int]:
    """Assemble real current inputs for the shared CAA/Planning entry rule."""
    pair = load_reviewed_thesis(connection, alternative.thesis_id)
    if (
        pair.document.input_snapshot.ticker != alternative.ticker
        or pair.core_sha256 != alternative.thesis_core_sha256
        or pair.review.review_id != alternative.thesis_review_id
    ):
        raise ValueError("allocated Reviewed Thesis binding differs")
    document, _ = load_ledger_in_transaction(connection)
    if document.as_of > now or any(event.occurred_at > now for event in document.events):
        raise ValueError("ledger contains facts after the judgment instant")
    state = replay_events_through(document.events, now)
    quote = read_unadjusted_close(sqlite_path=sqlite_path, ticker=alternative.ticker, at=now)
    original_price = next(
        (
            fact
            for fact in pair.document.input_snapshot.facts
            if fact.fact_id == pair.document.valuation.market_price_fact_id
        ),
        None,
    )
    basis_confirmed = (
        quote is not None
        and original_price is not None
        and (
            read_holding_unadjusted_close_on_basis(
                sqlite_path=sqlite_path,
                ticker=alternative.ticker,
                ledger_price_observed_on=original_price.as_of,
                basis_as_of=quote.price_as_of,
            )
            is not None
        )
    )
    result = evaluate_entry(
        pair.document,
        reviewed=True,
        latest=latest_thesis_id(connection, alternative.ticker) == pair.thesis_id,
        as_of=as_of,
        price_yen=None if quote is None else Decimal(str(quote.close_yen)),
        price_as_of=None if quote is None else quote.price_as_of,
        basis_confirmed=basis_confirmed,
        minimum_required_annual_return_pct=Decimal(
            str(PORTFOLIO_POLICY["valuation"]["minimum_required_annual_return_pct"])
        ),
        market_price_max_age_days=PORTFOLIO_POLICY["valuation"]["market_price_max_age_days"],
        held_quantity=sum(lot.quantity for lot in state.lots.get(alternative.ticker, [])),
        active_reservation=any(
            item.ticker == alternative.ticker for item in state.active_reservations.values()
        ),
        assessment_executed=any(
            isinstance(event, ExecutionEvent)
            and event.side == "buy"
            and event.decision_reference == assessment_id
            for event in document.events
        ),
        available_cash_yen=Decimal(state.available_cash_yen),
        budget_max_yen=Decimal(budget_max_yen),
        board_lot=PORTFOLIO_POLICY["order_constraints"]["board_lot"],
    )
    return pair, result, quote, state.available_cash_yen


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
    if target_session != now.date():
        raise ValueError("formal planning basis must be today")
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
            as_of=target_session,
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
        state = replay_events_through(ledger.events, now)
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
    # Capital without every holding quote is unknown, not an invented cash-only NAV.
    nav: Decimal | None = Decimal(state.available_cash_yen + state.reserved_cash_yen)
    values: dict[str, Decimal] = {}
    for ticker, lots in state.lots.items():
        quantity = sum(lot.quantity for lot in lots)
        if not quantity:
            continue
        holding_quote = read_unadjusted_close(sqlite_path=sqlite_path, ticker=ticker, at=now)
        observed = next((item for item in ledger.market_prices if item.ticker == ticker), None)
        if (
            holding_quote is None
            or observed is None
            or read_holding_unadjusted_close_on_basis(
                sqlite_path=sqlite_path,
                ticker=ticker,
                ledger_price_observed_on=observed.observed_at.date(),
                basis_as_of=holding_quote.price_as_of,
            )
            is None
        ):
            nav = None
            warnings.append(f"portfolio_valuation_unknown:{ticker}")
        else:
            values[ticker] = Decimal(str(holding_quote.close_yen)) * quantity
    if nav is not None:
        nav += sum(values.values())
    exposure: dict[str, object] = {
        "total_capital_yen": None if nav is None else decimal_to_number(nav),
        "ticker": None,
        "sector": None,
        "common_factors": [],
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
                (
                    value
                    for ticker, value in values.items()
                    if matches(ticker, *state.metadata[ticker])
                ),
                Decimal(0),
            )
            total += sum(
                (
                    Decimal(item.remaining_quantity) * item.price_guard_yen
                    for item in state.active_reservations.values()
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
        "expires_at": datetime.combine(target_session, time(15, 30), tzinfo=JST).isoformat(),
        "warnings": warnings,
        "defer_reasons": list(entry.reasons),
    }
