from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from baibai_loop.foundation.coerce import (
    int_map,
    int_or,
    mapping_sequence,
    string_or_empty,
    string_sequence,
)
from baibai_loop.market.benchmark import NIKKEI225_ETF_PROXY
from baibai_loop.screening.regime import MarketRegimeSnapshot, compute_market_regime
from baibai_loop.screening.rule_config import ScreeningRules
from baibai_loop.screening.selection import (
    PreviousCandidates,
    PriorResearch,
    build_selection_sweep_payload,
    candidate_record_from_mapping,
    load_previous_candidates,
    load_prior_research,
)

from .forward_return import (
    DEFAULT_HORIZON_WEEKS,
    HorizonAggregate,
    TickerForwardReturn,
    aggregate_forward_returns,
    compute_ticker_forward_returns,
    latest_bar_date,
    load_bars_for_tickers,
)
from .weeks import WeekSpec, load_week_candidates

# Replay is intentionally macro-agnostic: only 3 weeks have a non-stale macro
# context and fabricating historical contexts would violate the fact/analysis
# separation. macro context is a soft selection diagnostic, not a gate, so the
# sweep is replayed with macro_context=None and the artifact notes this.
DISTRIBUTION_FIELDS: tuple[str, ...] = (
    "selection_playbook",
    "fast_confidence",
    "fast_data_status",
    "long_hold_rating",
)


@dataclass(frozen=True, slots=True)
class ProfileWeekResult:
    week: date
    profile: str
    is_holdout: bool
    market_regime: Mapping[str, object] | None
    recommended_tickers: tuple[str, ...]
    recommended: tuple[Mapping[str, object], ...]
    fast_dislocation_count: int
    long_hold_counts: Mapping[str, int]
    suppressed_count: int
    distributions: Mapping[str, Mapping[str, int]]
    forward_returns: tuple[TickerForwardReturn, ...]
    forward_aggregates: tuple[HorizonAggregate, ...]


@dataclass(frozen=True, slots=True)
class ReplayResult:
    profiles: tuple[str, ...]
    horizon_weeks: tuple[int, ...]
    eval_cap: date | None
    benchmark_ticker: str
    regime_lens: bool
    results: tuple[ProfileWeekResult, ...]


def run_replay(
    weeks: Sequence[WeekSpec],
    *,
    profiles: Sequence[str],
    rules: ScreeningRules,
    sqlite_path: Path,
    candidates_root: Path,
    ledger_root: Path,
    top: int = 10,
    horizon_weeks: Sequence[int] = DEFAULT_HORIZON_WEEKS,
    benchmark_ticker: str = NIKKEI225_ETF_PROXY,
    regime_lens: bool = True,
) -> ReplayResult:
    """Replay profile selection over several weeks and score recommended forward return.

    For each week the sweep is built macro-agnostically, recommended tickers are
    collected per profile, and forward returns at each horizon are computed from
    cached bars. Bars are read once for the union of recommended tickers plus the
    benchmark proxy, so the cost is independent of universe size.

    ``regime_lens`` toggles the market regime lens per week so a replay can
    compare lens-on and lens-off rankings over the same recorded candidates.
    """
    regimes: dict[date, MarketRegimeSnapshot | None] = {
        spec.asof: compute_market_regime(sqlite_path, spec.asof) if regime_lens else None
        for spec in weeks
    }
    # Each weekly YAML is read twice: as the current week's candidates AND as
    # the previous-week reference for the next week. The dict here lets both
    # loaders skip the second parse — the post-CSafeLoader hotspot is the
    # Python-side constructor (~1.6s tottime for 6 weeks), so deduping the parse
    # is the largest win available without restructuring the loop.
    payload_cache: dict[Path, Mapping[str, object]] = {}
    sweeps = [
        (
            spec,
            _build_week_sweep(
                spec,
                profiles=profiles,
                rules=rules,
                top=top,
                candidates_root=candidates_root,
                ledger_root=ledger_root,
                market_regime=regimes[spec.asof],
                payload_cache=payload_cache,
            ),
        )
        for spec in weeks
    ]
    needed = {benchmark_ticker}
    for _spec, payload in sweeps:
        for profile_result in _profile_results(payload):
            needed.update(string_sequence(profile_result.get("recommended_tickers")))
    bars = load_bars_for_tickers(sqlite_path, needed)
    eval_cap = latest_bar_date(bars)

    results: list[ProfileWeekResult] = []
    for spec, payload in sweeps:
        for profile_result in _profile_results(payload):
            tickers = tuple(string_sequence(profile_result.get("recommended_tickers")))
            recommended = tuple(mapping_sequence(profile_result.get("recommended")))
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
            regime = regimes[spec.asof]
            results.append(
                ProfileWeekResult(
                    week=spec.asof,
                    profile=string_or_empty(profile_result.get("profile")),
                    is_holdout=spec.is_holdout,
                    market_regime=regime.to_dict() if regime is not None else None,
                    recommended_tickers=tickers,
                    recommended=recommended,
                    fast_dislocation_count=int_or(profile_result.get("fast_dislocation_count"), 0),
                    long_hold_counts=int_map(profile_result.get("long_hold_counts")),
                    suppressed_count=int_or(profile_result.get("suppressed_count"), 0),
                    distributions=_distributions(recommended),
                    forward_returns=forward,
                    forward_aggregates=tuple(aggregate_forward_returns(forward, horizon_weeks)),
                )
            )
    return ReplayResult(
        profiles=tuple(profiles),
        horizon_weeks=tuple(horizon_weeks),
        eval_cap=eval_cap,
        benchmark_ticker=benchmark_ticker,
        regime_lens=regime_lens,
        results=tuple(results),
    )


