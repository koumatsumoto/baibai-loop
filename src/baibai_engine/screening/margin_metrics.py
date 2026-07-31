"""Supply/demand axes derived from the exchange's weekly margin balances.

These sit beside valuation, quality and liquidity rather than inside them: a
margin balance says who is already positioned, which is a different question from
what a business is worth. The axes exist to make two situations visible that a
valuation screen cannot see on its own — a cheap-looking name that retail margin
buyers have crowded into, and a name whose margin longs have already been flushed
out. They are measured on the calibration panel before any of them is allowed to
change a rule.

Balances are reported in shares as of the balance date and are not restated for
later splits, so an axis that spans a split would compare two different share
bases. Rather than guess an adjustment, an axis whose window contains a split
declines to answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .providers.jquants import JQuantsWeeklyMargin

# A 信用銘柄 carries no stock lending, so it has no margin short balance to speak
# of. Its long side cannot be placed on a crowding scale that is defined by the
# short side, and mixing the two into one cross-section would rank instruments
# against a quantity half of them cannot have.
ISSUE_TYPE_LENDING_ELIGIBLE = "2"


@dataclass(frozen=True, slots=True, kw_only=True)
class MarginSupplyDemand:
    """One ticker's supply/demand facts as of the latest published balance date."""

    margin_week_end: date | None = None
    margin_issue_type: str | None = None
    # Margin long balance expressed in days of trading. A balance worth many days
    # of volume can only be unwound into the market slowly, so it is overhang on
    # the way up regardless of the absolute share count, and it is comparable
    # across names of different sizes.
    margin_long_to_adv: float | None = None
    # Long share of the two-sided margin balance, in [0, 1]. The classic ratio
    # (long / short) is undefined exactly where crowding is most extreme — a
    # 貸借銘柄 with longs and no shorts — which would drop its own tail out of the
    # cross-section. The share is defined everywhere the balance exists and orders
    # the same way, so it is the form the axis takes.
    margin_long_share: float | None = None
    # Half-year change in the margin long balance, scaled by shares outstanding so
    # it compares across names. A large negative reading alongside a drawdown is
    # what a washout looks like from the positioning side.
    margin_long_delta_26w: float | None = None
    # Share of the long balance held on standard margin, which carries a six-month
    # settlement deadline. A high share means part of the overhang is on a clock.
    margin_std_long_share: float | None = None


def margin_supply_demand(
    *,
    latest: JQuantsWeeklyMargin | None,
    prior_26w: JQuantsWeeklyMargin | None,
    avg_daily_volume_shares: float | None,
    shares_outstanding: float | None,
    split_within_adv_window: bool = False,
    split_within_delta_window: bool = False,
) -> MarginSupplyDemand:
    """Derive the supply/demand axes for one ticker.

    An axis is None when its inputs are missing or when its window crosses a
    split, never defaulted: a zero balance and an unobserved balance mean opposite
    things, and a share count that changed mid-window is not a balance that fell.
    A zero long balance still yields a zero `margin_long_to_adv` — no overhang is
    an observation — while `margin_std_long_share` stays None there, because a
    share of nothing is not zero, it is undefined.
    """
    if latest is None:
        return MarginSupplyDemand()
    long_vol = latest.long_vol
    short_vol = latest.short_vol
    two_sided = (
        long_vol + short_vol
        if latest.issue_type == ISSUE_TYPE_LENDING_ELIGIBLE
        and long_vol is not None
        and short_vol is not None
        else None
    )
    return MarginSupplyDemand(
        margin_week_end=latest.week_end,
        margin_issue_type=latest.issue_type,
        margin_long_to_adv=(
            long_vol / avg_daily_volume_shares
            if long_vol is not None and avg_daily_volume_shares and not split_within_adv_window
            else None
        ),
        margin_long_share=(long_vol / two_sided if two_sided and long_vol is not None else None),
        margin_long_delta_26w=(
            (long_vol - prior_26w.long_vol) / shares_outstanding
            if long_vol is not None
            and prior_26w is not None
            and prior_26w.long_vol is not None
            and shares_outstanding
            and not split_within_delta_window
            else None
        ),
        margin_std_long_share=(
            latest.long_std_vol / long_vol if latest.long_std_vol is not None and long_vol else None
        ),
    )
