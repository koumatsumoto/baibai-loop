"""Replay the preregistered capacity-native universe policy comparison."""

from __future__ import annotations

import argparse
import csv
import hashlib
import math
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import fmean, median
from typing import Literal, TextIO

import yaml
from tools.experiments.measure_capacity_native_universe import (
    DEFAULT_CALIBRATION_DIR,
    DEFAULT_MARKET_DB,
    MAX_TICKER_SHARE,
    PRIMARY_NOTIONAL,
    Candidate,
    CapacityStudyError,
    _asof_from_panel,
    _bars_for_window,
    _capacity_eligible,
    _capacity_fact,
    _current_eligible,
    _is_true,
    _market_segments,
    _market_sessions,
    _optional_float,
    _optional_int,
    _panel_paths,
    _panel_rows,
    _rank,
    _rules_hash,
)

from baibai_engine.screening.calibration.forward import TOTAL_RETURN_STATUSES

Basis = Literal["price", "total"]
BASES: tuple[Basis, ...] = ("price", "total")
COSTS_BPS = (50, 200, 500)
PRIMARY_COST_BPS = 200
ER_HURDLE = 0.085
TRAP_THRESHOLD = -0.20
REQUIRED_METRICS = frozenset({"recommended_rank_top5", "recommended_rank_top10", "er_calibration"})
KNOWN_PRICE_STATUSES = frozenset(
    {
        "resolved",
        "unresolved_missing_entry",
        "unresolved_future_horizon",
        "unresolved_missing_exit",
        "unresolved_stale_exit",
    }
)
POLICY_SIZES = {
    "current_core_top20": 20,
    "capacity_core_top20": 20,
    "capacity_only_outside_current_top5": 5,
    "current_boundary_21_25": 5,
}
WINDOWS: Mapping[str, tuple[str, date, date]] = {
    "1y_design": ("1y", date(2020, 1, 1), date(2023, 7, 31)),
    "1y_time_holdout": ("1y", date(2023, 8, 1), date(2025, 6, 30)),
    "3y_all": ("3y", date(2020, 1, 1), date(2023, 6, 30)),
    "3y_postcovid": ("3y", date(2021, 7, 1), date(2023, 6, 30)),
    "5y_all": ("5y", date(2019, 11, 29), date(2021, 7, 30)),
}
VERIFICATION_COHORTS: tuple[tuple[date, str], ...] = (
    (date(2023, 6, 30), "1y"),
    (date(2022, 6, 30), "3y"),
    (date(2020, 6, 30), "5y"),
)


class CapacityReplayError(CapacityStudyError):
    """Raised when the replay cannot honor its preregistered contract."""


@dataclass(frozen=True, slots=True)
class ForwardOutcome:
    ticker: str
    horizon: str
    status: str
    price_return: float | None
    stale_price: bool
    entry_date: str | None
    exit_date: str | None
    adjustment_factor_coverage: str
    total_return: float | None
    total_return_status: str


@dataclass(frozen=True, slots=True)
class Cohort:
    asof: date
    candidates: tuple[Candidate, ...]
    population_tickers: frozenset[str]
    forward: Mapping[tuple[str, str], ForwardOutcome]
    policies: Mapping[str, tuple[Candidate, ...]]
    capacity_only: tuple[Candidate, ...]


@dataclass(frozen=True, slots=True)
class ExcessObservation:
    asof: str
    ticker: str
    value: float


def _read_integrity(path: Path) -> tuple[str, frozenset[tuple[str, str]]]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise CapacityReplayError("calibration evaluation is not a mapping")
    rules_hash = payload.get("screening_rules_hash")
    if not isinstance(rules_hash, str) or not rules_hash:
        raise CapacityReplayError("calibration evaluation lacks screening_rules_hash")
    rows = payload.get("cohort_integrity")
    if not isinstance(rows, list):
        raise CapacityReplayError("calibration evaluation lacks cohort_integrity")
    eligible: set[tuple[str, str]] = set()
    seen: set[tuple[str, str]] = set()
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise CapacityReplayError("cohort integrity row is not a mapping")
        asof = raw.get("asof")
        horizon = raw.get("horizon")
        statuses = raw.get("metric_statuses")
        if not isinstance(asof, str) or not isinstance(horizon, str):
            raise CapacityReplayError("cohort integrity identity is invalid")
        key = (asof, horizon)
        if key in seen:
            raise CapacityReplayError(f"duplicate cohort integrity identity: {key}")
        seen.add(key)
        if not isinstance(statuses, Mapping) or set(statuses) != REQUIRED_METRICS:
            raise CapacityReplayError(f"cohort integrity metric scope is invalid: {key}")
        if raw.get("integrity_status") == "eligible" and all(
            statuses[name] == "eligible" for name in REQUIRED_METRICS
        ):
            eligible.add(key)
    return rules_hash, frozenset(eligible)


