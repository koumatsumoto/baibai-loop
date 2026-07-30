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
                er_annual=None,
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
    """Attach the machine E[r] the judgments were made against.

    The estimate lives in the run the shortlist bound, and the run store keeps only
    the newest generations. A cohort whose run has been pruned keeps ``None`` and the
    machine comparison for it reports as unresolved rather than being computed from a
    different run's numbers.
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
                er_annual=er_by_ticker.get(item.ticker, item.er_annual),
            )
            for item in cohort.judgments
        ),
    )


def _cohort_summary(
    tickers: Sequence[str], returns: Mapping[str, float], benchmark: float
) -> dict[str, object]:
    resolved = [returns[ticker] for ticker in tickers if ticker in returns]
    if not resolved:
        return {"n": 0, "resolved": 0, "median_return_pct": None, "median_excess_pct": None}
    return {
        "n": len(tickers),
        "resolved": len(resolved),
        "median_return_pct": round(median(resolved) * 100, 1),
        "median_excess_pct": round((median(resolved) - benchmark) * 100, 1),
    }


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
            **drawdown_block,
        }
    benchmark = median(pool_resolved)
    selected = [item.ticker for item in cohort.selected]
    machine_ranked = [
        item.ticker
        for item in sorted(
            (item for item in cohort.judgments if item.er_annual is not None),
            key=lambda item: (-(item.er_annual or 0.0), item.ticker),
        )
    ]
    machine = machine_ranked[: len(selected)] if machine_ranked else []
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
        "machine_top_n": (
            _cohort_summary(machine, resolved, benchmark)
            if machine
            else {"n": 0, "resolved": 0, "median_return_pct": None, "median_excess_pct": None}
        ),
        "machine_basis": "run_estimate" if machine else "unresolved_pruned_run",
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
