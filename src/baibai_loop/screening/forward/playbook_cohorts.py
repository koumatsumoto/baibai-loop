"""Playbook-cohort forward-return telemetry over weekly candidates.

The replay pipeline scores only the recommended queue (top N per profile), so
nobody measures whether each screening playbook actually produces forward
return across its full weekly cohort of 300-650 candidates. This module
aggregates forward returns per evidence playbook so the monthly retro and
playbook revisions get quantitative footing. It is forward-only measurement of
recorded screening output — the same stance as the replay — not a backtest or
parameter search. Aggregates are facts; interpretation stays in the retro.
"""

from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from baibai_loop.foundation.coerce import optional_float
from baibai_loop.position.benchmark import NIKKEI225_ETF_PROXY
from baibai_loop.position.tracking import resolve_price_on_or_before
from baibai_loop.screening.providers.jquants import JQuantsDailyBar

from .forward_return import format_pct
from .weeks import WeekSpec, load_week_candidates

ALL_CANDIDATES_COHORT = "all_candidates"
DEFAULT_COHORT_HORIZON_WEEKS: tuple[int, ...] = (1, 4)

# Entry prices resolve on/before asof, so a short pre-asof tail of bars is
# enough; loading full per-ticker history for ~650 tickers per week would not
# scale in memory.
_ENTRY_LOOKBACK_CALENDAR_DAYS = 14


@dataclass(frozen=True, slots=True)
class CohortAggregate:
    cohort: str
    horizon_weeks: int
    member_count: int
    resolved_count: int
    mean_return: float | None
    median_return: float | None
    mean_relative: float | None
    win_rate_vs_benchmark: float | None


@dataclass(frozen=True, slots=True)
class CohortWeek:
    week: date
    candidates_path: str
    candidate_count: int
    playbook_counts: Mapping[str, int]
    aggregates: tuple[CohortAggregate, ...]


@dataclass(frozen=True, slots=True)
class CohortResult:
    horizon_weeks: tuple[int, ...]
    eval_cap: date | None
    benchmark_ticker: str
    weeks: tuple[CohortWeek, ...]


def run_playbook_cohorts(
    weeks: Sequence[WeekSpec],
    *,
    sqlite_path: Path,
    horizon_weeks: Sequence[int] = DEFAULT_COHORT_HORIZON_WEEKS,
    benchmark_ticker: str = NIKKEI225_ETF_PROXY,
) -> CohortResult:
    """Aggregate forward returns per cohort for every candidate of each week.

    Each cohort is one evidence playbook or the all-candidates baseline. Bars are
    loaded once for the union of all candidate tickers plus the benchmark,
    restricted to the evaluation window, and grouped by ticker so the per-ticker
    price resolution stays linear in that ticker's own bars.
    """
    cohorts_by_week = [(spec, _load_week_cohorts(spec.candidates_path)) for spec in weeks]
    tickers = {benchmark_ticker}
    for _spec, (cohorts, _count) in cohorts_by_week:
        tickers.update(cohorts.get(ALL_CANDIDATES_COHORT, ()))
    window_start = min((spec.asof for spec in weeks), default=None)
    if window_start is None:
        return CohortResult(
            horizon_weeks=tuple(horizon_weeks),
            eval_cap=None,
            benchmark_ticker=benchmark_ticker,
            weeks=(),
        )
    bars_by_ticker = _load_bars_by_ticker(
        sqlite_path,
        tickers,
        start=window_start - timedelta(days=_ENTRY_LOOKBACK_CALENDAR_DAYS),
    )
    eval_cap = max(
        (bars[-1].traded_at for bars in bars_by_ticker.values() if bars),
        default=None,
    )

    week_results: list[CohortWeek] = []
    for spec, (cohorts, candidate_count) in cohorts_by_week:
        relatives_cache: dict[str, dict[int, tuple[float, float] | None]] = {}
        aggregates: list[CohortAggregate] = []
        for cohort in sorted(cohorts):
            members = cohorts[cohort]
            for horizon in horizon_weeks:
                aggregates.append(
                    _aggregate_cohort(
                        cohort=cohort,
                        horizon=horizon,
                        members=members,
                        asof=spec.asof,
                        eval_cap=eval_cap,
                        bars_by_ticker=bars_by_ticker,
                        benchmark_ticker=benchmark_ticker,
                        relatives_cache=relatives_cache,
                    )
                )
        week_results.append(
            CohortWeek(
                week=spec.asof,
                candidates_path=str(spec.candidates_path),
                candidate_count=candidate_count,
                playbook_counts={
                    playbook: len(members)
                    for playbook, members in sorted(cohorts.items())
                    if playbook != ALL_CANDIDATES_COHORT
                },
                aggregates=tuple(aggregates),
            )
        )
    return CohortResult(
        horizon_weeks=tuple(horizon_weeks),
        eval_cap=eval_cap,
        benchmark_ticker=benchmark_ticker,
        weeks=tuple(week_results),
    )