def _read_forward(
    path: Path, wanted_horizons: frozenset[str]
) -> dict[tuple[str, str], ForwardOutcome]:
    result: dict[tuple[str, str], ForwardOutcome] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            horizon = str(raw.get("horizon") or "")
            if horizon not in wanted_horizons:
                continue
            ticker = str(raw.get("ticker") or "")
            key = (ticker, horizon)
            if not ticker or key in result:
                raise CapacityReplayError(f"invalid or duplicate forward identity in {path}: {key}")
            status = str(raw.get("status") or "")
            total_status = str(raw.get("total_return_status") or "")
            if status not in KNOWN_PRICE_STATUSES:
                raise CapacityReplayError(f"unknown forward status in {path}: {status}")
            if total_status not in TOTAL_RETURN_STATUSES:
                raise CapacityReplayError(f"unknown total-return status in {path}: {total_status}")
            price = _optional_float(raw.get("price_return"))
            total = _optional_float(raw.get("total_return"))
            if (status == "resolved") != (price is not None):
                raise CapacityReplayError(f"price status/value mismatch in {path}: {key}")
            if (total_status == "resolved") != (total is not None):
                raise CapacityReplayError(f"total status/value mismatch in {path}: {key}")
            result[key] = ForwardOutcome(
                ticker=ticker,
                horizon=horizon,
                status=status,
                price_return=price,
                stale_price=_is_true(raw.get("stale_price")),
                entry_date=raw.get("entry_date") or None,
                exit_date=raw.get("exit_date") or None,
                adjustment_factor_coverage=str(raw.get("adjustment_factor_coverage") or ""),
                total_return=total,
                total_return_status=total_status,
            )
    return result


def _horizons_for(asof: date) -> frozenset[str]:
    return frozenset(horizon for horizon, start, end in WINDOWS.values() if start <= asof <= end)


