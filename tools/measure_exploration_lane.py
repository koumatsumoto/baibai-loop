"""Replay the fixed exploration policy against the calibration store.

Compares the preregistered policy (`reports/2026-08-08-exploration-lane-preregistration.md`)
against the simple top-21 comparator over fixed windows, both return bases, both
missing-exit imputations, and both weightings, then resolves the four-word verdict
by the preregistered precedence.

This is a decision artifact, not a production path. Cohort windows overlap and the
feature was chosen after seeing the parent axis result, so no significance is
claimed: the output is read as effect size plus cohort win rate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import median
from typing import Literal, TextIO

import yaml

from baibai_engine.screening.calibration.authority import (
    PRODUCTION_REQUIRED_METRICS,
    CohortIntegrity,
    EvaluationScope,
    decide_authority,
)
from baibai_engine.screening.calibration.evaluation import MIN_AXIS_SAMPLE
from baibai_engine.screening.calibration.forward import ForwardReturnRow
from baibai_engine.screening.calibration.panel import PanelRow
from baibai_engine.screening.calibration.store import read_forward, read_panel, read_panel_meta
from baibai_engine.screening.selection.exploration import (
    EXPLORATION_LANE,
    POLICY_VERSION,
    ExplorationCandidate,
    ExplorationSelection,
    select_exploration,
)

DEFAULT_CALIBRATION_DIR = Path("data/screening/calibration")
COMPARATOR_RANK = 21

type Basis = Literal["price", "total"]
type Imputation = Literal["reported", "total_loss", "neutral"]
type Weighting = Literal["cohort", "ticker"]

BASES: tuple[Basis, ...] = ("price", "total")
IMPUTATIONS: tuple[Imputation, ...] = ("reported", "total_loss", "neutral")
WEIGHTINGS: tuple[Weighting, ...] = ("cohort", "ticker")

# Preregistered windows. `5y_aggregate` deliberately reuses the `3y_design`
# as-of set: those are the only as-ofs a 5y horizon resolves for, so the two are
# nested rather than independent and the report says so.
WINDOWS: Mapping[str, tuple[str, str, str]] = {
    "1y_design": ("1y", "2019-11-29", "2022-12-30"),
    "1y_holdout": ("1y", "2024-01-31", "2025-06-30"),
    "3y_design": ("3y", "2019-11-29", "2021-07-30"),
    "3y_holdout": ("3y", "2022-01-31", "2023-07-31"),
    "5y_aggregate": ("5y", "2019-11-29", "2021-07-30"),
}

AUTHORITY_ASOFS: tuple[str, ...] = (
    "2020-01-31",
    "2020-05-29",
    "2021-01-29",
    "2021-05-31",
)
AUTHORITY_METRIC = "normalized_per_3fy_exploration"

# Preregistered floors. Changing one of these after reading an outcome would make
# the verdict a choice rather than a measurement.
MIN_CANDIDATE_AVAILABILITY = 0.75
MIN_PRICE_RESOLUTION = 0.75
MIN_TOTAL_RESOLUTION = 0.75
MIN_PAIRS = 8
MIN_CHANGED_PAIRS = 8
MIN_UNIQUE_TICKERS = 8
MIN_PAIR_DELTA_MEDIAN = 0.03
MIN_POSITIVE_SHARE = 0.60
TRAP_EXCESS_THRESHOLD = -0.20

# Price statuses whose names left the market inside the window. They carry no exit
# value, so the pair is recomputed with each end of the plausible range rather than
# dropped.
IMPUTABLE_PRICE_STATUSES = frozenset({"unresolved_missing_exit", "unresolved_stale_exit"})


class ExplorationMeasurementError(RuntimeError):
    """The calibration store cannot support a truthful comparison."""


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateObservation:
    """One selected name's forward outcome on one basis."""

    ticker: str
    excess: float | None
    imputable: bool
    status: str


@dataclass(frozen=True, slots=True, kw_only=True)
class CohortPair:
    asof: str
    horizon: str
    basis: Basis
    exploration: CandidateObservation
    comparator: CandidateObservation
    population_n: int
    population_median: float
    same_ticker: bool

    def excesses(self, imputation: Imputation) -> tuple[float, float] | None:
        left = _imputed(self.exploration, imputation, self.population_median)
        right = _imputed(self.comparator, imputation, self.population_median)
        if left is None or right is None:
            return None
        return left, right


