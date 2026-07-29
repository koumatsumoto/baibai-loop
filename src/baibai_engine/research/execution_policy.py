"""The entry price a thesis can justify, derived from its own 5-year base scenario."""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal

from baibai_engine.research.thesis import ThesisDocument


class ExecutionPolicyError(ValueError):
    """A thesis cannot produce a usable entry price."""


def max_acceptable_price(document: ThesisDocument, *, tick_size_yen: Decimal) -> Decimal:
    """Derive the maximum entry price from the thesis's 5-year base scenario.

    The ceiling is the terminal value the thesis itself claims, discounted at the
    required return the thesis itself states. Paying above it means the thesis no
    longer supports the purchase at that price, whatever the market is quoting.
    """

    scenario = next(
        (
            item
            for item in document.estimates.scenarios
            if item.horizon_years == 5 and item.name == "base"
        ),
        None,
    )
    if scenario is None:
        raise ExecutionPolicyError("thesis has no 5y/base scenario")
    growth = Decimal(str(scenario.annual_earnings_growth_pct)) / 100
    share_change = Decimal(str(scenario.annual_share_count_change_pct)) / 100
    terminal_earnings = scenario.starting_earnings_yen * (Decimal(1) + growth) ** 5
    terminal_shares = scenario.starting_share_count * (Decimal(1) + share_change) ** 5
    if terminal_shares <= 0:
        raise ExecutionPolicyError("5y/base terminal share count must remain positive")
    terminal_price = terminal_earnings / terminal_shares * scenario.terminal_valuation_multiple
    terminal_total = terminal_price + scenario.cumulative_dividend_per_share_yen
    required_return = Decimal(str(document.estimates.required_5y_base_cagr_pct)) / 100
    raw_max_price = terminal_total / (Decimal(1) + required_return) ** 5
    # Floor to a legal tick: a ceiling rounded up would authorise a price the
    # thesis does not support.
    rounded = (raw_max_price / tick_size_yen).to_integral_value(
        rounding=ROUND_FLOOR
    ) * tick_size_yen
    if rounded <= 0:
        raise ExecutionPolicyError("max acceptable price must remain positive after tick rounding")
    return rounded


__all__ = ["ExecutionPolicyError", "max_acceptable_price"]