def _build_cohorts(calibration_dir: Path, market_db: Path) -> tuple[str, list[Cohort]]:
    panel_paths = _panel_paths(calibration_dir)
    rules_hash = _rules_hash(calibration_dir, panel_paths)
    conn = sqlite3.connect(f"file:{market_db}?mode=ro", uri=True)
    cohorts: list[Cohort] = []
    try:
        sessions = _market_sessions(conn)
        session_index = {session: index for index, session in enumerate(sessions)}
        for path in panel_paths:
            asof = _asof_from_panel(path)
            wanted_horizons = _horizons_for(asof)
            if not wanted_horizons:
                continue
            index = session_index.get(asof)
            if index is None or index < 59:
                raise CapacityReplayError(f"cohort lacks 60 market sessions: {asof}")
            session_dates = sessions[index - 59 : index + 1]
            raw_rows = _panel_rows(path)
            screened = [
                row
                for row in raw_rows
                if _is_true(row.get("pass_screen"))
                and row.get("population_coverage_status") == "evaluated"
                and _optional_float(row.get("er_annual")) is not None
            ]
            wanted = frozenset(str(row["ticker"]) for row in screened)
            bars = _bars_for_window(conn, session_dates[0], session_dates[-1], wanted)
            segments = _market_segments(conn, asof)
            candidates: list[Candidate] = []
            for raw in screened:
                ticker = str(raw["ticker"])
                candidates.append(
                    Candidate(
                        ticker=ticker,
                        sector=str(raw.get("sector_33") or ""),
                        market=segments.get(ticker),
                        er_annual=float(raw["er_annual"]),
                        er_carry_annual=_optional_float(raw.get("er_carry_annual")),
                        er_reversion_annual=_optional_float(raw.get("er_reversion_annual")),
                        market_cap_oku=_optional_float(raw.get("market_cap_oku")),
                        avg_turnover_oku=_optional_float(raw.get("avg_turnover_oku")),
                        listing_span_days=_optional_int(raw.get("listing_span_days")),
                        fact=_capacity_fact(session_dates=session_dates, bars=bars.get(ticker, {})),
                    )
                )
            current = _rank(candidate for candidate in candidates if _current_eligible(candidate))
            capacity = _rank(
                candidate
                for candidate in candidates
                if _capacity_eligible(candidate, PRIMARY_NOTIONAL)
            )
            current_set = {candidate.ticker for candidate in current}
            capacity_only = tuple(
                candidate for candidate in capacity if candidate.ticker not in current_set
            )
            capacity_top20 = tuple(capacity[:20])
            policies = {
                "current_core_top20": tuple(current[:20]),
                "capacity_core_top20": capacity_top20,
                "capacity_only_outside_current_top5": tuple(
                    candidate for candidate in capacity_top20 if candidate.ticker not in current_set
                )[:5],
                "current_boundary_21_25": tuple(current[20:25]),
            }
            forward_path = calibration_dir / f"forward-{asof.isoformat()}.csv"
            forward = _read_forward(forward_path, wanted_horizons)
            panel_tickers = {str(row["ticker"]) for row in raw_rows}
            forward_tickers = {ticker for ticker, _ in forward if ticker != "1306"}
            if panel_tickers != forward_tickers:
                missing = sorted(panel_tickers - forward_tickers)[:5]
                extra = sorted(forward_tickers - panel_tickers)[:5]
                raise CapacityReplayError(
                    f"panel/forward partition mismatch at {asof}: missing={missing}, extra={extra}"
                )
            cohorts.append(
                Cohort(
                    asof=asof,
                    candidates=tuple(candidates),
                    population_tickers=frozenset(
                        str(row["ticker"]) for row in raw_rows if _is_true(row.get("in_population"))
                    ),
                    forward=forward,
                    policies=policies,
                    capacity_only=capacity_only,
                )
            )
    finally:
        conn.close()
    return rules_hash, cohorts


def _return(outcome: ForwardOutcome | None, basis: Basis) -> float | None:
    if outcome is None:
        return None
    return outcome.price_return if basis == "price" else outcome.total_return


def _population_median(cohort: Cohort, horizon: str, basis: Basis) -> float | None:
    values = [
        value
        for ticker in cohort.population_tickers
        if (value := _return(cohort.forward.get((ticker, horizon)), basis)) is not None
    ]
    return median(values) if values else None


def _observations(
    cohorts: Sequence[Cohort],
    *,
    horizon: str,
    basis: Basis,
    cost_bps: int,
    policy: str,
) -> list[ExcessObservation]:
    observations: list[ExcessObservation] = []
    cost = cost_bps / 10_000
    for cohort in cohorts:
        population_median = _population_median(cohort, horizon, basis)
        if population_median is None:
            continue
        for candidate in cohort.policies[policy]:
            value = _return(cohort.forward.get((candidate.ticker, horizon)), basis)
            if value is not None:
                observations.append(
                    ExcessObservation(
                        asof=cohort.asof.isoformat(),
                        ticker=candidate.ticker,
                        value=value - cost - population_median,
                    )
                )
    return observations


def _stats(observations: Sequence[ExcessObservation]) -> dict[str, float | int | None]:
    values = [observation.value for observation in observations]
    return {
        "n": len(values),
        "median_excess": median(values) if values else None,
        "trap_rate": (
            sum(value <= TRAP_THRESHOLD for value in values) / len(values) if values else None
        ),
    }


def _cohort_weighted_stats(
    observations: Sequence[ExcessObservation],
) -> dict[str, float | int | None]:
    by_asof: dict[str, list[float]] = defaultdict(list)
    for observation in observations:
        by_asof[observation.asof].append(observation.value)
    medians = [median(values) for values in by_asof.values()]
    traps = [
        sum(value <= TRAP_THRESHOLD for value in values) / len(values)
        for values in by_asof.values()
    ]
    return {
        "cohorts": len(by_asof),
        "median_of_cohort_medians": median(medians) if medians else None,
        "mean_cohort_trap_rate": fmean(traps) if traps else None,
    }