@dataclass(frozen=True, slots=True, kw_only=True)
class CohortMembership:
    asof: str
    selection: ExplorationSelection
    comparator_ticker: str | None


def _imputed(
    observation: CandidateObservation, imputation: Imputation, population_median: float
) -> float | None:
    if observation.excess is not None:
        return observation.excess
    if imputation == "reported" or not observation.imputable:
        return None
    if imputation == "total_loss":
        return -1.0 - population_median
    return 0.0


def _panel_candidates(panel: Sequence[PanelRow]) -> list[ExplorationCandidate]:
    return [
        ExplorationCandidate(
            ticker=row.ticker,
            full_rank=row.selection_rank,
            in_population=row.in_population,
            normalized_per_3fy=row.normalized_per_3fy,
        )
        for row in panel
    ]


def _basis_returns(
    forward_rows: Sequence[ForwardReturnRow], *, horizon: str, basis: Basis
) -> tuple[dict[str, float], dict[str, str], dict[str, str]]:
    """Resolved returns, the basis status, and the price status behind it."""
    returns: dict[str, float] = {}
    basis_status: dict[str, str] = {}
    price_status: dict[str, str] = {}
    for row in forward_rows:
        if row.horizon != horizon:
            continue
        price_status[row.ticker] = row.status
        if basis == "price":
            basis_status[row.ticker] = row.status
            if row.status == "resolved" and row.price_return is not None:
                returns[row.ticker] = row.price_return
            continue
        basis_status[row.ticker] = row.total_return_status
        if row.total_return_status == "resolved" and row.total_return is not None:
            returns[row.ticker] = row.total_return
    return returns, basis_status, price_status


def _observation(
    ticker: str,
    *,
    returns: Mapping[str, float],
    basis_status: Mapping[str, str],
    price_status: Mapping[str, str],
    population_median: float,
) -> CandidateObservation:
    value = returns.get(ticker)
    if value is not None:
        return CandidateObservation(
            ticker=ticker,
            excess=value - population_median,
            imputable=False,
            status="resolved",
        )
    status = basis_status.get(ticker, "missing_forward_row")
    imputable = price_status.get(ticker, "") in IMPUTABLE_PRICE_STATUSES
    return CandidateObservation(ticker=ticker, excess=None, imputable=imputable, status=status)


def _integrity_failures(
    tickers: Sequence[str],
    *,
    price_status: Mapping[str, str],
    priced_at_asof: frozenset[str],
) -> list[str]:
    """Statuses that must block rather than be imputed or silently dropped."""
    failures: list[str] = []
    for ticker in tickers:
        status = price_status.get(ticker)
        if status is None:
            failures.append(f"{ticker}:missing_forward_row")
            continue
        if status == "resolved" or status in IMPUTABLE_PRICE_STATUSES:
            continue
        if status == "unresolved_missing_entry":
            # A name the panel priced at as-of was tradeable then; losing its entry
            # is a dropped observation, not a name that was absent from the market.
            failures.append(
                f"{ticker}:entry_price_gap" if ticker in priced_at_asof else f"{ticker}:{status}"
            )
            continue
        failures.append(f"{ticker}:{status}")
    return failures


@dataclass(frozen=True, slots=True, kw_only=True)
class CohortResult:
    asof: str
    horizon: str
    membership: CohortMembership
    pairs: dict[Basis, CohortPair]
    matured: bool
    integrity_failures: tuple[str, ...]
    population_counts: dict[Basis, int]


