from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

# Pure derived-series formulas: each maps aligned input values to one output
# value with no I/O. A formula names its input series and, for each aligned
# observed_at, computes the output. Registry bounds are checked once by the
# indicator service, just like externally fetched observations.

type Compute = Callable[[Mapping[str, float]], float | None]

# How the provider pairs input observations before computing:
#   "exact"   - inputs share a frequency; align on identical observed_at.
#   "monthly" - inputs mix frequencies; fold each input to one value per calendar
#               month (a daily input collapses to its month-end reading) and pair
#               on the month. The formula declares how that bundle is dated.
type Alignment = Literal["exact", "monthly"]
type MonthlyObservationDate = Literal["period_start", "latest_input"]


@dataclass(frozen=True, slots=True, kw_only=True)
class DerivedFormula:
    inputs: tuple[str, ...]
    unit: str
    # Compute one output value from the aligned input values (keyed by series_id).
    # Return None to skip an observed_at (e.g. a zero divisor).
    compute: Compute
    alignment: Alignment = "exact"
    # Existing monthly formulas use a canonical month-start grid. A formula that
    # consumes within-month market data can instead date output at the latest
    # selected input to avoid making month-end information appear at month start.
    monthly_observation_date: MonthlyObservationDate = "period_start"

    def __post_init__(self) -> None:
        if self.alignment != "monthly" and self.monthly_observation_date != "period_start":
            raise ValueError("monthly_observation_date requires monthly alignment")


# provider_series_id -> formula. provider_series_id equals the derived series_id.
FORMULAS: Mapping[str, DerivedFormula] = {
    "us.net_liquidity": DerivedFormula(
        inputs=("us.fed_assets", "us.reverse_repo", "us.tga"),
        unit="usd-billion",
        compute=lambda v: (v["us.fed_assets"] - v["us.tga"]) / 1000.0 - v["us.reverse_repo"],
    ),
    "us.erp": DerivedFormula(
        inputs=("us.sp500_earnings_yield", "us.10y"),
        unit="percent",
        compute=lambda v: v["us.sp500_earnings_yield"] - v["us.10y"],
        alignment="monthly",
        monthly_observation_date="latest_input",
    ),
    "rate_diff.us_jp_10y": DerivedFormula(
        inputs=("us.10y", "jp.10y"),
        unit="percent",
        compute=lambda v: v["us.10y"] - v["jp.10y"],
    ),
    # Observed spread between a two-year bond yield and a policy-rate reference.
    # Different maturities and instruments are compared; this does not isolate
    # expected policy changes or assign probabilities to policy decisions.
    "us.policy_path_gap": DerivedFormula(
        # Use the declared target-range midpoint, not its upper boundary.
        inputs=("us.2y", "us.fed_funds.upper", "us.fed_funds.lower"),
        unit="percent",
        compute=lambda v: v["us.2y"] - (v["us.fed_funds.upper"] + v["us.fed_funds.lower"]) / 2.0,
    ),
    "jp.policy_path_gap": DerivedFormula(
        inputs=("jp.2y", "jp.policy_rate"),
        unit="percent",
        compute=lambda v: v["jp.2y"] - v["jp.policy_rate"],
    ),
    "credit.hy_ig_spread": DerivedFormula(
        inputs=("credit.us_hy_oas", "credit.us_ig_oas"),
        unit="bp",
        compute=lambda v: (v["credit.us_hy_oas"] - v["credit.us_ig_oas"]) * 100.0,
    ),
    "gold_copper_ratio": DerivedFormula(
        inputs=("gold", "copper"),
        unit="ratio",
        compute=lambda v: v["gold"] / v["copper"] if v["copper"] else None,
    ),
    "jp.erp": DerivedFormula(
        inputs=("jp.nikkei_per", "jp.10y"),
        unit="percent",
        # Nikkei earnings yield (100 / PER) minus the 10Y JGB yield.
        compute=lambda v: 100.0 / v["jp.nikkei_per"] - v["jp.10y"] if v["jp.nikkei_per"] else None,
    ),
    "jp.terms_of_trade": DerivedFormula(
        inputs=("jp.export_price_index", "jp.import_price_index"),
        unit="ratio",
        compute=lambda v: (
            v["jp.export_price_index"] / v["jp.import_price_index"]
            if v["jp.import_price_index"]
            else None
        ),
    ),
    "jp.real_10y_proxy": DerivedFormula(
        # Month-end nominal JGB yield minus observed core-CPI YoY, not expected
        # inflation or a market-implied real yield. The monthly CPI observation
        # determines this proxy's frequency.
        inputs=("jp.10y", "jp.cpi.core_yoy"),
        unit="percent",
        compute=lambda v: v["jp.10y"] - v["jp.cpi.core_yoy"],
        alignment="monthly",
    ),
}