def _resolution(cohorts: Sequence[Cohort], horizon: str, policy: str) -> dict[str, object]:
    outcomes = [
        cohort.forward.get((candidate.ticker, horizon))
        for cohort in cohorts
        for candidate in cohort.policies[policy]
    ]
    membership = len(outcomes)
    price_resolved = sum(
        outcome is not None and outcome.price_return is not None for outcome in outcomes
    )
    total_resolved = sum(
        outcome is not None and outcome.total_return is not None for outcome in outcomes
    )
    price_statuses = Counter(
        outcome.status if outcome is not None else "missing_row" for outcome in outcomes
    )
    total_statuses = Counter(
        outcome.total_return_status if outcome is not None else "missing_row"
        for outcome in outcomes
    )
    adverse = sum(
        outcome is None
        or outcome.status
        in {"unresolved_missing_entry", "unresolved_missing_exit", "unresolved_stale_exit"}
        for outcome in outcomes
    )
    return {
        "membership": membership,
        "price_resolved": price_resolved,
        "price_resolved_share": price_resolved / membership if membership else None,
        "total_resolved": total_resolved,
        "total_to_price_share": total_resolved / price_resolved if price_resolved else None,
        "adverse_resolution_rate": adverse / membership if membership else None,
        "price_statuses": dict(sorted(price_statuses.items())),
        "total_statuses": dict(sorted(total_statuses.items())),
    }


def _cohort_delta(
    cohorts: Sequence[Cohort], *, horizon: str, basis: Basis, cost_bps: int
) -> dict[str, float | int | None]:
    incremental = _observations(
        cohorts,
        horizon=horizon,
        basis=basis,
        cost_bps=cost_bps,
        policy="capacity_only_outside_current_top5",
    )
    boundary = _observations(
        cohorts,
        horizon=horizon,
        basis=basis,
        cost_bps=cost_bps,
        policy="current_boundary_21_25",
    )
    left: dict[str, list[float]] = defaultdict(list)
    right: dict[str, list[float]] = defaultdict(list)
    for observation in incremental:
        left[observation.asof].append(observation.value)
    for observation in boundary:
        right[observation.asof].append(observation.value)
    deltas = [median(left[asof]) - median(right[asof]) for asof in sorted(left.keys() & right)]
    return {
        "matched_cohorts": len(deltas),
        "median_delta": median(deltas) if deltas else None,
        "positive_share": sum(value > 0 for value in deltas) / len(deltas) if deltas else None,
    }