def evaluate_cohort(
    panel: Sequence[PanelRow], forward_rows: Sequence[ForwardReturnRow], *, asof: str, horizon: str
) -> CohortResult:
    selection = select_exploration(_panel_candidates(panel))
    comparator = next(
        (row.ticker for row in panel if row.selection_rank == COMPARATOR_RANK),
        None,
    )
    membership = CohortMembership(asof=asof, selection=selection, comparator_ticker=comparator)
    priced_at_asof = frozenset(row.ticker for row in panel if row.close is not None)
    pairs: dict[Basis, CohortPair] = {}
    counts: dict[Basis, int] = {}
    failures: list[str] = []
    matured = False
    for basis in BASES:
        returns, basis_status, price_status = _basis_returns(
            forward_rows, horizon=horizon, basis=basis
        )
        population = [row for row in panel if row.in_population and row.ticker in returns]
        counts[basis] = len(population)
        if len(population) < MIN_AXIS_SAMPLE:
            continue
        if basis == "price":
            matured = True
        population_median = median(returns[row.ticker] for row in population)
        if selection.ticker is None or comparator is None:
            continue
        if basis == "price":
            failures.extend(
                _integrity_failures(
                    (selection.ticker, comparator),
                    price_status=price_status,
                    priced_at_asof=priced_at_asof,
                )
            )
        pairs[basis] = CohortPair(
            asof=asof,
            horizon=horizon,
            basis=basis,
            exploration=_observation(
                selection.ticker,
                returns=returns,
                basis_status=basis_status,
                price_status=price_status,
                population_median=population_median,
            ),
            comparator=_observation(
                comparator,
                returns=returns,
                basis_status=basis_status,
                price_status=price_status,
                population_median=population_median,
            ),
            population_n=len(population),
            population_median=population_median,
            same_ticker=selection.ticker == comparator,
        )
    return CohortResult(
        asof=asof,
        horizon=horizon,
        membership=membership,
        pairs=pairs,
        matured=matured,
        integrity_failures=tuple(failures),
        population_counts=counts,
    )


