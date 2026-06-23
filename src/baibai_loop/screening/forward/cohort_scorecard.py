"""Playbook scorecard: pool candidate forward relatives across weeks and turn them
into keep / kill / review decisions for screening playbooks.

`playbook-cohorts` reports per-week cohort means; it does not pool across weeks,
give a confidence interval, or compare a playbook against the all-candidates
baseline. Yet the screening-improvement decision — "does this playbook add
selection value, so keep / strengthen / remove it?" — needs exactly that: the
large-N edge of a playbook over the average candidate, with an interval that says
whether the edge is real.

This module pools the raw per-candidate relatives (return minus the Nikkei 225
ETF proxy) per (cohort, horizon) across all supplied weeks, computes a
deterministic bootstrap CI on the cohort mean, and compares it against the
all-candidates baseline mean to emit a forward-only, principle-disciplined
decision. Aggregates and decisions are facts about recorded screening output; the
retro still owns interpretation. No threshold is fit to the data — the bootstrap
seed and the decision cut points are fixed, and grid search is out of scope
(design-principles §9).
"""

from __future__ import annotations

import random
import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from baibai_loop.market.benchmark import NIKKEI225_ETF_PROXY
from baibai_loop.screening.rule_config import ScreeningRules

from .playbook_cohorts import (
    ALL_CANDIDATES_COHORT,
    DEFAULT_COHORT_HORIZON_WEEKS,
    pool_cohort_relatives,
)
from .screening_replay import run_replay
from .weeks import WeekSpec

RECOMMENDED_QUEUE_COHORT = "recommended_queue"

DEFAULT_MIN_RESOLVED = 30
DEFAULT_BOOTSTRAP_ITERATIONS = 10_000
DEFAULT_BOOTSTRAP_SEED = 20260621
_CI_ALPHA = 0.05


class CohortDecision(Enum):
    """Screening-improvement decision a playbook's forward edge drives.

    The decision is the measurement-to-action link: `keep` / `kill_candidate` feed
    the playbook keep-or-remove retro; `review` flags insufficient or ambiguous
    evidence; `baseline` marks the all-candidates reference row.
    """

    KEEP = "keep"
    KILL_CANDIDATE = "kill_candidate"
    REVIEW = "review"
    BASELINE = "baseline"


@dataclass(frozen=True, slots=True, kw_only=True)
class CohortScore:
    cohort: str
    horizon_weeks: int
    resolved_count: int
    mean_relative: float
    median_relative: float
    ci_low: float
    ci_high: float
    prob_mean_negative: float
    win_rate: float
    baseline_mean_relative: float
    edge_vs_baseline: float
    decision: CohortDecision
    decision_reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ScorecardResult:
    horizon_weeks: tuple[int, ...]
    min_resolved: int
    bootstrap_iterations: int
    bootstrap_seed: int
    benchmark_ticker: str
    scores: tuple[CohortScore, ...]


