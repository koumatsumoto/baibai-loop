"""Selection ablation replay: measure what each ranking feature is worth.

Replays the recorded weekly candidates through selection variants that each
disable one ranking component (fast boost, long-hold, lane rank, evidence
strength, diversity caps, prior-research suppression) or drop one evidence
lane entirely, then scores every variant's recommended queue by forward
return versus the benchmark proxy. The deltas against the ``full`` variant
quantify each feature's contribution to recommended performance, which is the
evidence base for removing features that do not earn their complexity.

This is forward-only measurement over recorded screening output — the same
stance as the replay and the lane cohorts — not a parameter search: variants
are pre-enumerated feature switches, not threshold sweeps.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

from baibai_loop.screening.regime import MarketRegimeSnapshot, compute_market_regime
from baibai_loop.screening.rule_config import ScreeningRules
from baibai_loop.screening.selection import (
    CandidateRecord,
    PreviousCandidates,
    PriorResearch,
    RankingToggles,
    build_selection_payload,
    candidate_record_from_mapping,
    load_previous_candidates,
    load_prior_research,
)

from .benchmark import NIKKEI225_ETF_PROXY
from .forward_return import (
    TickerForwardReturn,
    compute_ticker_forward_returns,
    format_pct,
    latest_bar_date,
    load_bars_for_tickers,
)
from .weeks import WeekSpec, load_week_candidates

FULL_VARIANT = "full"

_LANES: tuple[str, ...] = (
    "valuation-reversion",
    "cash-rich-asset-discount",
    "cashflow-yield-discount",
    "sales-discount-growth",
)

# Effectively unlimited diversity caps: the recommendation limit (top N) binds
# long before these do, so the variant isolates the caps' effect.
_NO_DIVERSITY_OVERRIDES: Mapping[str, Mapping[str, object]] = {
    "diversity": {
        "max_recommended_per_sector": 10_000,
        "max_recommended_per_lane": 10_000,
        "max_previous_candidates_in_recommended": None,
    }
}


_ALL_ON_TOGGLES = RankingToggles()


@dataclass(frozen=True, slots=True)
class AblationVariant:
    name: str
    ranking_toggles: RankingToggles = _ALL_ON_TOGGLES
    drop_lane: str | None = None
    disable_diversity: bool = False
    disable_prior_suppression: bool = False


DEFAULT_VARIANTS: tuple[AblationVariant, ...] = (
    AblationVariant(name=FULL_VARIANT),
    AblationVariant(name="no_fast_boost", ranking_toggles=RankingToggles(fast_boost=False)),
    AblationVariant(name="no_lane_rank", ranking_toggles=RankingToggles(lane_rank=False)),
    AblationVariant(name="no_strength", ranking_toggles=RankingToggles(strength=False)),
    AblationVariant(name="no_diversity", disable_diversity=True),
    *(AblationVariant(name=f"drop_lane:{lane}", drop_lane=lane) for lane in _LANES),
)
# F1 / F3 dead-code cleanup: `no_prior_suppression` and `no_stabilization`
# variants were removed from DEFAULT_VARIANTS. Both measured Δfull ≈ 0pt
# across 6 weeks (no_prior_suppression overlap 100% — never altered the queue;
# no_stabilization recorded Δfull = -0.2pt noise because fast_boost is
# neutralized 5/6 weeks of 2026-05 by the regime gate, making the
# stabilization toggle a no-op in production). Keeping them in the default set
# would just inflate the ablation table without informing decisions; callers
# that want to re-measure can still construct them ad hoc via AblationVariant.


@dataclass(frozen=True, slots=True)
class VariantWeekResult:
    week: date
    variant: str
    recommended_tickers: tuple[str, ...]
    overlap_with_full: float | None
    forward_returns: tuple[TickerForwardReturn, ...]


@dataclass(frozen=True, slots=True)
class VariantAggregate:
    variant: str
    horizon_weeks: int
    week_count: int
    resolved_count: int
    mean_return: float | None
    mean_relative: float | None
    mean_overlap_with_full: float | None


@dataclass(frozen=True, slots=True)
class AblationResult:
    profile: str
    top: int
    horizon_weeks: tuple[int, ...]
    eval_cap: date | None
    benchmark_ticker: str
    week_results: tuple[VariantWeekResult, ...]
    aggregates: tuple[VariantAggregate, ...]


def run_selection_ablation(
    weeks: Sequence[WeekSpec],
    *,
    rules: ScreeningRules,
    sqlite_path: Path,
    candidates_root: Path,
    ledger_root: Path,
    variants: Sequence[AblationVariant] = DEFAULT_VARIANTS,
    profile: str | None = None,
    top: int = 5,
    horizon_weeks: Sequence[int] = (1, 4),
    benchmark_ticker: str = NIKKEI225_ETF_PROXY,
) -> AblationResult:
    """Replay every variant over every week and score recommended forward return.

    Like the profile replay this is macro-agnostic (``macro_context=None``), so
    the macro ranking component is constant across candidates and ablating it
    here would be a no-op; macro is excluded from the variant set by default.
    """
    effective_profile = profile or rules.selection.default_profile
    recommended: dict[tuple[date, str], tuple[str, ...]] = {}
    for spec in weeks:
        inputs = _load_week_inputs(
            spec,
            candidates_root=candidates_root,
            ledger_root=ledger_root,
            sqlite_path=sqlite_path,
        )
        for variant in variants:
            recommended[(spec.asof, variant.name)] = _recommended_tickers(
                inputs,
                variant=variant,
                rules=rules,
                profile=effective_profile,
                top=top,
            )

    needed = {benchmark_ticker}
    for tickers in recommended.values():
        needed.update(tickers)
    bars = load_bars_for_tickers(sqlite_path, needed)
    eval_cap = latest_bar_date(bars)

    week_results: list[VariantWeekResult] = []
    for spec in weeks:
        full_tickers = set(recommended[(spec.asof, FULL_VARIANT)])
        for variant in variants:
            tickers = recommended[(spec.asof, variant.name)]
            forward = (
                tuple(
                    compute_ticker_forward_returns(
                        ticker,
                        spec.asof,
                        bars,
                        eval_cap=eval_cap,
                        horizon_weeks=horizon_weeks,
                        benchmark_ticker=benchmark_ticker,
                    )
                    for ticker in tickers
                )
                if eval_cap is not None
                else ()
            )
            overlap = len(set(tickers) & full_tickers) / len(full_tickers) if full_tickers else None
            week_results.append(
                VariantWeekResult(
                    week=spec.asof,
                    variant=variant.name,
                    recommended_tickers=tickers,
                    overlap_with_full=overlap,
                    forward_returns=forward,
                )
            )
    return AblationResult(
        profile=effective_profile,
        top=top,
        horizon_weeks=tuple(horizon_weeks),
        eval_cap=eval_cap,
        benchmark_ticker=benchmark_ticker,
        week_results=tuple(week_results),
        aggregates=tuple(_aggregate_variants(week_results, variants, horizon_weeks=horizon_weeks)),
    )


def ablation_to_payload(result: AblationResult) -> dict[str, object]:
    """Serialize the ablation result into a plain, YAML-friendly mapping."""
    return {
        "profile": result.profile,
        "top": result.top,
        "horizon_weeks": list(result.horizon_weeks),
        "eval_cap": result.eval_cap.isoformat() if result.eval_cap else None,
        "benchmark_ticker": result.benchmark_ticker,
        "aggregates": [
            {
                "variant": item.variant,
                "horizon_weeks": item.horizon_weeks,
                "week_count": item.week_count,
                "resolved_count": item.resolved_count,
                "mean_return": item.mean_return,
                "mean_relative": item.mean_relative,
                "mean_overlap_with_full": item.mean_overlap_with_full,
            }
            for item in result.aggregates
        ],
        "weeks": [
            {
                "week": item.week.isoformat(),
                "variant": item.variant,
                "recommended_tickers": list(item.recommended_tickers),
                "overlap_with_full": item.overlap_with_full,
            }
            for item in result.week_results
        ],
    }


def render_ablation_summary(result: AblationResult) -> str:
    """Render a fixed-width variant scoreboard for stdout triage."""
    full_relative = {
        item.horizon_weeks: item.mean_relative
        for item in result.aggregates
        if item.variant == FULL_VARIANT
    }
    lines = [
        f"{'variant':<36}{'h':<3}{'n':>4}{'rel':>8}{'Δfull':>8}{'overlap':>9}",
    ]
    for item in result.aggregates:
        baseline = full_relative.get(item.horizon_weeks)
        delta = (
            item.mean_relative - baseline
            if item.mean_relative is not None and baseline is not None
            else None
        )
        lines.append(
            f"{item.variant:<36}{item.horizon_weeks:<3}{item.resolved_count:>4}"
            f"{format_pct(item.mean_relative):>8}{format_pct(delta):>8}"
            f"{format_pct(item.mean_overlap_with_full):>9}"
        )
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class _WeekInputs:
    spec: WeekSpec
    candidates: tuple[CandidateRecord, ...]
    previous_candidates: PreviousCandidates
    prior_research: Mapping[str, PriorResearch]
    market_regime: MarketRegimeSnapshot | None


def _load_week_inputs(
    spec: WeekSpec,
    *,
    candidates_root: Path,
    ledger_root: Path,
    sqlite_path: Path,
    regime_lens: bool = True,
) -> _WeekInputs:
    candidates = tuple(
        candidate_record_from_mapping(item) for item in load_week_candidates(spec.candidates_path)
    )
    return _WeekInputs(
        spec=spec,
        candidates=candidates,
        previous_candidates=load_previous_candidates(
            candidates_root, spec.asof, current_path=spec.candidates_path
        ),
        prior_research=load_prior_research(ledger_root / "_ledger/research-decisions", spec.asof),
        market_regime=compute_market_regime(sqlite_path, spec.asof) if regime_lens else None,
    )


def _recommended_tickers(
    inputs: _WeekInputs,
    *,
    variant: AblationVariant,
    rules: ScreeningRules,
    profile: str,
    top: int,
) -> tuple[str, ...]:
    candidates = inputs.candidates
    if variant.drop_lane is not None:
        candidates = tuple(_drop_lane(item, variant.drop_lane) for item in candidates)
    payload = build_selection_payload(
        asof_date=inputs.spec.asof,
        candidates=candidates,
        macro_context=None,
        rules=rules,
        top=top,
        profile=profile,
        candidates_ref=str(inputs.spec.candidates_path),
        macro_context_ref=None,
        previous_candidates=inputs.previous_candidates,
        prior_research_by_ticker=(
            {} if variant.disable_prior_suppression else inputs.prior_research
        ),
        profile_overrides=(
            {profile: _NO_DIVERSITY_OVERRIDES} if variant.disable_diversity else None
        ),
        ranking_toggles=variant.ranking_toggles,
        market_regime=inputs.market_regime,
    )
    recommendations = payload.get("recommendations")
    if not isinstance(recommendations, Sequence):
        return ()
    return tuple(
        ticker
        for item in recommendations
        if isinstance(item, Mapping) and isinstance(ticker := item.get("ticker"), str)
    )


def _drop_lane(item: CandidateRecord, lane: str) -> CandidateRecord:
    kept = tuple(hit for hit in item.evidence_hits if hit.get("name") != lane)
    return replace(item, evidence_hits=kept)


def _aggregate_variants(
    week_results: Sequence[VariantWeekResult],
    variants: Sequence[AblationVariant],
    *,
    horizon_weeks: Sequence[int],
) -> list[VariantAggregate]:
    aggregates: list[VariantAggregate] = []
    for variant in variants:
        items = [result for result in week_results if result.variant == variant.name]
        for horizon in horizon_weeks:
            returns: list[float] = []
            relatives: list[float] = []
            for result in items:
                for forward in result.forward_returns:
                    for horizon_return in forward.horizons:
                        if horizon_return.weeks != horizon or not horizon_return.resolved:
                            continue
                        if horizon_return.return_ratio is not None:
                            returns.append(horizon_return.return_ratio)
                        if horizon_return.relative is not None:
                            relatives.append(horizon_return.relative)
            overlaps = [
                result.overlap_with_full for result in items if result.overlap_with_full is not None
            ]
            aggregates.append(
                VariantAggregate(
                    variant=variant.name,
                    horizon_weeks=horizon,
                    week_count=len(items),
                    resolved_count=len(returns),
                    mean_return=statistics.fmean(returns) if returns else None,
                    mean_relative=statistics.fmean(relatives) if relatives else None,
                    mean_overlap_with_full=statistics.fmean(overlaps) if overlaps else None,
                )
            )
    return aggregates