def replay_to_payload(result: ReplayResult) -> dict[str, object]:
    """Serialize a replay result into a plain, YAML-friendly mapping.

    Used to persist a reproducible replay artifact input. Per-ticker detail is
    summarized to recommended tickers and per-horizon aggregates so the output
    stays reviewable; the per-week sweep diagnostics are kept for the artifact.
    """
    return {
        "profiles": list(result.profiles),
        "horizon_weeks": list(result.horizon_weeks),
        "eval_cap": result.eval_cap.isoformat() if result.eval_cap else None,
        "benchmark_ticker": result.benchmark_ticker,
        "regime_lens": result.regime_lens,
        "weeks": [
            {
                "week": item.week.isoformat(),
                "profile": item.profile,
                "is_holdout": item.is_holdout,
                "market_regime": dict(item.market_regime)
                if item.market_regime is not None
                else None,
                "recommended_tickers": list(item.recommended_tickers),
                "fast_dislocation_count": item.fast_dislocation_count,
                "long_hold_counts": dict(item.long_hold_counts),
                "suppressed_count": item.suppressed_count,
                "distributions": {
                    field: dict(values) for field, values in item.distributions.items()
                },
                "forward_aggregates": [
                    {
                        "weeks": aggregate.weeks,
                        "count": aggregate.count,
                        "mean_return": aggregate.mean_return,
                        "median_return": aggregate.median_return,
                        "mean_relative": aggregate.mean_relative,
                    }
                    for aggregate in item.forward_aggregates
                ],
            }
            for item in result.results
        ],
    }


def _build_week_sweep(
    spec: WeekSpec,
    *,
    profiles: Sequence[str],
    rules: ScreeningRules,
    top: int,
    candidates_root: Path,
    ledger_root: Path,
    market_regime: MarketRegimeSnapshot | None = None,
    payload_cache: dict[Path, Mapping[str, object]] | None = None,
) -> Mapping[str, object]:
    candidates = tuple(
        candidate_record_from_mapping(item)
        for item in load_week_candidates(spec.candidates_path, payload_cache=payload_cache)
    )
    # previous_candidates is resolved within the replay root so overlap is scoped
    # to the replay set, while prior_research stays anchored to the real ledger.
    previous_candidates: PreviousCandidates = load_previous_candidates(
        candidates_root,
        spec.asof,
        current_path=spec.candidates_path,
        payload_cache=payload_cache,
    )
    prior_research: Mapping[str, PriorResearch] = load_prior_research(
        ledger_root / "_decisions/thesis-decisions", spec.asof
    )
    return build_selection_sweep_payload(
        asof_date=spec.asof,
        candidates=candidates,
        macro_context=None,
        rules=rules,
        top=top,
        profiles=profiles,
        candidates_ref=str(spec.candidates_path),
        macro_context_ref=None,
        previous_candidates=previous_candidates,
        prior_research_by_ticker=prior_research,
        market_regime=market_regime,
    )


def _distributions(
    recommended: Sequence[Mapping[str, object]],
) -> dict[str, dict[str, int]]:
    distributions: dict[str, dict[str, int]] = {}
    for field in DISTRIBUTION_FIELDS:
        counter: Counter[str] = Counter()
        for item in recommended:
            counter[string_or_empty(item.get(field)) or "unknown"] += 1
        distributions[field] = dict(counter)
    return distributions


def _profile_results(payload: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    return mapping_sequence(payload.get("profiles"))