def _share(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _trap_rate(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return sum(1 for value in values if value < TRAP_EXCESS_THRESHOLD) / len(values)


@dataclass(frozen=True, slots=True, kw_only=True)
class EffectResult:
    n: int
    changed_n: int
    unique_tickers: int
    pair_delta_median: float | None
    positive_share: float | None
    exploration_median_excess: float | None
    comparator_median_excess: float | None
    exploration_trap_rate: float | None
    comparator_trap_rate: float | None
    changed_only_median: float | None
    changed_only_positive_share: float | None

    def passes(self) -> bool:
        return (
            self.pair_delta_median is not None
            and self.pair_delta_median >= MIN_PAIR_DELTA_MEDIAN
            and self.positive_share is not None
            and self.positive_share >= MIN_POSITIVE_SHARE
            and self.exploration_trap_rate is not None
            and self.comparator_trap_rate is not None
            and self.exploration_trap_rate <= self.comparator_trap_rate
            and self.exploration_median_excess is not None
            and self.exploration_median_excess > 0
        )

    def payload(self) -> dict[str, object]:
        return {
            "n": self.n,
            "changed_n": self.changed_n,
            "unique_exploration_tickers": self.unique_tickers,
            "pair_delta_median": _round(self.pair_delta_median),
            "positive_share": _round(self.positive_share),
            "exploration_median_excess": _round(self.exploration_median_excess),
            "comparator_median_excess": _round(self.comparator_median_excess),
            "exploration_trap_rate": _round(self.exploration_trap_rate),
            "comparator_trap_rate": _round(self.comparator_trap_rate),
            # Not preregistered and not read by any gate. The preregistered median
            # runs over every pair, so the months where both lanes name the same
            # ticker enter as exact zeros and can pin it there. These two say what
            # the comparison looks like with only the months where the policy
            # actually chose differently, so a reader can see whether a zero median
            # means "no edge" or "mostly identical".
            "changed_only_median": _round(self.changed_only_median),
            "changed_only_positive_share": _round(self.changed_only_positive_share),
            "pass": self.passes(),
        }


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def compute_effect(
    pairs: Sequence[CohortPair], *, imputation: Imputation, weighting: Weighting
) -> EffectResult:
    usable: list[tuple[CohortPair, float, float]] = []
    for pair in pairs:
        resolved = pair.excesses(imputation)
        if resolved is None:
            continue
        usable.append((pair, resolved[0], resolved[1]))
    if weighting == "cohort":
        deltas = [left - right for _, left, right in usable]
        exploration = [left for _, left, _ in usable]
        comparator = [right for _, _, right in usable]
        exploration_traps = [float(value < TRAP_EXCESS_THRESHOLD) for value in exploration]
        comparator_traps = [float(value < TRAP_EXCESS_THRESHOLD) for value in comparator]
    else:
        by_ticker: dict[str, list[tuple[float, float]]] = defaultdict(list)
        for pair, left, right in usable:
            by_ticker[pair.exploration.ticker].append((left, right))
        deltas = [median(left - right for left, right in rows) for rows in by_ticker.values()]
        exploration = [median(left for left, _ in rows) for rows in by_ticker.values()]
        comparator = [median(right for _, right in rows) for rows in by_ticker.values()]
        exploration_traps = [
            sum(1 for left, _ in rows if left < TRAP_EXCESS_THRESHOLD) / len(rows)
            for rows in by_ticker.values()
        ]
        comparator_traps = [
            sum(1 for _, right in rows if right < TRAP_EXCESS_THRESHOLD) / len(rows)
            for rows in by_ticker.values()
        ]
    changed = [(pair, left, right) for pair, left, right in usable if not pair.same_ticker]
    changed_deltas = [left - right for _, left, right in changed]
    if weighting == "ticker":
        by_changed_ticker: dict[str, list[float]] = defaultdict(list)
        for pair, left, right in changed:
            by_changed_ticker[pair.exploration.ticker].append(left - right)
        changed_deltas = [median(values) for values in by_changed_ticker.values()]
    # Under ticker weighting every count is a ticker count, so the changed figure
    # has to collapse to tickers too. Leaving it as a pair count made a window
    # report more changed units than units.
    changed_units = len(changed_deltas)
    return EffectResult(
        n=len(deltas),
        changed_n=changed_units,
        unique_tickers=len({pair.exploration.ticker for pair, _, _ in usable}),
        # A pair whose two lanes named the same ticker contributes a zero delta: it
        # belongs in the denominator (the policy did produce that month) but is not
        # evidence that the policy beat the comparator.
        pair_delta_median=median(deltas) if deltas else None,
        positive_share=_share(sum(1 for value in deltas if value > 0), len(deltas)),
        exploration_median_excess=median(exploration) if exploration else None,
        comparator_median_excess=median(comparator) if comparator else None,
        exploration_trap_rate=(sum(exploration_traps) / len(exploration_traps))
        if exploration_traps
        else None,
        comparator_trap_rate=(sum(comparator_traps) / len(comparator_traps))
        if comparator_traps
        else None,
        changed_only_median=median(changed_deltas) if changed_deltas else None,
        changed_only_positive_share=_share(
            sum(1 for value in changed_deltas if value > 0), len(changed_deltas)
        ),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class WindowResult:
    name: str
    horizon: str
    eligible_matured_cohorts: int
    candidate_available_cohorts: int
    price_resolved_pairs: int
    total_resolved_pairs: int
    membership_statuses: dict[str, int]
    integrity_failures: tuple[str, ...]
    sufficiency: dict[str, object]
    effects: dict[str, dict[str, object]]

    @property
    def sufficient(self) -> bool:
        return bool(self.sufficiency.get("pass"))

    @property
    def passes(self) -> bool:
        return self.sufficient and all(bool(cell.get("pass")) for cell in self.effects.values())


def evaluate_window(name: str, cohorts: Sequence[CohortResult]) -> WindowResult:
    horizon = WINDOWS[name][0]
    matured = [cohort for cohort in cohorts if cohort.matured]
    available = [cohort for cohort in matured if cohort.membership.selection.ticker is not None]
    statuses: dict[str, int] = defaultdict(int)
    for cohort in matured:
        statuses[cohort.membership.selection.status] += 1
    price_pairs = [
        cohort.pairs["price"]
        for cohort in available
        if "price" in cohort.pairs and cohort.pairs["price"].excesses("reported") is not None
    ]
    total_pairs = [
        cohort.pairs["total"]
        for cohort in available
        if "total" in cohort.pairs and cohort.pairs["total"].excesses("reported") is not None
    ]
    failures = tuple(
        f"{cohort.asof}:{item}" for cohort in matured for item in cohort.integrity_failures
    )
    reported = {"price": price_pairs, "total": total_pairs}
    availability = _share(len(available), len(matured))
    price_resolution = _share(len(price_pairs), len(available))
    total_resolution = _share(len(total_pairs), len(price_pairs))
    basis_floors: dict[str, object] = {}
    floors_pass = True
    for basis in BASES:
        effect = compute_effect(reported[basis], imputation="reported", weighting="cohort")
        ok = (
            effect.n >= MIN_PAIRS
            and effect.changed_n >= MIN_CHANGED_PAIRS
            and effect.unique_tickers >= MIN_UNIQUE_TICKERS
        )
        floors_pass = floors_pass and ok
        basis_floors[basis] = {
            "paired_n": effect.n,
            "changed_pair_n": effect.changed_n,
            "unique_exploration_tickers": effect.unique_tickers,
            "pass": ok,
        }
    sufficiency: dict[str, object] = {
        "eligible_matured_cohorts": len(matured),
        "candidate_available_cohorts": len(available),
        "price_resolved_pairs": len(price_pairs),
        "total_resolved_pairs": len(total_pairs),
        "candidate_availability": _round(availability),
        "price_resolution": _round(price_resolution),
        "total_resolution": _round(total_resolution),
        "basis_floors": basis_floors,
        "integrity_failure_count": len(failures),
        "pass": (
            not failures
            and availability is not None
            and availability >= MIN_CANDIDATE_AVAILABILITY
            and price_resolution is not None
            and price_resolution >= MIN_PRICE_RESOLUTION
            and total_resolution is not None
            and total_resolution >= MIN_TOTAL_RESOLUTION
            and floors_pass
        ),
    }
    effects: dict[str, dict[str, object]] = {}
    for basis in BASES:
        basis_pairs = [cohort.pairs[basis] for cohort in available if basis in cohort.pairs]
        for imputation in IMPUTATIONS:
            for weighting in WEIGHTINGS:
                effects[f"{basis}/{imputation}/{weighting}"] = compute_effect(
                    basis_pairs, imputation=imputation, weighting=weighting
                ).payload()
    return WindowResult(
        name=name,
        horizon=horizon,
        eligible_matured_cohorts=len(matured),
        candidate_available_cohorts=len(available),
        price_resolved_pairs=len(price_pairs),
        total_resolved_pairs=len(total_pairs),
        membership_statuses=dict(statuses),
        integrity_failures=failures,
        sufficiency=sufficiency,
        effects=effects,
    )


def resolve_verdict(windows: Mapping[str, WindowResult]) -> tuple[str, list[str]]:
    """Apply the preregistered precedence: insufficient > inconclusive > negative."""
    reasons: list[str] = []
    insufficient = [name for name, window in windows.items() if not window.sufficient]
    if insufficient:
        reasons.extend(f"insufficient:{name}" for name in sorted(insufficient))
        return "insufficient", reasons
    signs: set[int] = set()
    for name, window in windows.items():
        for cell, payload in window.effects.items():
            value = payload.get("pair_delta_median")
            if not isinstance(value, int | float):
                reasons.append(f"missing_delta:{name}/{cell}")
                return "insufficient", reasons
            signs.add(0 if value == 0 else (1 if value > 0 else -1))
    if len({sign for sign in signs if sign != 0}) > 1:
        reasons.append("pair_delta_sign_splits_across_cells")
        return "inconclusive", reasons
    failing = [
        f"{name}/{cell}"
        for name, window in windows.items()
        for cell, payload in window.effects.items()
        if not payload.get("pass")
    ]
    if failing:
        reasons.extend(f"effect_predicate_failed:{item}" for item in sorted(failing))
        return "negative", reasons
    return "adoption_candidate", reasons


def build_authority(cohorts: Mapping[tuple[str, str], CohortResult]) -> dict[str, object]:
    integrity: list[CohortIntegrity] = []
    cells: list[dict[str, object]] = []
    for asof in AUTHORITY_ASOFS:
        for horizon in ("3y", "5y"):
            result = cohorts.get((asof, horizon))
            if result is None:
                continue
            reproducible = (
                result.membership.selection.ticker is not None
                and result.membership.comparator_ticker is not None
                and "price" in result.pairs
                and "total" in result.pairs
            )
            eligible = reproducible and not result.integrity_failures
            integrity.append(
                CohortIntegrity(
                    asof=asof,
                    horizon=horizon,
                    integrity_status="eligible" if not result.integrity_failures else "blocked",
                    metric_statuses={
                        **dict.fromkeys(PRODUCTION_REQUIRED_METRICS, "eligible"),
                        AUTHORITY_METRIC: "eligible" if eligible else "unresolved",
                    },
                )
            )
            cells.append(
                {
                    "asof": asof,
                    "horizon": horizon,
                    "exploration_ticker": result.membership.selection.ticker,
                    "exploration_source_rank": result.membership.selection.source_rank,
                    "normalized_per_3fy": result.membership.selection.normalized_per_3fy,
                    "good_decile_cutoff": result.membership.selection.good_decile_cutoff,
                    "band_metric_count": result.membership.selection.band_metric_count,
                    "comparator_ticker": result.membership.comparator_ticker,
                    "metric_status": "eligible" if eligible else "unresolved",
                    "integrity_failures": list(result.integrity_failures),
                }
            )
    scope = EvaluationScope(
        run_purpose="production_decision",
        requested_horizons=("3y", "5y"),
        cohort_window={"start": AUTHORITY_ASOFS[0], "end": AUTHORITY_ASOFS[-1]},
        required_asofs=AUTHORITY_ASOFS,
        required_metrics=(*PRODUCTION_REQUIRED_METRICS, AUTHORITY_METRIC),
    )
    decision = decide_authority(scope, tuple(integrity))
    return {"decision": decision.payload(), "cells": cells}


def require_single_rules_hash(calibration_dir: Path, asofs: Sequence[str]) -> str:
    hashes: dict[str, list[str]] = defaultdict(list)
    for asof in asofs:
        meta = read_panel_meta(calibration_dir, date.fromisoformat(asof))
        value = meta.get("rules_hash")
        if not isinstance(value, str) or not value:
            raise ExplorationMeasurementError(f"panel {asof} carries no rules_hash")
        hashes[value].append(asof)
    if len(hashes) > 1:
        summary = ", ".join(f"{key}={len(names)}" for key, names in sorted(hashes.items()))
        raise ExplorationMeasurementError(f"panels mix screening rules revisions: {summary}")
    return next(iter(hashes))


def measure(calibration_dir: Path) -> dict[str, object]:
    asofs = sorted(
        path.name.removeprefix("panel-").removesuffix(".csv")
        for path in calibration_dir.glob("panel-*.csv")
    )
    if not asofs:
        raise ExplorationMeasurementError(f"no panels under {calibration_dir}")
    rules_hash = require_single_rules_hash(calibration_dir, asofs)
    needed = {
        asof: sorted({WINDOWS[name][0] for name in WINDOWS if _in_window(name, asof)})
        for asof in asofs
    }
    cohorts: dict[tuple[str, str], CohortResult] = {}
    for asof in asofs:
        horizons = sorted({*needed[asof], *(("3y", "5y") if asof in AUTHORITY_ASOFS else ())})
        if not horizons:
            continue
        as_of_date = date.fromisoformat(asof)
        panel = read_panel(calibration_dir, as_of_date)
        forward_rows = read_forward(calibration_dir, as_of_date)
        for horizon in horizons:
            cohorts[(asof, horizon)] = evaluate_cohort(
                panel, forward_rows, asof=asof, horizon=horizon
            )
    windows = {
        name: evaluate_window(
            name,
            [
                cohorts[(asof, WINDOWS[name][0])]
                for asof in asofs
                if _in_window(name, asof) and (asof, WINDOWS[name][0]) in cohorts
            ],
        )
        for name in WINDOWS
    }
    verdict, reasons = resolve_verdict(windows)
    authority = build_authority(cohorts)
    if verdict == "adoption_candidate" and not authority["decision"]["production_change_allowed"]:  # type: ignore[index]
        verdict = "insufficient"
        reasons.append("authority_not_granted")
    return {
        "policy_version": POLICY_VERSION,
        "selection_lane": EXPLORATION_LANE,
        "rules_hash": rules_hash,
        "comparator": f"full_rank_{COMPARATOR_RANK}",
        "verdict": verdict,
        "verdict_reasons": reasons,
        "windows": {
            name: {
                "horizon": window.horizon,
                "membership_statuses": window.membership_statuses,
                "sufficiency": window.sufficiency,
                "integrity_failures": list(window.integrity_failures),
                "effects": window.effects,
                "pass": window.passes,
            }
            for name, window in windows.items()
        },
        "authority": authority,
    }


def _in_window(name: str, asof: str) -> bool:
    _, start, end = WINDOWS[name]
    return start <= asof <= end


def _emit(payload: Mapping[str, object], *, stream: TextIO) -> None:
    document = yaml.safe_dump(dict(payload), sort_keys=False, allow_unicode=True)
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    stream.write(document)
    stream.write(f"artifact_sha256: {digest}\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration-dir", type=Path, default=DEFAULT_CALIBRATION_DIR)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        payload = measure(args.calibration_dir)
    except ExplorationMeasurementError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.out is not None:
        with args.out.open("w", encoding="utf-8") as handle:
            _emit(payload, stream=handle)
    _emit(payload, stream=sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
