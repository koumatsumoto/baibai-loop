"""Derive a thesis scenario's claimed terminal values from its parameters.

`research evaluate` recomputes every scenario and rejects a thesis whose claimed values
disagree, so the author has to produce them at the same rounding first. This calls the
engine's own `project_scenario`, which is the function the claim comparison measures
against — there is no second implementation of the formula to drift.

`--required-cagr-pct` additionally discounts the 5-year base path back to the price that
would return exactly that CAGR. That price is the usual anchor for
`estimates.current_fair_value_yen`, but the discount convention is the analyst's, not a
schema contract: a thesis may set fair value another way and say so.

    uv run python tools/scenario_arithmetic.py \
      --entry-price 1217 --starting-earnings 8000000000 --starting-shares 87870663 \
      --scenario bear:5:-2.0:-1.0:10.0:145 \
      --scenario base:5:3.0:-1.2:12.0:155 \
      --scenario bull:5:7.0:-2.0:14.0:175 \
      --required-cagr-pct 8.5
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass

import yaml

from baibai_engine.research.thesis import project_scenario

_SCENARIO_FIELDS = "name:horizon:growth_pct:share_change_pct:multiple:cumulative_dividend"


@dataclass(frozen=True, slots=True)
class ScenarioSpec:
    name: str
    horizon_years: int
    annual_earnings_growth_pct: float
    annual_share_count_change_pct: float
    terminal_valuation_multiple: float
    cumulative_dividend_per_share_yen: float


def parse_scenario(spec: str) -> ScenarioSpec:
    """Parse `base:5:3.0:-1.2:12.0:155` into a scenario specification."""

    parts = spec.split(":")
    if len(parts) != 6:
        raise ValueError(f"scenario must be {_SCENARIO_FIELDS}, got {spec!r}")
    name, horizon, growth, share_change, multiple, dividend = parts
    if name not in {"bear", "base", "bull"}:
        raise ValueError(f"scenario name must be bear/base/bull, got {name!r}")
    if horizon not in {"3", "5"}:
        raise ValueError(f"scenario horizon must be 3 or 5, got {horizon!r}")
    return ScenarioSpec(
        name=name,
        horizon_years=int(horizon),
        annual_earnings_growth_pct=float(growth),
        annual_share_count_change_pct=float(share_change),
        terminal_valuation_multiple=float(multiple),
        cumulative_dividend_per_share_yen=float(dividend),
    )


def build_claims(
    specs: Sequence[ScenarioSpec],
    *,
    entry_price_yen: float,
    starting_earnings_yen: float,
    starting_share_count: float,
) -> dict[str, dict[str, float]]:
    """Return the four claimed_* values per scenario, keyed `<horizon>y/<name>`."""

    claims: dict[str, dict[str, float]] = {}
    for spec in specs:
        projection = project_scenario(
            horizon_years=spec.horizon_years,
            starting_earnings_yen=starting_earnings_yen,
            annual_earnings_growth_pct=spec.annual_earnings_growth_pct,
            starting_share_count=starting_share_count,
            annual_share_count_change_pct=spec.annual_share_count_change_pct,
            terminal_valuation_multiple=spec.terminal_valuation_multiple,
            cumulative_dividend_per_share_yen=spec.cumulative_dividend_per_share_yen,
            entry_price_yen=entry_price_yen,
        )
        claims[f"{spec.horizon_years}y/{spec.name}"] = {
            "claimed_terminal_earnings_yen": projection.terminal_earnings_yen,
            "claimed_terminal_share_count": projection.terminal_share_count,
            "claimed_terminal_price_yen": projection.terminal_price_yen,
            "claimed_total_return_cagr_pct": projection.total_return_cagr_pct,
        }
    return claims


def required_return_price(
    claims: dict[str, dict[str, float]],
    specs: Sequence[ScenarioSpec],
    *,
    required_cagr_pct: float,
) -> float | None:
    """Discount the 5-year base terminal value back at the required CAGR."""

    base = next((spec for spec in specs if spec.name == "base" and spec.horizon_years == 5), None)
    if base is None:
        return None
    terminal_price = claims["5y/base"]["claimed_terminal_price_yen"]
    total_value = terminal_price + base.cumulative_dividend_per_share_yen
    return round(total_value / (1 + required_cagr_pct / 100) ** 5, 4)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python tools/scenario_arithmetic.py", description=__doc__
    )
    parser.add_argument("--entry-price", type=float, required=True, help="entry price in JPY")
    parser.add_argument(
        "--starting-earnings", type=float, required=True, help="starting earnings in JPY"
    )
    parser.add_argument(
        "--starting-shares",
        type=float,
        required=True,
        help="starting share count excluding treasury stock",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        required=True,
        metavar=_SCENARIO_FIELDS,
        help="repeatable; e.g. base:5:3.0:-1.2:12.0:155",
    )
    parser.add_argument(
        "--required-cagr-pct",
        type=float,
        help="discount the 5y base path back to the price returning this CAGR",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        specs = [parse_scenario(item) for item in args.scenario]
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2

    claims = build_claims(
        specs,
        entry_price_yen=args.entry_price,
        starting_earnings_yen=args.starting_earnings,
        starting_share_count=args.starting_shares,
    )
    payload: dict[str, object] = {"scenarios": claims}
    if args.required_cagr_pct is not None:
        price = required_return_price(claims, specs, required_cagr_pct=args.required_cagr_pct)
        if price is None:
            print("no 5y base scenario: required-return price skipped", file=sys.stderr)
        else:
            payload["price_at_required_cagr_yen"] = price
    print(yaml.safe_dump(payload, allow_unicode=True, sort_keys=True).rstrip())
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
