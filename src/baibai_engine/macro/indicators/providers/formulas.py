from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

# Pure derived-series formulas: each maps aligned input values to one output
# value with no I/O. A formula names its input series and, for each aligned
# observed_at, computes the output. Range checks live here so a derived misprint
# cannot enter the store.

type Compute = Callable[[Mapping[str, float]], float | None]

# How the provider pairs input observations before computing:
#   "exact"   - inputs share a frequency; align on identical observed_at.
#   "monthly" - inputs mix frequencies; fold each input to one value per calendar
#               month (a daily input collapses to its month-end reading) and pair
#               on the month. The formula declares how that bundle is dated.
type Alignment = Literal["exact", "monthly"]
type MonthlyObservationDate = Literal["period_start", "latest_input"]


class DerivedComputationError(RuntimeError):
    """Raised when a derived value is internally inconsistent or implausible."""


@dataclass(frozen=True, slots=True, kw_only=True)
class DerivedFormula:
    inputs: tuple[str, ...]
    unit: str
    # Compute one output value from the aligned input values (keyed by series_id).
    # Return None to skip an observed_at (e.g. a zero divisor).
    compute: Compute
    plausible_min: float | None = None
    plausible_max: float | None = None
    alignment: Alignment = "exact"
    # Existing monthly formulas use a canonical month-start grid. A formula that
    # consumes within-month market data can instead date output at the latest
    # selected input to avoid making month-end information appear at month start.
    monthly_observation_date: MonthlyObservationDate = "period_start"

    def __post_init__(self) -> None:
        if self.alignment != "monthly" and self.monthly_observation_date != "period_start":
            raise ValueError("monthly_observation_date requires monthly alignment")

    def evaluate(self, aligned: Mapping[str, float]) -> float | None:
        value = self.compute(aligned)
        if value is None:
            return None
        low, high = self.plausible_min, self.plausible_max
        if (low is not None and value < low) or (high is not None and value > high):
            raise DerivedComputationError(
                f"derived value {value} outside plausible range [{low}, {high}]"
            )
        return value


# provider_series_id -> formula. provider_series_id equals the derived series_id.
FORMULAS: Mapping[str, DerivedFormula] = {
    "us.net_liquidity": DerivedFormula(
        inputs=("us.fed_assets", "us.reverse_repo", "us.tga"),
        unit="usd-billion",
        compute=lambda v: (v["us.fed_assets"] - v["us.reverse_repo"] - v["us.tga"]) / 1000.0,
    ),
    "us.erp": DerivedFormula(
        inputs=("us.sp500_earnings_yield", "us.10y"),
        unit="percent",
        compute=lambda v: v["us.sp500_earnings_yield"] - v["us.10y"],
        plausible_min=-10.0,
        plausible_max=15.0,
        alignment="monthly",
        monthly_observation_date="latest_input",
    ),
    "rate_diff.us_jp_10y": DerivedFormula(
        inputs=("us.10y", "jp.10y"),
        unit="percent",
        compute=lambda v: v["us.10y"] - v["jp.10y"],
        plausible_min=-5.0,
        plausible_max=10.0,
    ),
    "credit.hy_ig_spread": DerivedFormula(
        inputs=("credit.us_hy_oas", "credit.us_ig_oas"),
        unit="bp",
        compute=lambda v: (v["credit.us_hy_oas"] - v["credit.us_ig_oas"]) * 100.0,
        plausible_min=0.0,
        plausible_max=2500.0,
    ),
    "gold_copper_ratio": DerivedFormula(
        inputs=("gold", "copper"),
        unit="ratio",
        compute=lambda v: v["gold"] / v["copper"] if v["copper"] else None,
        plausible_min=0.0,
        plausible_max=2000.0,
    ),
    "jp.erp": DerivedFormula(
        inputs=("jp.nikkei_per", "jp.10y"),
        unit="percent",
        # Nikkei earnings yield (100 / PER) minus the 10Y JGB yield.
        compute=lambda v: 100.0 / v["jp.nikkei_per"] - v["jp.10y"] if v["jp.nikkei_per"] else None,
        plausible_min=-10.0,
        plausible_max=15.0,
    ),
    "jp.terms_of_trade": DerivedFormula(
        inputs=("jp.export_price_index", "jp.import_price_index"),
        unit="ratio",
        compute=lambda v: (
            v["jp.export_price_index"] / v["jp.import_price_index"]
            if v["jp.import_price_index"]
            else None
        ),
        plausible_min=0.2,
        plausible_max=5.0,
    ),
    "jp.real_10y_proxy": DerivedFormula(
        # Month-end 10Y JGB yield minus that month's core-CPI YoY: a real-yield
        # proxy Japan has no clean free daily linker series for. Inflation only
        # moves monthly, so a monthly cadence is the honest resolution rather
        # than a daily series holding inflation flat within the month.
        inputs=("jp.10y", "jp.cpi.core_yoy"),
        unit="percent",
        compute=lambda v: v["jp.10y"] - v["jp.cpi.core_yoy"],
        plausible_min=-8.0,
        plausible_max=8.0,
        alignment="monthly",
    ),
}
