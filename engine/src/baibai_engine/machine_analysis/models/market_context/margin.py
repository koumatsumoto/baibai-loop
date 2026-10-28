"""Supply/demand axes derived from the exchange's published margin balances.

These sit beside valuation, quality and liquidity rather than inside them: a
margin balance says who is already positioned, which is a different question from
what a business is worth. The axes exist to make two situations visible that a
valuation screen cannot see on its own — a cheap-looking name that retail margin
buyers have crowded into, and a name whose margin longs have already been flushed
out. They are measured on the calibration panel before any of them is allowed to
change a rule.

Balances are reported in shares on their own balance dates. Known split and reverse-split
factors put balances and traded volumes on one as-of share-unit basis. Price-only rights
factors do not rescale shares. Unknown action kinds leave only the affected comparisons
unresolved, rather than substituting a price factor for a share conversion.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from baibai_engine.machine_analysis.observations.prices.price_basis import cumulative_share_unit_factor_after
from baibai_engine.machine_analysis.observations.prices.price_models import JQuantsAdjustmentFactorEvent, JQuantsDailyBar

MARKET_CONTEXT_CALCULATION_REVISION = "native-master-and-margin-publication-v5"

# A 信用銘柄 carries no stock lending, so it has no margin short balance to speak
# of. Its long side cannot be placed on a crowding scale that is defined by the
# short side, and mixing the two into one cross-section would rank instruments
# against a quantity half of them cannot have.
ISSUE_TYPE_LENDING_ELIGIBLE = "2"


@dataclass(frozen=True, slots=True, kw_only=True)
class MarginBalance:
    """One ticker's margin balance on one balance date, whichever series published it.

    The exchange publishes the same six quantities weekly through 2026-09-18 and for
    every issue daily from 2026-09-25, so an axis that named one of those two tables
    would stop answering on the day the other one took over. What the axis needs is
    the balance, not the cadence that produced it — this type is that need stated
    once, and the reader is where a balance date is resolved to its series.
    """

    balance_date: date
    issue_type: str | None = None
    long_vol: float | None = None
    short_vol: float | None = None
    long_std_vol: float | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class MarginSupplyDemand:
    """One ticker's supply/demand facts as of the latest published balance date."""

    margin_balance_date: date | None = None
    margin_issue_type: str | None = None
    # Balance divided by mean observed daily volume on the same share basis.
    # This scale comparison is not a forecast of how long liquidation would take.
    margin_long_to_adv: float | None = None
    # Short balance in days of trading. This is deliberately not expressed as a
    # share of the two-sided balance: that would be the exact complement of
    # `margin_long_share` and would only relabel an already observed result.
    margin_short_to_adv: float | None = None
    # Composition of the observed long/short balance, not absolute crowding.
    # Unlike long/short, this remains defined when only the long side is positive.
    margin_long_share: float | None = None
    # Change over 26 observed ISO weeks, scaled by as-of shares outstanding so
    # it compares across names. A decline is not proof of forced liquidation.
    margin_long_delta_26w: float | None = None
    # Share of the long balance held on standard margin, which carries a six-month
    # settlement deadline. A high share means part of the overhang is on a clock.
    margin_std_long_share: float | None = None


def margin_supply_demand(
    *,
    latest: MarginBalance | None,
    prior_26w: MarginBalance | None,
    avg_daily_volume_shares: float | None,
    shares_outstanding: float | None,
    asof_date: date,
    adjustment_events: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar] = (),
) -> MarginSupplyDemand:
    """Derive the supply/demand axes for one ticker.

    Balances use their actual observation dates, not an approximate 130-session
    window. ADV and outstanding shares must already use the as-of basis. Missing
    values and unknown share-unit changes remain unresolved; zero stays observed.
    A zero long balance still yields a zero `margin_long_to_adv` — no overhang is
    an observation — while `margin_std_long_share` stays None there, because a
    share of nothing is not zero, it is undefined.
    """
    if latest is None:
        return MarginSupplyDemand()
    long_vol = latest.long_vol
    short_vol = latest.short_vol
    latest_factor = cumulative_share_unit_factor_after(
        adjustment_events, latest.balance_date, asof_date
    )
    prior_factor = (
        cumulative_share_unit_factor_after(adjustment_events, prior_26w.balance_date, asof_date)
        if prior_26w is not None
        else None
    )
    asof_long = _asof_quantity(long_vol, latest_factor)
    asof_short = _asof_quantity(short_vol, latest_factor)
    prior_long = _asof_quantity(prior_26w.long_vol, prior_factor) if prior_26w is not None else None
    two_sided = (
        long_vol + short_vol
        if latest.issue_type == ISSUE_TYPE_LENDING_ELIGIBLE
        and long_vol is not None
        and short_vol is not None
        else None
    )
    return MarginSupplyDemand(
        margin_balance_date=latest.balance_date,
        margin_issue_type=latest.issue_type,
        margin_long_to_adv=(
            asof_long / avg_daily_volume_shares
            if asof_long is not None and avg_daily_volume_shares
            else None
        ),
        margin_short_to_adv=(
            asof_short / avg_daily_volume_shares
            if latest.issue_type == ISSUE_TYPE_LENDING_ELIGIBLE
            and asof_short is not None
            and avg_daily_volume_shares
            else None
        ),
        margin_long_share=(long_vol / two_sided if two_sided and long_vol is not None else None),
        margin_long_delta_26w=(
            (asof_long - prior_long) / shares_outstanding
            if asof_long is not None and prior_long is not None and shares_outstanding
            else None
        ),
        margin_std_long_share=(
            latest.long_std_vol / long_vol if latest.long_std_vol is not None and long_vol else None
        ),
    )


def _asof_quantity(value: float | None, factor: float | None) -> float | None:
    if value == 0:
        return 0.0
    return value / factor if value is not None and factor else None
