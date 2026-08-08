"""Fixed exploration policy: one normalized-PER qualified name past the longlist.

The policy is deliberately narrow. It never reorders, replaces, or removes a
baseline longlist entry; it only names at most one additional ticker from the
band immediately after the longlist, and only when that ticker is already in the
good decile of the same liquidity population the normalized-PER axis was
measured on. Ordering inside the band is returned to the calibrated E[r] rank
rather than to the metric, because the metric's evidence is a cross-sectional
decile difference, not a claim that the lowest multiple in a band is the best
name in it.

The band is fixed at the twenty ranks after a twenty-name longlist. A different
longlist depth would move the band to ranks that were never measured, so the
policy declines rather than extrapolating.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

__all__ = (
    "BAND_SIZE",
    "BASELINE_LONGLIST_TOP",
    "EXPLORATION_LANE",
    "GOOD_DECILE_DIVISOR",
    "MIN_BAND_METRIC_COUNT",
    "POLICY_VERSION",
    "ExplorationCandidate",
    "ExplorationSelection",
    "ExplorationStatus",
    "select_exploration",
)

POLICY_VERSION = "normalized_per_3fy_exploration_v1"
EXPLORATION_LANE = "normalized_per_3fy_exploration"
BASELINE_LONGLIST_TOP = 20
BAND_SIZE = 20
GOOD_DECILE_DIVISOR = 10
# Half the band. Below it the qualifying step reads too few names to mean
# "this one stood out among its neighbours", so the cohort abstains instead of
# promoting whichever handful happened to carry the metric.
MIN_BAND_METRIC_COUNT = 10

type ExplorationStatus = Literal[
    "available",
    "no_qualified_candidate",
    "insufficient_metric_coverage",
    "unsupported_longlist_top",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class ExplorationCandidate:
    """One ranked row, reduced to what the policy reads.

    ``full_rank`` is the production full rank (E[r] descending). ``None`` means
    the row carries no E[r] and therefore no rank; such rows still belong to the
    decile population, because the axis was measured over the liquidity
    population rather than over the ranked subset.
    """

    ticker: str
    full_rank: int | None
    in_population: bool
    normalized_per_3fy: float | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ExplorationSelection:
    status: ExplorationStatus
    ticker: str | None = None
    source_rank: int | None = None
    normalized_per_3fy: float | None = None
    good_decile_cutoff: float | None = None
    good_decile_size: int = 0
    decile_population_size: int = 0
    band_metric_count: int = 0
    policy_version: str = POLICY_VERSION

    @property
    def selected(self) -> bool:
        return self.ticker is not None


def _positive_metric(candidate: ExplorationCandidate) -> float | None:
    """The metric when it can carry the policy's meaning, else ``None``.

    A non-positive or non-finite multiple does not say "cheap on three years of
    profit"; it says the three-year average earnings were not positive. Reading
    it as the cheapest end of the axis would rank exactly the names the axis
    cannot speak about.
    """
    value = candidate.normalized_per_3fy
    if value is None or not math.isfinite(value) or value <= 0:
        return None
    return value


def select_exploration(
    candidates: Iterable[ExplorationCandidate],
    *,
    longlist_top: int = BASELINE_LONGLIST_TOP,
) -> ExplorationSelection:
    """Pick at most one exploration ticker from the band after the longlist."""
    if longlist_top != BASELINE_LONGLIST_TOP:
        return ExplorationSelection(status="unsupported_longlist_top")

    rows = list(candidates)
    decile, cutoff, decile_population_size = _good_decile(rows)
    band_start = longlist_top + 1
    band_end = longlist_top + BAND_SIZE
    band_with_metric = [
        row
        for row in rows
        if row.full_rank is not None
        and band_start <= row.full_rank <= band_end
        and _positive_metric(row) is not None
    ]
    shared = ExplorationSelection(
        status="available",
        good_decile_cutoff=cutoff,
        good_decile_size=len(decile),
        decile_population_size=decile_population_size,
        band_metric_count=len(band_with_metric),
    )
    if len(band_with_metric) < MIN_BAND_METRIC_COUNT:
        return _with_status(shared, "insufficient_metric_coverage")
    qualified = sorted(
        (row for row in band_with_metric if row.ticker in decile),
        # Order returns to the calibrated E[r] rank; the metric decided
        # eligibility only. ``ticker`` breaks ties so the same panel always
        # yields the same name.
        key=lambda row: (row.full_rank or 0, row.ticker),
    )
    if not qualified:
        return _with_status(shared, "no_qualified_candidate")
    chosen = qualified[0]
    return ExplorationSelection(
        status="available",
        ticker=chosen.ticker,
        source_rank=chosen.full_rank,
        normalized_per_3fy=chosen.normalized_per_3fy,
        good_decile_cutoff=cutoff,
        good_decile_size=len(decile),
        decile_population_size=decile_population_size,
        band_metric_count=len(band_with_metric),
    )


def _with_status(base: ExplorationSelection, status: ExplorationStatus) -> ExplorationSelection:
    return ExplorationSelection(
        status=status,
        good_decile_cutoff=base.good_decile_cutoff,
        good_decile_size=base.good_decile_size,
        decile_population_size=base.decile_population_size,
        band_metric_count=base.band_metric_count,
    )


def _good_decile(
    rows: Sequence[ExplorationCandidate],
) -> tuple[frozenset[str], float | None, int]:
    """The cheapest tenth of the liquidity population on the axis.

    The population is the one the axis was measured on — every liquidity-passing
    row with a positive multiple — not the ranked subset, so the cutoff does not
    move with how many names happened to carry an E[r] that month.
    """
    pool = sorted(
        (value, row.ticker)
        for row in rows
        if row.in_population and (value := _positive_metric(row)) is not None
    )
    if not pool:
        return frozenset(), None, 0
    size = math.ceil(len(pool) / GOOD_DECILE_DIVISOR)
    head = pool[:size]
    return frozenset(ticker for _, ticker in head), head[-1][0], len(pool)
