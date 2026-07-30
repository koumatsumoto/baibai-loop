"""Compare what the OP3 gate selected against what it rejected, after the fact.

The improvement loop measures the machine estimate against realised prices and the
few holdings against their own outcome. The judgment between them — which names got
a research slot — accumulates roughly twenty structured decisions per cycle and has
never been compared to anything. This module supplies that comparison.

The population is the shortlist's own entries: selected and rejected together are the
pool the human reviewed, so the benchmark needs no other store and no re-derivation of
what counted as a candidate. Within that pool three cohorts are read — the selected
names, the rejected names, and the machine's own top-N by E[r] taken at the same size
as the selection — so the question "did the judgment beat the ranking it started
from" has an answer rather than an impression.

The comparison is descriptive. Assignment to selected and rejected is not random (it
correlates with E[r], durability and the disclosure scan), cohort windows overlap, and
the sample is small; none of that is fixed by arithmetic, so the payload carries the
counts and the caller states the limitation rather than claiming an effect.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from statistics import median

from .calibration.forward import ForwardReturnRow

# The ploss vocabulary the OP3 narrative uses, ordered from the least to the most
# concerning so a report reads down the scale.
PLOSS_ORDER: tuple[str, ...] = ("低", "中低", "中", "要精査", "高")


@dataclass(frozen=True, slots=True, kw_only=True)
class ShortlistJudgment:
    """One ticker's OP3 verdict, with the machine rank it was judged against."""

    ticker: str
    decision: str
    ploss: str | None
    catalyst_date: date | None
    er_annual: float | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ShortlistCohort:
    """One shortlist's judgments and the run they were made from."""

    shortlist_id: str
    as_of: date
    run_revision_id: str
    judgments: tuple[ShortlistJudgment, ...]

    @property
    def selected(self) -> tuple[ShortlistJudgment, ...]:
        return tuple(item for item in self.judgments if item.decision == "selected")

    @property
    def rejected(self) -> tuple[ShortlistJudgment, ...]:
        return tuple(item for item in self.judgments if item.decision == "rejected")


def cohort_from_payload(payload: Mapping[str, object]) -> ShortlistCohort | None:
    """Read one published shortlist into its judgments, or None when it is unusable."""

    entries = payload.get("entries")
    shortlist_id = payload.get("shortlist_id")
    as_of = payload.get("as_of")
    run_revision_id = payload.get("run_revision_id")
    if not isinstance(entries, list) or not shortlist_id or not as_of or not run_revision_id:
        return None
    judgments: list[ShortlistJudgment] = []
    for entry in entries:
        if not isinstance(entry, Mapping) or not entry.get("ticker"):
            continue
        narrative = entry.get("narrative")
        narrative = narrative if isinstance(narrative, Mapping) else {}
        judgments.append(
            ShortlistJudgment(
                ticker=str(entry["ticker"]),
                decision=str(entry.get("decision", "")),
                ploss=_optional_str(narrative.get("ploss")),
                catalyst_date=_optional_date(narrative.get("catalyst_date")),
                er_annual=_optional_float(entry.get("er_annual")),
            )
        )
    if not judgments:
        return None
    return ShortlistCohort(
        shortlist_id=str(shortlist_id),
        as_of=_require_date(as_of),
        run_revision_id=str(run_revision_id),
        judgments=tuple(judgments),
    )


def with_machine_estimates(
    cohort: ShortlistCohort, er_by_ticker: Mapping[str, float]
) -> ShortlistCohort:
    """Fill in the machine E[r] for judgments published before it was burned in.

    A shortlist published now carries the estimate it was judged against. An older one
    does not, and the run it bound is usually gone by the time the horizon matures, so
    this fallback only helps while that run survives. An estimate already on the
    judgment always wins: it is what the judgment saw.
    """

    return ShortlistCohort(
        shortlist_id=cohort.shortlist_id,
        as_of=cohort.as_of,
        run_revision_id=cohort.run_revision_id,
        judgments=tuple(
            ShortlistJudgment(
                ticker=item.ticker,
                decision=item.decision,
                ploss=item.ploss,
                catalyst_date=item.catalyst_date,
                er_annual=item.er_annual
                if item.er_annual is not None
                else er_by_ticker.get(item.ticker),
            )
            for item in cohort.judgments
        ),
    )


def _cohort_summary(
    tickers: Sequence[str], returns: Mapping[str, float], benchmark: float
) -> dict[str, object]:
    """Summarise one cohort, keeping "no names" distinct from "no prices".

    ``n`` is always the cohort's size. Collapsing it to zero when nothing resolved
    would report a cohort of eight unpriced names the same as an empty one, which is
    the coverage gap the honesty discipline requires to be counted rather than hidden.
    """

    resolved = [returns[ticker] for ticker in tickers if ticker in returns]
    return {
        "n": len(tickers),
        "resolved": len(resolved),
        "unresolved": len(tickers) - len(resolved),
        "median_return_pct": round(median(resolved) * 100, 1) if resolved else None,
        "median_excess_pct": (round((median(resolved) - benchmark) * 100, 1) if resolved else None),
    }


def _machine_basis(cohort: ShortlistCohort, ranked: Sequence[str]) -> str:
    """Say why the machine cohort is what it is, without guessing a cause.

    An empty cohort has several possible causes and they send a reader somewhere
    different: a cycle that selected nothing is a normal outcome, while a judgment
    whose estimates are gone is a measurement gap.
    """

    if not cohort.selected:
        return "no_selection"
    if not ranked:
        return "estimate_missing"
    return "judgment_estimate"


