from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from baibai_loop.screening.providers.jquants import JQuantsDailyBar
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

from .benchmark import NIKKEI225_ETF_PROXY
from .forward_return import (
    DEFAULT_HORIZON_WEEKS,
    HorizonAggregate,
    TickerForwardReturn,
    aggregate_forward_returns,
    compute_ticker_forward_returns,
    latest_bar_date,
)

# Replay is intentionally macro-agnostic: only 3 weeks have a non-stale macro
# context and fabricating historical contexts would violate the fact/analysis
# separation. macro context is a soft selection diagnostic, not a gate, so the
# sweep is replayed with macro_context=None and the artifact notes this.
DISTRIBUTION_FIELDS: tuple[str, ...] = (
    "selection_lane",
    "fast_confidence",
    "fast_data_status",
    "long_hold_rating",
)


@dataclass(frozen=True, slots=True)
class WeekSpec:
    asof: date
    candidates_path: Path
    is_holdout: bool = False


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
    previous_overlap: object
    concentration: object
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
            ),
        )
        for spec in weeks
    ]
    needed = {benchmark_ticker}
    for _spec, payload in sweeps:
        for profile_result in _profile_results(payload):
            needed.update(_string_list(profile_result.get("recommended_tickers")))
    bars = _load_bars_for_tickers(sqlite_path, needed)
    eval_cap = latest_bar_date(bars)

    results: list[ProfileWeekResult] = []
    for spec, payload in sweeps:
        for profile_result in _profile_results(payload):
            tickers = tuple(_string_list(profile_result.get("recommended_tickers")))
            recommended = tuple(_mapping_list(profile_result.get("recommended")))
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
                    profile=_string(profile_result.get("profile")),
                    is_holdout=spec.is_holdout,
                    market_regime=regime.to_dict() if regime is not None else None,
                    recommended_tickers=tickers,
                    recommended=recommended,
                    fast_dislocation_count=_int(profile_result.get("fast_dislocation_count")),
                    long_hold_counts=_int_map(profile_result.get("long_hold_counts")),
                    suppressed_count=_int(profile_result.get("suppressed_count")),
                    previous_overlap=profile_result.get("previous_overlap"),
                    concentration=profile_result.get("concentration"),
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
                "previous_overlap": item.previous_overlap,
                "concentration": item.concentration,
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


def discover_week_specs(candidates_root: Path, holdout_weeks: int = 0) -> list[WeekSpec]:
    """Discover weekly candidate files under ``candidates_root`` sorted by asof.

    Expects the canonical ``<root>/<YYYY>/<MM>/<YYYY-MM-DD>.yaml`` layout. The
    last ``holdout_weeks`` weeks are flagged as hold-out so the artifact can
    separate the tuning window from the evaluation window.
    """
    specs: list[WeekSpec] = []
    for path in sorted(candidates_root.rglob("*.yaml")):
        try:
            asof = date.fromisoformat(path.stem)
        except ValueError:
            continue
        specs.append(WeekSpec(asof=asof, candidates_path=path))
    specs.sort(key=lambda spec: spec.asof)
    if holdout_weeks > 0:
        cutoff = len(specs) - holdout_weeks
        specs = [
            WeekSpec(spec.asof, spec.candidates_path, is_holdout=index >= cutoff)
            for index, spec in enumerate(specs)
        ]
    return specs


def _build_week_sweep(
    spec: WeekSpec,
    *,
    profiles: Sequence[str],
    rules: ScreeningRules,
    top: int,
    candidates_root: Path,
    ledger_root: Path,
    market_regime: MarketRegimeSnapshot | None = None,
) -> Mapping[str, object]:
    payload = yaml.safe_load(spec.candidates_path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"invalid candidates YAML: {spec.candidates_path}")
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, Sequence):
        raise ValueError(f"candidates list missing: {spec.candidates_path}")
    candidates = tuple(
        candidate_record_from_mapping(item) for item in raw_candidates if isinstance(item, Mapping)
    )
    # previous_candidates is resolved within the replay root so overlap is scoped
    # to the replay set, while prior_research stays anchored to the real ledger.
    previous_candidates: PreviousCandidates = load_previous_candidates(
        candidates_root, spec.asof, current_path=spec.candidates_path
    )
    prior_research: Mapping[str, PriorResearch] = load_prior_research(
        ledger_root / "_ledger/research-decisions", spec.asof
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


def _load_bars_for_tickers(sqlite_path: Path, tickers: set[str]) -> tuple[JQuantsDailyBar, ...]:
    if not tickers or not sqlite_path.exists():
        return ()
    # placeholders is only "?,?,..." markers; ticker values are bound parameters
    # in conn.execute, so the f-string is not an injection vector. bandit cannot
    # see the binding, so suppress its B608 false positive here.
    placeholders = ",".join("?" for _ in tickers)
    query = (
        "SELECT ticker, traded_at, close, turnover_value, adjustment_close, adjustment_factor "  # nosec B608
        "FROM jquants_daily_bars "
        f"WHERE ticker IN ({placeholders}) ORDER BY ticker, traded_at"
    )
    conn = sqlite3.connect(sqlite_path)
    try:
        rows = conn.execute(query, tuple(sorted(tickers))).fetchall()
    finally:
        conn.close()
    bars: list[JQuantsDailyBar] = []
    for ticker, traded_at, close, turnover_value, adjustment_close, adjustment_factor in rows:
        if close is None or traded_at is None:
            continue
        bars.append(
            JQuantsDailyBar(
                ticker=str(ticker),
                traded_at=date.fromisoformat(traded_at),
                close=float(close),
                turnover_value=_optional_float(turnover_value),
                adjustment_close=_optional_float(adjustment_close),
                adjustment_factor=_optional_float(adjustment_factor),
            )
        )
    return tuple(bars)


def _distributions(
    recommended: Sequence[Mapping[str, object]],
) -> dict[str, dict[str, int]]:
    distributions: dict[str, dict[str, int]] = {}
    for field in DISTRIBUTION_FIELDS:
        counter: Counter[str] = Counter()
        for item in recommended:
            counter[_string(item.get(field)) or "unknown"] += 1
        distributions[field] = dict(counter)
    return distributions


def _profile_results(payload: Mapping[str, object]) -> list[Mapping[str, object]]:
    return _mapping_list(payload.get("profiles"))


def _mapping_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _string_list(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [item for item in value if isinstance(item, str)]


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _int_map(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): _int(item) for key, item in value.items()}


def _optional_float(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None
