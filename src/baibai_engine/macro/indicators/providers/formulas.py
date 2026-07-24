from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

# Pure derived-series formulas: each maps aligned input values to one output
# value with no I/O. A formula names its input series and, for each observed_at
# present in every input, computes the output. Range checks live here so a
# derived misprint cannot enter the store.

type Compute = Callable[[Mapping[str, float]], float | None]


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
}