def _unresolved_reasons(
    rows: Sequence[ForwardReturnRow], tickers: Sequence[str]
) -> dict[str, object]:
    """Count why observations are missing, split the way the calibration gate splits them.

    A name that leaves the market inside the window — a buyout, a delisting — drops out
    of both the cohort and the benchmark without saying so, and a premium buyout is a
    good outcome that happens to selected names. Counting the classes keeps that
    exclusion visible instead of quietly pulling the selected cohort down.
    """

    wanted = set(tickers)
    unresolved = [row for row in rows if row.ticker in wanted and not row.resolved]
    counts: dict[str, int] = {}
    for row in unresolved:
        counts[row.status] = counts.get(row.status, 0) + 1
    observed = {row.ticker for row in rows if row.ticker in wanted}
    return {
        "unresolved_count": len(unresolved),
        "unresolved_reason_counts": counts,
        # A name the store never answered for at all is a different gap from one it
        # answered "no price" for.
        "not_observed_count": len(wanted - observed),
        "unpriced_exit_count": sum(
            1
            for row in unresolved
            if row.status in {"unresolved_missing_exit", "unresolved_stale_exit"}
        ),
        "adjustment_factor_coverage": _adjustment_coverage(
            [row for row in rows if row.ticker in wanted]
        ),
    }


def _adjustment_coverage(rows: Sequence[ForwardReturnRow]) -> str:
    values = {str(row.adjustment_factor_coverage) for row in rows}
    if not values or "unknown" in values:
        return "unknown"
    return "complete" if values == {"complete"} else "incomplete"


def evaluate_cohort(
    cohort: ShortlistCohort,
    forward_rows: Sequence[ForwardReturnRow],
    *,
    horizon: str,
    drawdowns: Mapping[str, float] | None = None,
    drawdown_window_end: date | None = None,
) -> dict[str, object]:
    """Compare the selected, rejected and machine cohorts over one horizon.

    The drawdown block is reported whether or not the return has matured: how far a
    name has fallen so far is already observable, and it is the quantity the
    permanent-loss judgment was about. ``drawdown_window_end`` states how much of the
    horizon that covers so a partial window is not read as the whole one.
    """

    resolved = {
        row.ticker: row.price_return
        for row in forward_rows
        if row.horizon == horizon and row.resolved and row.price_return is not None
    }
    pool = [item.ticker for item in cohort.judgments]
    pool_resolved = [resolved[ticker] for ticker in pool if ticker in resolved]
    horizon_rows = [row for row in forward_rows if row.horizon == horizon]
    coverage = _unresolved_reasons(horizon_rows, pool)
    machine_ranked = [
        item.ticker
        for item in sorted(
            (item for item in cohort.judgments if item.er_annual is not None),
            key=lambda item: (-(item.er_annual or 0.0), item.ticker),
        )
    ]
    machine_basis = _machine_basis(cohort, machine_ranked)
    drawdown_block: dict[str, object] = {}
    if drawdowns:
        drawdown_block = {
            "drawdown_window_end": (
                None if drawdown_window_end is None else drawdown_window_end.isoformat()
            ),
            "ploss": _ploss_summary(cohort, drawdowns),
        }
    if not pool_resolved:
        return {
            "shortlist_id": cohort.shortlist_id,
            "as_of": cohort.as_of.isoformat(),
            "horizon": horizon,
            "status": "unresolved",
            "pool_size": len(pool),
            "resolved": 0,
            "machine_basis": machine_basis,
            **coverage,
            **drawdown_block,
        }
    benchmark = median(pool_resolved)
    selected = [item.ticker for item in cohort.selected]
    machine = machine_ranked[: len(selected)]
    payload: dict[str, object] = {
        "shortlist_id": cohort.shortlist_id,
        "as_of": cohort.as_of.isoformat(),
        "horizon": horizon,
        "status": "resolved",
        "pool_size": len(pool),
        "pool_median_return_pct": round(benchmark * 100, 1),
        "selected": _cohort_summary(selected, resolved, benchmark),
        "rejected": _cohort_summary([item.ticker for item in cohort.rejected], resolved, benchmark),
        # The machine cohort takes the same number of names the judgment took, so the
        # two are answering the same question at the same size.
        "machine_top_n": _cohort_summary(machine, resolved, benchmark),
        "machine_basis": machine_basis,
        # The counterfactual only answers the same question when it takes the same
        # number of names; a short one is a different comparison, not a smaller one.
        "machine_matches_selected_size": len(machine) == len(selected),
        **coverage,
    }
    payload.update(drawdown_block)
    return payload


def _ploss_summary(
    cohort: ShortlistCohort, drawdowns: Mapping[str, float]
) -> list[dict[str, object]]:
    """Group the realised drawdown by the permanent-loss category the narrative gave.

    Only selected names carry a narrative, so the scale is measured on the cohort that
    was actually judged on it.
    """

    by_category: dict[str, list[float]] = {}
    for item in cohort.selected:
        if item.ploss is None or item.ticker not in drawdowns:
            continue
        by_category.setdefault(item.ploss, []).append(drawdowns[item.ticker])
    ordered = [name for name in PLOSS_ORDER if name in by_category]
    ordered.extend(sorted(set(by_category) - set(PLOSS_ORDER)))
    return [
        {
            "ploss": name,
            "n": len(by_category[name]),
            "median_drawdown_pct": round(median(by_category[name]) * 100, 1),
            "worst_drawdown_pct": round(min(by_category[name]) * 100, 1),
        }
        for name in ordered
    ]


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _require_date(value: object) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))