def pool_cohort_relatives(
    weeks: Sequence[WeekSpec],
    *,
    sqlite_path: Path,
    horizon_weeks: Sequence[int] = DEFAULT_COHORT_HORIZON_WEEKS,
    benchmark_ticker: str = NIKKEI225_ETF_PROXY,
) -> dict[tuple[str, int], list[float]]:
    """Pool the raw per-candidate forward relatives per (cohort, horizon) across weeks.

    Unlike :func:`run_playbook_cohorts`, which keeps per-week cohort means, this
    pools the individual candidate relatives (return minus the benchmark) so a
    downstream scorecard can compute a large-N bootstrap CI over the whole
    candidate cross-section. Forward-only: a (week, horizon) only contributes when
    its target falls on/before the eval cap, and every price resolves on/before its
    own date. A ticker that appears in several playbooks is priced once per
    (week, horizon).
    """
    cohorts_by_week = [(spec, _load_week_cohorts(spec.candidates_path)) for spec in weeks]
    tickers = {benchmark_ticker}
    for _spec, (cohorts, _count) in cohorts_by_week:
        tickers.update(cohorts.get(ALL_CANDIDATES_COHORT, ()))
    window_start = min((spec.asof for spec in weeks), default=None)
    if window_start is None:
        return {}
    bars_by_ticker = _load_bars_by_ticker(
        sqlite_path,
        tickers,
        start=window_start - timedelta(days=_ENTRY_LOOKBACK_CALENDAR_DAYS),
    )
    eval_cap = max(
        (bars[-1].traded_at for bars in bars_by_ticker.values() if bars),
        default=None,
    )
    pooled: dict[tuple[str, int], list[float]] = defaultdict(list)
    for spec, (cohorts, _count) in cohorts_by_week:
        all_members = cohorts.get(ALL_CANDIDATES_COHORT, set())
        relative_by_horizon: dict[int, dict[str, float]] = {}
        for horizon in horizon_weeks:
            target = spec.asof + timedelta(days=horizon * 7)
            if eval_cap is None or target > eval_cap:
                continue
            benchmark_return = _forward_return(
                benchmark_ticker, spec.asof, target, bars_by_ticker.get(benchmark_ticker, ())
            )
            if benchmark_return is None:
                continue
            relatives: dict[str, float] = {}
            for ticker in all_members:
                outcome = _ticker_outcome(
                    ticker,
                    asof=spec.asof,
                    target=target,
                    bars=bars_by_ticker.get(ticker, ()),
                    benchmark_return=benchmark_return,
                )
                if outcome is not None:
                    relatives[ticker] = outcome[1]
            relative_by_horizon[horizon] = relatives
        for cohort, members in cohorts.items():
            for horizon, relatives in relative_by_horizon.items():
                for ticker in sorted(members):
                    relative = relatives.get(ticker)
                    if relative is not None:
                        pooled[(cohort, horizon)].append(relative)
    return dict(pooled)


def playbook_cohorts_to_payload(result: CohortResult) -> dict[str, object]:
    """Serialize the cohort result into a plain, YAML-friendly mapping."""
    return {
        "horizon_weeks": list(result.horizon_weeks),
        "eval_cap": result.eval_cap.isoformat() if result.eval_cap else None,
        "benchmark_ticker": result.benchmark_ticker,
        "weeks": [
            {
                "week": item.week.isoformat(),
                "candidates_path": item.candidates_path,
                "candidate_count": item.candidate_count,
                "playbook_counts": dict(item.playbook_counts),
                "aggregates": [
                    {
                        "cohort": aggregate.cohort,
                        "horizon_weeks": aggregate.horizon_weeks,
                        "member_count": aggregate.member_count,
                        "resolved_count": aggregate.resolved_count,
                        "mean_return": aggregate.mean_return,
                        "median_return": aggregate.median_return,
                        "mean_relative": aggregate.mean_relative,
                        "win_rate_vs_benchmark": aggregate.win_rate_vs_benchmark,
                    }
                    for aggregate in item.aggregates
                ],
            }
            for item in result.weeks
        ],
    }


def render_playbook_cohort_summary(result: CohortResult) -> str:
    """Render a fixed-width scoreboard for stdout triage."""
    lines = [
        f"{'week':<12}{'cohort':<28}{'h':<3}{'n':>5}{'resolved':>9}"
        f"{'mean':>8}{'median':>8}{'rel':>8}{'win':>6}"
    ]
    for week in result.weeks:
        for aggregate in week.aggregates:
            lines.append(
                f"{week.week.isoformat():<12}{aggregate.cohort:<28}{aggregate.horizon_weeks:<3}"
                f"{aggregate.member_count:>5}{aggregate.resolved_count:>9}"
                f"{format_pct(aggregate.mean_return):>8}{format_pct(aggregate.median_return):>8}"
                f"{format_pct(aggregate.mean_relative):>8}{format_pct(aggregate.win_rate_vs_benchmark):>6}"
            )
    return "\n".join(lines)


def _load_week_cohorts(candidates_path: Path) -> tuple[dict[str, set[str]], int]:
    cohorts: dict[str, set[str]] = defaultdict(set)
    candidate_count = 0
    for raw in load_week_candidates(candidates_path):
        ticker = raw.get("ticker")
        if not isinstance(ticker, str) or not ticker:
            continue
        candidate_count += 1
        cohorts[ALL_CANDIDATES_COHORT].add(ticker)
        for playbook in _eligible_playbooks(raw.get("evidence_hits")):
            cohorts[playbook].add(ticker)
    return dict(cohorts), candidate_count