def run_playbook_scorecard(
    weeks: Sequence[WeekSpec],
    *,
    sqlite_path: Path,
    horizon_weeks: Sequence[int] = DEFAULT_COHORT_HORIZON_WEEKS,
    min_resolved: int = DEFAULT_MIN_RESOLVED,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    bootstrap_seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> ScorecardResult:
    """Score every (cohort, horizon) and attach a keep / kill / review decision.

    The cohort mean is bootstrapped for a CI; the all-candidates pool (which every
    candidate joins) provides the baseline a playbook must beat to add selection
    value. A playbook is `keep` when its CI sits entirely above the baseline,
    `kill_candidate` when entirely below, and `review` when underpowered or the CI
    spans the baseline. The baseline is treated as a fixed reference (large-N, low
    variance), so the edge CI is the cohort CI shifted by the baseline mean.
    """
    pooled = pool_cohort_relatives(weeks, sqlite_path=sqlite_path, horizon_weeks=horizon_weeks)
    baseline_by_horizon: dict[int, float] = {}
    for horizon in horizon_weeks:
        baseline = pooled.get((ALL_CANDIDATES_COHORT, horizon))
        if baseline:
            baseline_by_horizon[horizon] = statistics.fmean(baseline)
    scores: list[CohortScore] = []
    for cohort, horizon in sorted(pooled):
        relatives = pooled[(cohort, horizon)]
        if not relatives:
            continue
        scores.append(
            _score_pool(
                cohort=cohort,
                horizon=horizon,
                relatives=relatives,
                baseline_mean=baseline_by_horizon.get(horizon, 0.0),
                min_resolved=min_resolved,
                bootstrap_iterations=bootstrap_iterations,
                bootstrap_seed=bootstrap_seed,
            )
        )
    return ScorecardResult(
        horizon_weeks=tuple(horizon_weeks),
        min_resolved=min_resolved,
        bootstrap_iterations=bootstrap_iterations,
        bootstrap_seed=bootstrap_seed,
        benchmark_ticker=NIKKEI225_ETF_PROXY,
        scores=tuple(scores),
    )


def run_proposal_scorecard(
    weeks: Sequence[WeekSpec],
    *,
    sqlite_path: Path,
    rules: ScreeningRules,
    candidates_root: Path,
    records_root: Path,
    horizon_weeks: Sequence[int] = DEFAULT_COHORT_HORIZON_WEEKS,
    top: int = 10,
    profile: str = "balanced",
    min_resolved: int = DEFAULT_MIN_RESOLVED,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    bootstrap_seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> ScorecardResult:
    """Score the recommended queue (proposal level) against the all-candidates baseline.

    This is the relevance overlay: it asks whether `select`'s recommended queue —
    the proposals a human would actually consider trading — beats the average
    candidate. The recommended queue is the top-N per week, so the pooled N is
    small (top x resolved weeks); the decision is honestly CI-gated and will read
    `review` (directional only) when underpowered. The robust screening-quality
    conclusion stays at the candidate / playbook level (`run_playbook_scorecard`);
    this overlay only checks that the robust signal survives into tradeable
    proposals.
    """
    replay = run_replay(
        weeks,
        profiles=[profile],
        rules=rules,
        sqlite_path=sqlite_path,
        candidates_root=candidates_root,
        records_root=records_root,
        top=top,
        horizon_weeks=horizon_weeks,
    )
    recommended: dict[int, list[float]] = defaultdict(list)
    for week_result in replay.results:
        for forward in week_result.forward_returns:
            for horizon_return in forward.horizons:
                if horizon_return.resolved and horizon_return.relative is not None:
                    recommended[horizon_return.weeks].append(horizon_return.relative)
    pooled = pool_cohort_relatives(weeks, sqlite_path=sqlite_path, horizon_weeks=horizon_weeks)
    baseline_by_horizon: dict[int, float] = {}
    for horizon in horizon_weeks:
        baseline = pooled.get((ALL_CANDIDATES_COHORT, horizon))
        if baseline:
            baseline_by_horizon[horizon] = statistics.fmean(baseline)
    scores: list[CohortScore] = []
    for horizon in horizon_weeks:
        baseline = pooled.get((ALL_CANDIDATES_COHORT, horizon))
        if baseline:
            scores.append(
                _score_pool(
                    cohort=ALL_CANDIDATES_COHORT,
                    horizon=horizon,
                    relatives=baseline,
                    baseline_mean=baseline_by_horizon.get(horizon, 0.0),
                    min_resolved=min_resolved,
                    bootstrap_iterations=bootstrap_iterations,
                    bootstrap_seed=bootstrap_seed,
                )
            )
        recommended_pool = recommended.get(horizon)
        if recommended_pool:
            scores.append(
                _score_pool(
                    cohort=RECOMMENDED_QUEUE_COHORT,
                    horizon=horizon,
                    relatives=recommended_pool,
                    baseline_mean=baseline_by_horizon.get(horizon, 0.0),
                    min_resolved=min_resolved,
                    bootstrap_iterations=bootstrap_iterations,
                    bootstrap_seed=bootstrap_seed,
                )
            )
    return ScorecardResult(
        horizon_weeks=tuple(horizon_weeks),
        min_resolved=min_resolved,
        bootstrap_iterations=bootstrap_iterations,
        bootstrap_seed=bootstrap_seed,
        benchmark_ticker=NIKKEI225_ETF_PROXY,
        scores=tuple(scores),
    )


def _score_pool(
    *,
    cohort: str,
    horizon: int,
    relatives: Sequence[float],
    baseline_mean: float,
    min_resolved: int,
    bootstrap_iterations: int,
    bootstrap_seed: int,
) -> CohortScore:
    ci_low, ci_high, prob_negative = _bootstrap_mean_ci(
        relatives,
        iterations=bootstrap_iterations,
        seed=bootstrap_seed + horizon,
    )
    mean_relative = statistics.fmean(relatives)
    decision, reason = _decide(
        cohort=cohort,
        resolved=len(relatives),
        ci_low=ci_low,
        ci_high=ci_high,
        baseline_mean=baseline_mean,
        min_resolved=min_resolved,
    )
    return CohortScore(
        cohort=cohort,
        horizon_weeks=horizon,
        resolved_count=len(relatives),
        mean_relative=mean_relative,
        median_relative=statistics.median(relatives),
        ci_low=ci_low,
        ci_high=ci_high,
        prob_mean_negative=prob_negative,
        win_rate=sum(1 for relative in relatives if relative > 0) / len(relatives),
        baseline_mean_relative=baseline_mean,
        edge_vs_baseline=mean_relative - baseline_mean,
        decision=decision,
        decision_reason=reason,
    )


def _bootstrap_mean_ci(
    sample: Sequence[float],
    *,
    iterations: int,
    seed: int,
) -> tuple[float, float, float]:
    """Deterministic percentile bootstrap of the sample mean.

    Returns (ci_low, ci_high, prob_mean_negative). Resampling uses a seeded RNG so
    the same pool yields the same interval, keeping the measurement reproducible.
    """
    n = len(sample)
    if n == 0:
        return 0.0, 0.0, 0.0
    # Deterministic statistical bootstrap resampling; not a security/crypto use.
    rng = random.Random(seed)  # nosec B311
    # Sort to a canonical order so a seeded resample is reproducible regardless of
    # the pool's build order (cohort pooling iterates sets, whose order varies).
    population = sorted(sample)
    means: list[float] = []
    negative = 0
    for _ in range(iterations):
        resample = rng.choices(population, k=n)
        mean = statistics.fmean(resample)
        means.append(mean)
        if mean < 0:
            negative += 1
    means.sort()
    low_index = min(iterations - 1, max(0, int((_CI_ALPHA / 2) * iterations)))
    high_index = min(iterations - 1, max(0, int((1 - _CI_ALPHA / 2) * iterations) - 1))
    return means[low_index], means[high_index], negative / iterations


def _decide(
    *,
    cohort: str,
    resolved: int,
    ci_low: float,
    ci_high: float,
    baseline_mean: float,
    min_resolved: int,
) -> tuple[CohortDecision, str]:
    if cohort == ALL_CANDIDATES_COHORT:
        return CohortDecision.BASELINE, "all-candidates baseline (no playbook decision)"
    if resolved < min_resolved:
        return CohortDecision.REVIEW, f"underpowered: n={resolved} < min_resolved={min_resolved}"
    if ci_low > baseline_mean:
        return (
            CohortDecision.KEEP,
            f"95% CI [{ci_low:+.4f}, {ci_high:+.4f}] sits above baseline {baseline_mean:+.4f}",
        )
    if ci_high < baseline_mean:
        return (
            CohortDecision.KILL_CANDIDATE,
            f"95% CI [{ci_low:+.4f}, {ci_high:+.4f}] sits below baseline {baseline_mean:+.4f}",
        )
    return (
        CohortDecision.REVIEW,
        f"95% CI [{ci_low:+.4f}, {ci_high:+.4f}] spans baseline {baseline_mean:+.4f}",
    )


def scorecard_to_payload(result: ScorecardResult) -> dict[str, object]:
    """Serialize the scorecard into a plain, YAML-friendly mapping."""
    return {
        "horizon_weeks": list(result.horizon_weeks),
        "min_resolved": result.min_resolved,
        "bootstrap_iterations": result.bootstrap_iterations,
        "bootstrap_seed": result.bootstrap_seed,
        "benchmark_ticker": result.benchmark_ticker,
        "scores": [
            {
                "cohort": score.cohort,
                "horizon_weeks": score.horizon_weeks,
                "resolved_count": score.resolved_count,
                "mean_relative": score.mean_relative,
                "median_relative": score.median_relative,
                "ci_low": score.ci_low,
                "ci_high": score.ci_high,
                "prob_mean_negative": score.prob_mean_negative,
                "win_rate": score.win_rate,
                "baseline_mean_relative": score.baseline_mean_relative,
                "edge_vs_baseline": score.edge_vs_baseline,
                "decision": score.decision.value,
                "decision_reason": score.decision_reason,
            }
            for score in result.scores
        ],
    }


def render_scorecard_summary(result: ScorecardResult) -> str:
    """Render a fixed-width scoreboard for stdout triage."""
    header = (
        f"{'cohort':<28}{'h':<3}{'n':>6}{'mean_rel':>10}{'edge':>9}"
        f"{'ci_low':>9}{'ci_high':>9}{'P(<0)':>7}{'win':>6}  decision"
    )
    lines = [header]
    for score in result.scores:
        lines.append(
            f"{score.cohort:<28}{score.horizon_weeks:<3}{score.resolved_count:>6}"
            f"{score.mean_relative * 100:>+9.2f}%{score.edge_vs_baseline * 100:>+8.2f}%"
            f"{score.ci_low * 100:>+8.2f}%{score.ci_high * 100:>+8.2f}%"
            f"{score.prob_mean_negative:>7.2f}{score.win_rate:>6.2f}  {score.decision.value}"
        )
    return "\n".join(lines)
