"""Independent raw CSV/SQLite verification for the capacity replay artifact.

This module deliberately imports neither capacity evaluator.  It recomputes the
three preregistered representative cohorts from standard readers and compares
them with the result artifact.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import math
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import median

import yaml

CALIBRATION_DIR = Path("stores/screening/calibration")
MARKET_DB = Path("stores/market/market.sqlite")
TRADING_UNIT = 100
NOTIONAL = 300_000.0
PARTICIPATION = 0.01
TRAP_THRESHOLD = -0.20
TARGETS = ((date(2023, 6, 30), "1y"), (date(2022, 6, 30), "3y"), (date(2020, 6, 30), "5y"))


class IndependentVerificationError(ValueError):
    """Raised when the raw recomputation differs from the replay artifact."""


@dataclass(frozen=True, slots=True)
class Row:
    ticker: str
    er: float
    market_cap: float | None
    turnover: float | None
    listing_span: int | None
    minimum_lot: float | None
    p20: float
    usable: int
    nonzero_share: float
    capacity_days: float | None


def _number(value: object) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if not isinstance(value, str | int | float):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _truth(value: object) -> bool:
    return str(value).lower() in {"true", "1"}


def _p20(values: Sequence[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(0.20 * len(ordered)) - 1]


def _sessions(conn: sqlite3.Connection, asof: date) -> list[str]:
    rows = conn.execute(
        "SELECT traded_at FROM jquants_daily_bars WHERE traded_at <= ? "
        "GROUP BY traded_at HAVING COUNT(*) >= 2000 ORDER BY traded_at DESC LIMIT 60",
        (asof.isoformat(),),
    ).fetchall()
    days = [str(raw) for (raw,) in reversed(rows)]
    if len(days) != 60 or days[-1] != asof.isoformat():
        raise IndependentVerificationError(f"60-session window is incomplete: {asof}")
    return days


def _facts(conn: sqlite3.Connection, asof: date, tickers: frozenset[str]) -> dict[str, Row]:
    days = _sessions(conn, asof)
    by_ticker: dict[str, dict[str, tuple[float | None, ...]]] = {}
    for ticker, traded_at, close, adjusted, volume, turnover in conn.execute(
        "SELECT ticker, traded_at, close, adjustment_close, volume, turnover_value "
        "FROM jquants_daily_bars WHERE traded_at BETWEEN ? AND ?",
        (days[0], days[-1]),
    ):
        ticker_text = str(ticker)
        if ticker_text not in tickers:
            continue
        by_ticker.setdefault(ticker_text, {})[str(traded_at)] = (
            _number(close),
            _number(adjusted),
            _number(volume),
            _number(turnover),
        )
    result: dict[str, Row] = {}
    for ticker in tickers:
        values = by_ticker.get(ticker, {})
        turnovers: list[float] = []
        usable = 0
        nonzero = 0
        for day in days:
            close, adjusted, volume, turnover = values.get(day, (None, None, None, None))
            turnovers.append(turnover if turnover is not None and turnover >= 0 else 0.0)
            valid = (
                close is not None
                and close > 0
                and adjusted is not None
                and adjusted > 0
                and volume is not None
                and volume >= 0
                and turnover is not None
                and turnover >= 0
            )
            usable += valid
            if valid:
                assert volume is not None
                assert turnover is not None
                nonzero += volume > 0 and turnover > 0
        asof_values = values.get(days[-1])
        raw_close = asof_values[0] if asof_values is not None else None
        minimum_lot = raw_close * TRADING_UNIT if raw_close is not None and raw_close > 0 else None
        percentile = _p20(turnovers)
        capacity_days = (
            max(NOTIONAL, minimum_lot) / (percentile * PARTICIPATION)
            if minimum_lot is not None and percentile > 0
            else None
        )
        result[ticker] = Row(
            ticker=ticker,
            er=0,
            market_cap=None,
            turnover=None,
            listing_span=None,
            minimum_lot=minimum_lot,
            p20=percentile,
            usable=usable,
            nonzero_share=nonzero / 60,
            capacity_days=capacity_days,
        )
    return result


def _load_panel(path: Path, conn: sqlite3.Connection, asof: date) -> tuple[list[Row], set[str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        raw_rows = list(csv.DictReader(handle))
    screened = [
        raw
        for raw in raw_rows
        if _truth(raw.get("pass_screen"))
        and raw.get("population_coverage_status") == "evaluated"
        and _number(raw.get("er_annual")) is not None
    ]
    tickers = frozenset(str(raw["ticker"]) for raw in screened)
    facts = _facts(conn, asof, tickers)
    rows: list[Row] = []
    for raw in screened:
        ticker = str(raw["ticker"])
        fact = facts[ticker]
        er = _number(raw.get("er_annual"))
        assert er is not None
        rows.append(
            Row(
                ticker=ticker,
                er=er,
                market_cap=_number(raw.get("market_cap_oku")),
                turnover=_number(raw.get("avg_turnover_oku")),
                listing_span=(
                    int(value)
                    if (value := _number(raw.get("listing_span_days"))) is not None
                    and value.is_integer()
                    else None
                ),
                minimum_lot=fact.minimum_lot,
                p20=fact.p20,
                usable=fact.usable,
                nonzero_share=fact.nonzero_share,
                capacity_days=fact.capacity_days,
            )
        )
    population = {str(raw["ticker"]) for raw in raw_rows if _truth(raw.get("in_population"))}
    return rows, population


def _current(row: Row) -> bool:
    return (
        row.market_cap is not None
        and row.market_cap >= 100
        and row.turnover is not None
        and row.turnover >= 1
        and row.listing_span is not None
        and row.listing_span >= 182
    )


def _capacity(row: Row) -> bool:
    return (
        row.minimum_lot is not None
        and row.minimum_lot <= NOTIONAL
        and row.usable >= 57
        and row.nonzero_share >= 0.95
        and row.capacity_days is not None
        and row.capacity_days <= 5
        and row.listing_span is not None
        and row.listing_span >= 365
    )


def _policies(rows: Sequence[Row]) -> tuple[dict[str, list[Row]], list[Row]]:
    current = sorted((row for row in rows if _current(row)), key=lambda row: (-row.er, row.ticker))
    capacity = sorted(
        (row for row in rows if _capacity(row)), key=lambda row: (-row.er, row.ticker)
    )
    current_set = {row.ticker for row in current}
    capacity_only = [row for row in capacity if row.ticker not in current_set]
    capacity_top20 = capacity[:20]
    return (
        {
            "current_core_top20": current[:20],
            "capacity_core_top20": capacity_top20,
            "capacity_only_outside_current_top5": [
                row for row in capacity_top20 if row.ticker not in current_set
            ][:5],
            "current_boundary_21_25": current[20:25],
        },
        capacity_only,
    )


def _forward(path: Path, horizon: str) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            if raw.get("horizon") == horizon:
                rows[str(raw["ticker"])] = dict(raw)
    return rows


def _resolved(raw: Mapping[str, object] | None, basis: str) -> float | None:
    if raw is None:
        return None
    if basis == "price":
        return _number(raw.get("price_return")) if raw.get("status") == "resolved" else None
    return (
        _number(raw.get("total_return")) if raw.get("total_return_status") == "resolved" else None
    )


def _stats(
    members: Sequence[Row],
    forward: Mapping[str, Mapping[str, object]],
    *,
    basis: str,
    population_median: float,
) -> dict[str, float | int | None]:
    values = [
        value - 0.02 - population_median
        for member in members
        if (value := _resolved(forward.get(member.ticker), basis)) is not None
    ]
    return {
        "n": len(values),
        "median_excess": median(values) if values else None,
        "trap_rate": sum(value <= TRAP_THRESHOLD for value in values) / len(values)
        if values
        else None,
    }


def _quintiles(rows: Sequence[Row]) -> dict[str, int]:
    ordered = sorted(rows, key=lambda row: (row.er, row.ticker))
    return (
        {row.ticker: min(index * 5 // len(ordered) + 1, 5) for index, row in enumerate(ordered)}
        if ordered
        else {}
    )


def _close(expected: object, actual: object, path: str) -> None:
    if isinstance(expected, int | float) and not isinstance(expected, bool):
        if (
            not isinstance(actual, int | float)
            or isinstance(actual, bool)
            or not math.isclose(float(expected), float(actual), rel_tol=1e-12, abs_tol=1e-12)
        ):
            raise IndependentVerificationError(
                f"numeric mismatch at {path}: {expected} != {actual}"
            )
        return
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping) or set(expected) != set(actual):
            raise IndependentVerificationError(f"mapping mismatch at {path}")
        for key, value in expected.items():
            _close(value, actual[key], f"{path}.{key}")
        return
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            raise IndependentVerificationError(f"list mismatch at {path}")
        for index, value in enumerate(expected):
            _close(value, actual[index], f"{path}[{index}]")
        return
    if expected != actual:
        raise IndependentVerificationError(f"value mismatch at {path}: {expected!r} != {actual!r}")


def verify(*, result_path: Path, calibration_dir: Path, market_db: Path) -> dict[str, object]:
    artifact = yaml.safe_load(result_path.read_text(encoding="utf-8"))
    if not isinstance(artifact, Mapping) or not isinstance(
        artifact.get("verification_targets"), list
    ):
        raise IndependentVerificationError("result artifact lacks verification targets")
    expected_by_key = {
        (str(raw["asof"]), str(raw["horizon"])): raw
        for raw in artifact["verification_targets"]
        if isinstance(raw, Mapping)
    }
    conn = sqlite3.connect(f"file:{market_db}?mode=ro", uri=True)
    verified: list[dict[str, object]] = []
    try:
        for asof, horizon in TARGETS:
            panel = calibration_dir / f"panel-{asof.isoformat()}.csv"
            rows, population = _load_panel(panel, conn, asof)
            policies, capacity_only = _policies(rows)
            forward = _forward(calibration_dir / f"forward-{asof.isoformat()}.csv", horizon)
            population_medians = {
                basis: median(
                    value
                    for ticker in population
                    if (value := _resolved(forward.get(ticker), basis)) is not None
                )
                for basis in ("price", "total")
            }
            actual_policies = {
                name: {
                    "tickers": [row.ticker for row in members],
                    "facts": {
                        row.ticker: {
                            "minimum_lot_yen": row.minimum_lot,
                            "trading_value_p20_60d": row.p20,
                            "capacity_days_at_1pct_standard": row.capacity_days,
                        }
                        for row in members
                    },
                    "after_200bps": {
                        basis: _stats(
                            members,
                            forward,
                            basis=basis,
                            population_median=population_medians[basis],
                        )
                        for basis in ("price", "total")
                    },
                }
                for name, members in policies.items()
            }
            actual = {
                "asof": asof.isoformat(),
                "horizon": horizon,
                "population_median": population_medians,
                "policies": actual_policies,
                "capacity_only_quintiles": _quintiles(capacity_only),
            }
            expected = expected_by_key.get((asof.isoformat(), horizon))
            if expected is None:
                raise IndependentVerificationError(
                    f"target missing from artifact: {asof}/{horizon}"
                )
            _close(expected, actual, f"{asof}/{horizon}")
            verified.append(
                {
                    "asof": asof.isoformat(),
                    "horizon": horizon,
                    "policy_sizes": {name: len(rows) for name, rows in policies.items()},
                    "capacity_only_quintile_members": len(capacity_only),
                    "status": "matched",
                }
            )
    finally:
        conn.close()
    return {
        "schema_version": 1,
        "study": "capacity_native_universe",
        "verification": "independent_raw_csv_sqlite",
        "imports_capacity_evaluator": False,
        "result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
        "targets": verified,
        "status": "matched",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--calibration-dir", type=Path, default=CALIBRATION_DIR)
    parser.add_argument("--market-db", type=Path, default=MARKET_DB)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    payload = verify(
        result_path=args.result,
        calibration_dir=args.calibration_dir,
        market_db=args.market_db,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