def _quintile_members(candidates: Sequence[Candidate]) -> dict[str, int]:
    ordered = sorted(candidates, key=lambda item: (item.er_annual, item.ticker))
    size = len(ordered)
    return (
        {candidate.ticker: min(index * 5 // size + 1, 5) for index, candidate in enumerate(ordered)}
        if size
        else {}
    )


def _transportability(
    cohorts: Sequence[Cohort], *, horizon: str, basis: Basis, cost_bps: int
) -> dict[str, object]:
    by_group: dict[str, list[ExcessObservation]] = {"q1": [], "q5": [], "hurdle": []}
    cost = cost_bps / 10_000
    for cohort in cohorts:
        population_median = _population_median(cohort, horizon, basis)
        if population_median is None:
            continue
        quintiles = _quintile_members(cohort.capacity_only)
        for candidate in cohort.capacity_only:
            value = _return(cohort.forward.get((candidate.ticker, horizon)), basis)
            if value is None:
                continue
            observation = ExcessObservation(
                asof=cohort.asof.isoformat(),
                ticker=candidate.ticker,
                value=value - cost - population_median,
            )
            quintile = quintiles.get(candidate.ticker)
            if quintile == 1:
                by_group["q1"].append(observation)
            if quintile == 5:
                by_group["q5"].append(observation)
            if candidate.er_annual >= ER_HURDLE:
                by_group["hurdle"].append(observation)
    groups = {name: _stats(values) for name, values in by_group.items()}
    q1_median = groups["q1"]["median_excess"]
    q5_median = groups["q5"]["median_excess"]
    q1_trap = groups["q1"]["trap_rate"]
    q5_trap = groups["q5"]["trap_rate"]
    hurdle = groups["hurdle"]["median_excess"]
    return {
        "groups": groups,
        "q5_minus_q1_median_excess": _difference(q5_median, q1_median),
        "q5_minus_q1_trap_rate": _difference(q5_trap, q1_trap),
        "hurdle_median_excess": hurdle,
    }


def _difference(left: object, right: object) -> float | None:
    left_number = _number(left)
    right_number = _number(right)
    if left_number is None or right_number is None:
        return None
    return left_number - right_number


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _greater_than(value: object, threshold: float) -> bool:
    number = _number(value)
    return number is not None and number > threshold


def _at_least(value: object, threshold: float) -> bool:
    number = _number(value)
    return number is not None and number >= threshold


def _at_most(value: object, threshold: float) -> bool:
    number = _number(value)
    return number is not None and number <= threshold


def _less_than(value: object, threshold: float) -> bool:
    number = _number(value)
    return number is not None and number < threshold


def _passes_effect(
    *,
    policy: Mapping[str, Mapping[str, object]],
    cohort_delta: Mapping[str, object],
    transport: Mapping[str, object],
    resolution: Mapping[str, Mapping[str, object]],
) -> tuple[bool, dict[str, bool]]:
    current = policy["current_core_top20"]
    capacity = policy["capacity_core_top20"]
    incremental = policy["capacity_only_outside_current_top5"]
    boundary = policy["current_boundary_21_25"]
    checks = {
        "capacity_median_noninferiority": _at_least(
            _difference(capacity.get("median_excess"), current.get("median_excess")),
            -0.02,
        ),
        "capacity_trap_noninferiority": _at_most(
            _difference(capacity.get("trap_rate"), current.get("trap_rate")), 0.02
        ),
        "incremental_median_positive": _greater_than(incremental.get("median_excess"), 0),
        "boundary_delta_nonnegative": _at_least(cohort_delta.get("median_delta"), 0),
        "boundary_delta_positive_share": _at_least(cohort_delta.get("positive_share"), 0.55),
        "incremental_trap_noninferiority": _at_most(
            _difference(incremental.get("trap_rate"), boundary.get("trap_rate")), 0.02
        ),
        "incremental_resolution_noninferiority": _at_most(
            _difference(
                resolution["capacity_only_outside_current_top5"].get("adverse_resolution_rate"),
                resolution["current_boundary_21_25"].get("adverse_resolution_rate"),
            ),
            0.02,
        ),
        "transport_q5_above_q1": _greater_than(transport.get("q5_minus_q1_median_excess"), 0),
        "transport_hurdle_positive": _greater_than(transport.get("hurdle_median_excess"), 0),
        "transport_trap_noninferiority": _at_most(transport.get("q5_minus_q1_trap_rate"), 0.02),
    }
    return all(checks.values()), checks


def _window_verdict(
    *,
    sufficient: bool,
    effects: Mapping[Basis, tuple[bool, Mapping[str, bool]]],
    stress_effects: Mapping[Basis, tuple[bool, Mapping[str, bool]]],
    policy_stats: Mapping[Basis, Mapping[str, Mapping[str, object]]],
    cohort_deltas: Mapping[Basis, Mapping[str, object]],
    transport: Mapping[Basis, Mapping[str, object]],
) -> str:
    if not sufficient:
        return "insufficient"
    if all(result[0] for result in effects.values()):
        if all(result[0] for result in stress_effects.values()):
            return "adoption_candidate"
        return "inconclusive"
    noninferiority_breach = any(
        not checks["capacity_median_noninferiority"] or not checks["capacity_trap_noninferiority"]
        for _, checks in effects.values()
    )
    triple_incremental_failure = all(
        _at_most(
            policy_stats[basis]["capacity_only_outside_current_top5"].get("median_excess"),
            0,
        )
        and _less_than(cohort_deltas[basis].get("median_delta"), 0)
        and _less_than(cohort_deltas[basis].get("positive_share"), 0.55)
        for basis in BASES
    )
    transport_reversal = all(
        _at_most(transport[basis].get("q5_minus_q1_median_excess"), 0) for basis in BASES
    )
    if noninferiority_breach or triple_incremental_failure or transport_reversal:
        return "negative"
    return "inconclusive"


def _window_payload(
    *,
    name: str,
    all_cohorts: Sequence[Cohort],
    eligible_keys: frozenset[tuple[str, str]],
) -> dict[str, object]:
    horizon, start, end = WINDOWS[name]
    matured = [cohort for cohort in all_cohorts if start <= cohort.asof <= end]
    eligible = [cohort for cohort in matured if (cohort.asof.isoformat(), horizon) in eligible_keys]
    availability = {
        policy: (
            sum(len(cohort.policies[policy]) == size for cohort in matured) / len(matured)
            if matured
            else 0.0
        )
        for policy, size in POLICY_SIZES.items()
    }
    incremental_tickers = [
        candidate.ticker
        for cohort in eligible
        for candidate in cohort.policies["capacity_only_outside_current_top5"]
    ]
    ticker_counts = Counter(incremental_tickers)
    max_share = (
        max(ticker_counts.values()) / len(incremental_tickers) if incremental_tickers else None
    )
    resolution = {policy: _resolution(eligible, horizon, policy) for policy in POLICY_SIZES}
    unique_floor = 25 if name.startswith("1y") else 15 if name.startswith("3y") else 8
    sufficiency_checks = {
        "eligible_cohorts": len(eligible) >= 8,
        "capacity_top20_availability": availability["capacity_core_top20"] >= 0.90,
        "incremental_top5_availability": (
            availability["capacity_only_outside_current_top5"] >= 0.75
        ),
        "current_top20_availability": availability["current_core_top20"] >= 0.90,
        "boundary_availability": availability["current_boundary_21_25"] >= 0.90,
        "unique_tickers": len(ticker_counts) >= unique_floor,
        "max_ticker_share": max_share is not None and max_share <= MAX_TICKER_SHARE,
        "price_resolution": all(
            _at_least(value.get("price_resolved_share"), 0.75) for value in resolution.values()
        ),
        "total_resolution": all(
            _at_least(value.get("total_to_price_share"), 0.75) for value in resolution.values()
        ),
    }
    sufficient = all(sufficiency_checks.values())
    all_costs: dict[str, object] = {}
    primary_policy_stats: dict[Basis, Mapping[str, Mapping[str, object]]] = {}
    primary_deltas: dict[Basis, Mapping[str, object]] = {}
    primary_transport: dict[Basis, Mapping[str, object]] = {}
    effects: dict[Basis, tuple[bool, Mapping[str, bool]]] = {}
    stress_effects: dict[Basis, tuple[bool, Mapping[str, bool]]] = {}
    for cost_bps in COSTS_BPS:
        by_basis: dict[str, object] = {}
        for basis in BASES:
            policies: dict[str, Mapping[str, object]] = {}
            cohort_weighted: dict[str, Mapping[str, object]] = {}
            for policy in POLICY_SIZES:
                observations = _observations(
                    eligible,
                    horizon=horizon,
                    basis=basis,
                    cost_bps=cost_bps,
                    policy=policy,
                )
                policies[policy] = _stats(observations)
                cohort_weighted[policy] = _cohort_weighted_stats(observations)
            delta = _cohort_delta(eligible, horizon=horizon, basis=basis, cost_bps=cost_bps)
            transport = _transportability(eligible, horizon=horizon, basis=basis, cost_bps=cost_bps)
            passed = _passes_effect(
                policy=policies,
                cohort_delta=delta,
                transport=transport,
                resolution=resolution,
            )
            by_basis[basis] = {
                "ticker_asof_equal": policies,
                "cohort_equal": cohort_weighted,
                "incremental_minus_boundary": delta,
                "transportability": transport,
                "effect_checks": passed[1],
                "effect_pass": passed[0],
            }
            if cost_bps == PRIMARY_COST_BPS:
                primary_policy_stats[basis] = policies
                primary_deltas[basis] = delta
                primary_transport[basis] = transport
                effects[basis] = passed
            if cost_bps == 500:
                stress_effects[basis] = passed
        all_costs[str(cost_bps)] = by_basis
    verdict = _window_verdict(
        sufficient=sufficient,
        effects=effects,
        stress_effects=stress_effects,
        policy_stats=primary_policy_stats,
        cohort_deltas=primary_deltas,
        transport=primary_transport,
    )
    return {
        "horizon": horizon,
        "asof_start": start.isoformat(),
        "asof_end": end.isoformat(),
        "matured_cohorts": len(matured),
        "eligible_cohorts": len(eligible),
        "availability": availability,
        "incremental_unique_tickers": len(ticker_counts),
        "incremental_max_ticker_share": max_share,
        "resolution": resolution,
        "sufficiency_checks": sufficiency_checks,
        "sufficient": sufficient,
        "cost_bps": all_costs,
        "verdict": verdict,
    }


def _overall_verdict(windows: Mapping[str, Mapping[str, object]]) -> str:
    sufficient = [value for value in windows.values() if value.get("verdict") != "insufficient"]
    if not sufficient:
        return "insufficient"
    core_names = ("1y_design", "1y_time_holdout", "3y_all", "3y_postcovid")
    core_verdicts = [str(windows[name]["verdict"]) for name in core_names]
    five_year = windows["5y_all"]
    five_primary = five_year["cost_bps"]
    if not isinstance(five_primary, Mapping):  # pragma: no cover - local invariant
        raise AssertionError("5y cost payload must be a mapping")
    primary = five_primary[str(PRIMARY_COST_BPS)]
    if not isinstance(primary, Mapping):  # pragma: no cover - local invariant
        raise AssertionError("5y primary cost payload must be a mapping")
    five_direction = all(
        isinstance(primary[basis], Mapping)
        and bool(primary[basis].get("effect_checks", {}).get("capacity_median_noninferiority"))
        and bool(primary[basis].get("effect_checks", {}).get("capacity_trap_noninferiority"))
        and bool(primary[basis].get("effect_checks", {}).get("incremental_median_positive"))
        and bool(primary[basis].get("effect_checks", {}).get("incremental_trap_noninferiority"))
        and bool(primary[basis].get("effect_checks", {}).get("transport_q5_above_q1"))
        and bool(primary[basis].get("effect_checks", {}).get("transport_trap_noninferiority"))
        for basis in BASES
    )
    if all(verdict == "adoption_candidate" for verdict in core_verdicts) and five_direction:
        return "adoption_candidate"
    if "adoption_candidate" in core_verdicts and "negative" in core_verdicts:
        return "inconclusive"
    if all(verdict == "negative" for verdict in core_verdicts):
        return "negative"
    for check in (
        "capacity_median_noninferiority",
        "capacity_trap_noninferiority",
        "incremental_median_positive",
        "boundary_delta_nonnegative",
        "boundary_delta_positive_share",
        "transport_q5_above_q1",
    ):
        values = []
        for name in ("1y_design", "1y_time_holdout", "3y_all"):
            cost = windows[name]["cost_bps"]
            assert isinstance(cost, Mapping)
            primary_cost = cost[str(PRIMARY_COST_BPS)]
            assert isinstance(primary_cost, Mapping)
            values.append(
                all(
                    isinstance(primary_cost[basis], Mapping)
                    and primary_cost[basis].get("effect_checks", {}).get(check) is False
                    for basis in BASES
                )
            )
        if all(values):
            return "negative"
    return "inconclusive"


def _selected_rows(cohorts: Sequence[Cohort]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for cohort in cohorts:
        for horizon in sorted(_horizons_for(cohort.asof)):
            population = {basis: _population_median(cohort, horizon, basis) for basis in BASES}
            for policy, candidates in cohort.policies.items():
                for rank, candidate in enumerate(candidates, start=1):
                    outcome = cohort.forward.get((candidate.ticker, horizon))
                    rows.append(
                        {
                            "asof": cohort.asof.isoformat(),
                            "horizon": horizon,
                            "policy": policy,
                            "policy_rank": rank,
                            "ticker": candidate.ticker,
                            "er_annual": candidate.er_annual,
                            "market_cap_oku": candidate.market_cap_oku,
                            "minimum_lot_yen": candidate.fact.minimum_lot_yen,
                            "trading_value_p20_60d": candidate.fact.trading_value_p20_60d,
                            "capacity_days_at_1pct_standard": (
                                candidate.fact.capacity_days_by_notional[PRIMARY_NOTIONAL]
                            ),
                            "price_status": outcome.status if outcome else "missing_row",
                            "price_return": outcome.price_return if outcome else None,
                            "total_status": outcome.total_return_status
                            if outcome
                            else "missing_row",
                            "total_return": outcome.total_return if outcome else None,
                            "population_median": population,
                            "excess_after_200bps": {
                                basis: _costed_excess(outcome, basis, population[basis], 0.02)
                                for basis in BASES
                            },
                        }
                    )
    return rows


def _costed_excess(
    outcome: ForwardOutcome | None,
    basis: Basis,
    population_median: float | None,
    cost: float,
) -> float | None:
    value = _return(outcome, basis)
    if value is None or population_median is None:
        return None
    return value - cost - population_median


def _verification_targets(cohorts: Sequence[Cohort]) -> list[dict[str, object]]:
    by_identity = {cohort.asof: cohort for cohort in cohorts}
    targets: list[dict[str, object]] = []
    for asof, horizon in VERIFICATION_COHORTS:
        cohort = by_identity.get(asof)
        if cohort is None:
            raise CapacityReplayError(f"verification cohort is missing: {asof}")
        population = {basis: _population_median(cohort, horizon, basis) for basis in BASES}
        policies: dict[str, object] = {}
        for policy, candidates in cohort.policies.items():
            policies[policy] = {
                "tickers": [candidate.ticker for candidate in candidates],
                "facts": {
                    candidate.ticker: {
                        "minimum_lot_yen": candidate.fact.minimum_lot_yen,
                        "trading_value_p20_60d": candidate.fact.trading_value_p20_60d,
                        "capacity_days_at_1pct_standard": (
                            candidate.fact.capacity_days_by_notional[PRIMARY_NOTIONAL]
                        ),
                    }
                    for candidate in candidates
                },
                "after_200bps": {
                    basis: _stats(
                        _observations(
                            [cohort],
                            horizon=horizon,
                            basis=basis,
                            cost_bps=PRIMARY_COST_BPS,
                            policy=policy,
                        )
                    )
                    for basis in BASES
                },
            }
        targets.append(
            {
                "asof": asof.isoformat(),
                "horizon": horizon,
                "population_median": population,
                "policies": policies,
                "capacity_only_quintiles": _quintile_members(cohort.capacity_only),
            }
        )
    return targets


def replay(*, calibration_dir: Path, market_db: Path, evaluation: Path) -> dict[str, object]:
    evaluation_hash, eligible = _read_integrity(evaluation)
    panel_hash, cohorts = _build_cohorts(calibration_dir, market_db)
    if panel_hash != evaluation_hash:
        raise CapacityReplayError(
            f"evaluation/panel rules hash mismatch: {evaluation_hash} != {panel_hash}"
        )
    windows = {
        name: _window_payload(name=name, all_cohorts=cohorts, eligible_keys=eligible)
        for name in WINDOWS
    }
    return {
        "schema_version": 1,
        "study": "capacity_native_universe",
        "stage": "forward_replay",
        "preregistration_commit": "09a1d5de",
        "screening_rules_hash": panel_hash,
        "evaluation_sha256": hashlib.sha256(evaluation.read_bytes()).hexdigest(),
        "contract": {
            "primary_notional": PRIMARY_NOTIONAL,
            "bases": list(BASES),
            "cost_bps": list(COSTS_BPS),
            "primary_cost_bps": PRIMARY_COST_BPS,
            "trap_threshold": TRAP_THRESHOLD,
            "er_hurdle": ER_HURDLE,
        },
        "windows": windows,
        "overall_verdict": _overall_verdict(windows),
        "verification_targets": _verification_targets(cohorts),
        "selected_rows": _selected_rows(cohorts),
        "production_diff": {
            "er": 0,
            "fv": 0,
            "rank": 0,
            "gate": 0,
            "selection_payload": 0,
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration-dir", type=Path, default=DEFAULT_CALIBRATION_DIR)
    parser.add_argument("--market-db", type=Path, default=DEFAULT_MARKET_DB)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    args = _parser().parse_args(argv)
    payload = replay(
        calibration_dir=args.calibration_dir,
        market_db=args.market_db,
        evaluation=args.evaluation,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    rendered = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    args.out.write_text(rendered, encoding="utf-8")
    if stdout is not None:
        stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