def _eligible_playbooks(raw_evidence_hits: object) -> set[str]:
    # Mirrors selection's sizing-eligible evidence semantics: a hit counts
    # unless its source_status is degraded or sizing_eligible is explicitly
    # false, so the cohorts match what selection would actually rank.
    if not isinstance(raw_evidence_hits, Sequence) or isinstance(raw_evidence_hits, str | bytes):
        return set()
    playbooks: set[str] = set()
    for hit in raw_evidence_hits:
        if not isinstance(hit, Mapping):
            continue
        source_status = hit.get("source_status")
        if isinstance(source_status, str) and source_status != "ok":
            continue
        if hit.get("sizing_eligible") is False:
            continue
        playbook = hit.get("playbook_id") or hit.get("name")
        if isinstance(playbook, str) and playbook:
            playbooks.add(playbook)
    return playbooks


def _aggregate_cohort(
    *,
    cohort: str,
    horizon: int,
    members: set[str],
    asof: date,
    eval_cap: date | None,
    bars_by_ticker: Mapping[str, Sequence[JQuantsDailyBar]],
    benchmark_ticker: str,
    relatives_cache: dict[str, dict[int, tuple[float, float] | None]],
) -> CohortAggregate:
    returns: list[float] = []
    relatives: list[float] = []
    target = asof + timedelta(days=horizon * 7)
    if eval_cap is not None and target <= eval_cap:
        benchmark_return = _forward_return(
            benchmark_ticker, asof, target, bars_by_ticker.get(benchmark_ticker, ())
        )
        for ticker in sorted(members):
            cached = relatives_cache.setdefault(ticker, {})
            if horizon not in cached:
                cached[horizon] = _ticker_outcome(
                    ticker,
                    asof=asof,
                    target=target,
                    bars=bars_by_ticker.get(ticker, ()),
                    benchmark_return=benchmark_return,
                )
            outcome = cached[horizon]
            if outcome is None:
                continue
            return_ratio, relative = outcome
            returns.append(return_ratio)
            relatives.append(relative)
    return CohortAggregate(
        cohort=cohort,
        horizon_weeks=horizon,
        member_count=len(members),
        resolved_count=len(returns),
        mean_return=statistics.fmean(returns) if returns else None,
        median_return=statistics.median(returns) if returns else None,
        mean_relative=statistics.fmean(relatives) if relatives else None,
        win_rate_vs_benchmark=(
            sum(1 for relative in relatives if relative > 0) / len(relatives) if relatives else None
        ),
    )


def _ticker_outcome(
    ticker: str,
    *,
    asof: date,
    target: date,
    bars: Sequence[JQuantsDailyBar],
    benchmark_return: float | None,
) -> tuple[float, float] | None:
    if benchmark_return is None:
        return None
    return_ratio = _forward_return(ticker, asof, target, bars)
    if return_ratio is None:
        return None
    return return_ratio, return_ratio - benchmark_return


def _forward_return(
    ticker: str,
    asof: date,
    target: date,
    bars: Sequence[JQuantsDailyBar],
) -> float | None:
    entry = resolve_price_on_or_before(ticker, asof, bars)
    forward = resolve_price_on_or_before(ticker, target, bars)
    if entry is None or forward is None or entry.price == 0:
        return None
    return forward.price / entry.price - 1


def _load_bars_by_ticker(
    sqlite_path: Path,
    tickers: set[str],
    *,
    start: date,
) -> dict[str, list[JQuantsDailyBar]]:
    if not tickers or not sqlite_path.exists():
        return {}
    # placeholders is only "?,?,..." markers; ticker values are bound parameters
    # in conn.execute, so the f-string is not an injection vector (same shape as
    # screening_replay._load_bars_for_tickers).
    placeholders = ",".join("?" for _ in tickers)
    query = (
        "SELECT ticker, traded_at, close, turnover_value, adjustment_close, adjustment_factor "  # nosec B608
        "FROM jquants_daily_bars "
        f"WHERE traded_at >= ? AND ticker IN ({placeholders}) ORDER BY ticker, traded_at"
    )
    conn = sqlite3.connect(sqlite_path)
    try:
        rows = conn.execute(query, (start.isoformat(), *sorted(tickers))).fetchall()
    finally:
        conn.close()
    bars_by_ticker: dict[str, list[JQuantsDailyBar]] = defaultdict(list)
    for ticker, traded_at, close, turnover_value, adjustment_close, adjustment_factor in rows:
        if close is None or traded_at is None:
            continue
        bars_by_ticker[str(ticker)].append(
            JQuantsDailyBar(
                ticker=str(ticker),
                traded_at=date.fromisoformat(traded_at),
                close=float(close),
                turnover_value=optional_float(turnover_value),
                adjustment_close=optional_float(adjustment_close),
                adjustment_factor=optional_float(adjustment_factor),
            )
        )
    return dict(bars_by_ticker)
