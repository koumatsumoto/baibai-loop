"""保有を続けた場合に対する、作者が明示した回収案の差。資金・注文を更新しない。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from baibai_engine.investment_judgment.trade_proposal.contracts import (
    Instant,
    ISODate,
    NonEmptyStr,
    NonnegativeMoney,
    PositiveInt,
    ProposalError,
    TradeProposal,
    TradeProposalDraft,
    proposal_draft_sha256,
)
from baibai_engine.machine_analysis.observations.prices.sessions import latest_eligible_close_date

_ZERO = Decimal(0)


class _Assumption(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class DividendWindow(_Assumption):
    """作者が一次資料で確認した権利落ち日別の見込額。同日分は合計して一度だけ記載。"""

    source_ref: NonEmptyStr
    known_at: Instant
    covered_through: ISODate
    yen_per_share_by_ex_date: dict[ISODate, NonnegativeMoney | None]

    @field_validator("yen_per_share_by_ex_date", mode="before")
    @classmethod
    def _unique_dates(cls, value: object) -> object:
        if isinstance(value, dict):
            dates = {date.fromisoformat(key) if isinstance(key, str) else key for key in value}
            if len(dates) != len(value):
                raise ValueError("duplicate ex-rights date")
        return value

    def forgone_per_share(self, on: date, through: date) -> Decimal | None:
        if self.covered_through < through:
            return None
        result = _ZERO
        for ex_date, amount in self.yen_per_share_by_ex_date.items():
            if on < ex_date <= through:
                if amount is None:
                    return None
                result += amount
        return result


class AllocationAlternatives(_Assumption):
    quote_on: ISODate
    assumed_sale_on: ISODate
    dividend_rights_through: ISODate
    sector: NonEmptyStr
    gross_recovery_target_yen: NonnegativeMoney | None = None
    assume_unchanged_share_basis: bool = False
    # Use exact saved ticker keys. Do not silently combine ticker aliases or choose a basket.
    alternatives: dict[NonEmptyStr, dict[str, PositiveInt]] = Field(min_length=1)
    dividend_windows: dict[str, DividendWindow] = Field(default_factory=dict)

    @field_validator("alternatives", mode="before")
    @classmethod
    def _unique_names(cls, value: object) -> object:
        if isinstance(value, dict):
            names = {key.strip() if isinstance(key, str) else key for key in value}
            if len(names) != len(value):
                raise ValueError("duplicate comparison name")
        return value


def _pct(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    return (
        None
        if numerator is None or denominator is None or denominator == 0
        else numerator / denominator * 100
    )


def compare_holding_allocations(
    proposal: TradeProposalDraft, assumptions: AllocationAlternatives
) -> dict[str, object]:
    """All legs use the same captured holdings, close and entitlement window; no price forecast."""
    if proposal.request_scope != "review_positions":
        raise ProposalError("allocation comparison requires a holdings review")
    facts = proposal.inputs
    on, through = assumptions.quote_on, assumptions.dividend_rights_through
    sale_on = assumptions.assumed_sale_on
    decision = max(facts.checked_at, proposal.prepared_at)
    if proposal.review is not None:
        decision = max(decision, proposal.review.reviewed_at)
    if isinstance(proposal, TradeProposal):
        decision = max(decision, proposal.published_at)
    if (
        on > latest_eligible_close_date(facts.checked_at)
        or sale_on <= latest_eligible_close_date(decision)
        or sale_on.weekday() >= 5
        or through < sale_on
    ):
        raise ProposalError(
            "invalid reference close, assumed future session or dividend rights window"
        )
    positions = {row.ticker: row for row in facts.positions}
    scope = {target.ticker for target in proposal.research_scope.targets}
    quotes = {row.ticker: row for row in facts.quotes if row.price_as_of == on}
    group = {ticker for ticker, pos in positions.items() if pos.sector == assumptions.sector}
    if not group:
        raise ProposalError("sector must match a sector recorded in the fixed holdings")
    for ticker, window in assumptions.dividend_windows.items():
        if ticker not in positions or window.known_at > facts.checked_at:
            raise ProposalError(
                "dividend source is outside the fixed holdings or information cutoff"
            )
        if any(ex_date > window.covered_through for ex_date in window.yen_per_share_by_ex_date):
            raise ProposalError("dividend event is outside its source coverage")
    held = {ticker: pos.quantity for ticker, pos in positions.items()}
    unconfirmed = {ticker for ticker, pos in positions.items() if not pos.quantity_basis_confirmed}
    usable = set(quotes) & (
        set(held) if assumptions.assume_unchanged_share_basis else set(held) - unconfirmed
    )
    # A cum-rights close is not an ex-rights sale price. The actual price drop is not
    # necessarily the forecast distribution, so neither carry that close nor subtract the DPS.
    crossed_rights = {
        ticker
        for ticker, window in assumptions.dividend_windows.items()
        if any(
            on < ex_date <= sale_on and dps != 0
            for ex_date, dps in window.yen_per_share_by_ex_date.items()
        )
    }
    usable -= crossed_rights
    reserved = sum((r.reserved_yen for r in facts.active_reservations), _ZERO)
    recorded_cash = facts.available_cash_yen + reserved

    def value(quantities: dict[str, int], *, remaining: bool = False) -> Decimal | None:
        # Selling the entire *recorded* number does not establish a zero remainder if
        # units are unresolved and the author did not assume they were unchanged.
        if (
            remaining
            and not assumptions.assume_unchanged_share_basis
            and unconfirmed & quantities.keys()
        ):
            return None
        if any(qty and ticker not in usable for ticker, qty in quantities.items()):
            return None
        return sum((quotes[t].price_yen * n for t, n in quantities.items() if n), _ZERO)

    equity = value(held)
    denominator = None if equity is None else recorded_cash + equity

    def compare(sold: dict[str, int]) -> dict[str, object]:
        for ticker, qty in sold.items():
            if ticker not in scope or ticker not in held:
                raise ProposalError("a reduction must belong to the fixed holdings review")
            if qty > held[ticker] or qty % facts.board_lot:
                raise ProposalError("a reduction must fit the recorded holding in whole board lots")
        recovered = value(sold)
        left = {t: qty - sold.get(t, 0) for t, qty in held.items()}
        cash = None if recovered is None else recorded_cash + recovered
        stocks = value(left, remaining=True)
        sector = value({t: left[t] for t in group}, remaining=True)
        rights_unknown: list[str] = []
        forgone = _ZERO
        for ticker, qty in sold.items():
            window = assumptions.dividend_windows.get(ticker)
            dps = None if window is None else window.forgone_per_share(sale_on, through)
            if sale_on == through:
                dps = _ZERO  # No future ex-rights date lies inside an empty window.
            if dps is None or (
                dps != 0 and ticker in unconfirmed and not assumptions.assume_unchanged_share_basis
            ):
                rights_unknown.append(ticker)
            else:
                forgone += dps * qty
        # The saved total cost does not identify the FIFO/tax basis of a partial sale.
        full_cost_spread = (
            recovered - sum((positions[t].cost_yen for t in sold), _ZERO)
            if recovered is not None and all(n == held[t] for t, n in sold.items())
            else None
        )
        target = assumptions.gross_recovery_target_yen
        return {
            "assumed_reduction_shares": sold,
            "gross_recovery_yen": recovered,
            "gross_surplus_to_target_yen": None
            if recovered is None or target is None
            else recovered - target,
            "whole_positions_gross_spread_to_recorded_cost_yen": full_cost_spread,
            "cash_including_reserved_yen": cash,
            "cash_pct_of_cash_and_stocks": _pct(cash, denominator),
            "remaining_stocks_yen": stocks,
            "remaining_sector_yen": sector,
            "sector_pct_of_cash_and_stocks": _pct(sector, denominator),
            "sector_pct_of_remaining_stocks": _pct(sector, stocks),
            "foregone_forecast_dividend_yen": None if rights_unknown else forgone,
            "incomplete_dividend_tickers": sorted(rights_unknown),
        }

    if any(not sold for sold in assumptions.alternatives.values()):
        raise ProposalError("an alternative needs a reduction; keeping all is already the baseline")
    return {
        "role": "historical_reference_close_gross_scenarios_not_orders_cash_or_total_return",
        "proposal_id": proposal.proposal_id,
        "draft_sha256": proposal_draft_sha256(proposal),
        "inputs_checked_at": facts.checked_at.isoformat(),
        "ledger_as_of": facts.ledger_as_of.isoformat(),
        "ledger_head": facts.ledger_head,
        "recorded_reserved_cash_yen": reserved,
        "recorded_active_reservation_tickers": sorted(
            {r.ticker for r in facts.active_reservations}
        ),
        "recorded_total_capital_yen": facts.total_capital_yen,
        "unconfirmed_share_basis_tickers": sorted(unconfirmed),
        "known_ex_rights_since_quote_tickers": sorted(crossed_rights),
        "missing_reference_close_tickers": sorted(held.keys() - quotes.keys()),
        "assumptions": assumptions.model_dump(mode="json", exclude={"alternatives"}),
        "keep_all": compare({}),
        "alternatives": {name: compare(sold) for name, sold in assumptions.alternatives.items()},
    }
