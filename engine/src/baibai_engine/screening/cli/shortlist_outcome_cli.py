"""Emit the OP3 judgment cohort comparison as a deterministic YAML payload."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import TextIO

import yaml

from baibai_engine.foundation.time import JST
from baibai_engine.read_api.market import latest_market_bar_date, worst_close_drawdown
from baibai_engine.read_api.shortlist import list_shortlist_payloads
from baibai_engine.screening.calibration.forward import (
    compute_forward_returns,
    read_control_event_exits,
    read_failure_exits,
)
from baibai_engine.screening.calibration.horizons import require_horizon
from baibai_engine.screening.run_store import ScreeningRunReader, run_store_path
from baibai_engine.screening.shortlist_outcome import (
    ShortlistCohort,
    cohort_from_payload,
    evaluate_cohort,
    with_machine_estimates,
)
from baibai_engine.screening.store_readiness import unreadable_store_reason

# The horizons the loop already reasons in. Anything under three months is the short
# screen performance the doctrine rules out as an optimisation target, so it is not
# offered here either.
DEFAULT_HORIZONS: tuple[str, ...] = ("3m", "6m", "1y", "3y")


def _machine_estimates(runs_db: Path | None, run_revision_id: str) -> dict[str, float]:
    """Read the machine E[r] of one run, or nothing when that run is gone.

    The run store keeps a few generations, so an older shortlist's run is often
    pruned. Returning nothing makes the machine cohort report as unresolved instead of
    being filled from whatever run is still there.
    """

    resolved = run_store_path(runs_db)
    if not resolved.is_file():
        return {}
    try:
        run = ScreeningRunReader(resolved).get_run(run_revision_id)
    except (KeyError, LookupError, ValueError):
        return {}
    if run is None:
        return {}
    estimates: dict[str, float] = {}
    for candidate in run.candidates:
        metrics = candidate.get("metrics")
        value = metrics.get("er_annual") if isinstance(metrics, dict) else None
        ticker = candidate.get("ticker")
        if ticker is None or not isinstance(value, int | float):
            continue
        estimates[str(ticker)] = float(value)
    return estimates


def shortlist_outcome_command(
    *,
    db_path: Path,
    runs_db_path: Path | None,
    sqlite_path: Path,
    horizons: list[str] | None = None,
    output_path: Path | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Compare every published shortlist's cohorts against the pool it drew from."""

    out = stdout if stdout is not None else sys.stdout
    selected_horizons = list(horizons or DEFAULT_HORIZONS)
    for name in selected_horizons:
        require_horizon(name)
    # Every price this comparison rests on comes from the market store. A store the
    # readers cannot open yields no bar date, which drops the drawdown block out of
    # the payload entirely -- a cohort that never fell and a cohort nobody measured
    # would then look the same.
    unreadable = unreadable_store_reason(sqlite_path)
    if unreadable is not None:
        print(f"shortlist-outcome: {unreadable}", file=sys.stderr)
        return 1
    payloads = list_shortlist_payloads(db_path)
    cohorts: list[ShortlistCohort] = []
    for payload in payloads:
        cohort = cohort_from_payload(payload)
        if cohort is None:
            continue
        cohorts.append(
            with_machine_estimates(cohort, _machine_estimates(runs_db_path, cohort.run_revision_id))
        )
    if not cohorts:
        print("shortlist-outcome: no published shortlist to evaluate", file=sys.stderr)
        return 1

    observed_end = latest_market_bar_date(sqlite_path)
    # A shortlisted name that was taken over is an outcome the judgment owns, so it is
    # resolved by the price the offer paid rather than counted as a coverage gap.
    control_event_exits = read_control_event_exits(sqlite_path)
    failure_exits = read_failure_exits(sqlite_path)
    results: list[dict[str, object]] = []
    for cohort in cohorts:
        tickers = [item.ticker for item in cohort.judgments]
        forward_rows = compute_forward_returns(
            sqlite_path,
            asofs=[cohort.as_of],
            tickers=tickers,
            horizons=selected_horizons,
            control_event_exits=control_event_exits,
            failure_exits=failure_exits,
        )
        for horizon in selected_horizons:
            spec = require_horizon(horizon)
            # The window ends where the data ends, not where the calendar does: a
            # store that stopped updating would otherwise report "no fall in three
            # months" from a few weeks of prices.
            requested_end = min(spec.target_date(cohort.as_of), datetime.now(JST).date())
            window_end = min(requested_end, observed_end) if observed_end else None
            drawdowns = (
                worst_close_drawdown(sqlite_path, tickers, start=cohort.as_of, end=window_end)
                if window_end is not None
                else {}
            )
            results.append(
                evaluate_cohort(
                    cohort,
                    forward_rows,
                    horizon=horizon,
                    drawdowns=drawdowns,
                    drawdown_window_end=window_end,
                )
            )

    payload = {
        "kind": "shortlist-judgment-outcome",
        "metric_basis": "price_return_only",
        # Selection is not random assignment, the windows overlap and the sample is
        # small. The payload states this so a reader of the numbers alone still sees it.
        "comparison_basis": "descriptive_non_random_assignment",
        "cohort_count": len(cohorts),
        "judgment_count": sum(len(cohort.judgments) for cohort in cohorts),
        "horizons": selected_horizons,
        "results": results,
    }
    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
        print(f"shortlist-outcome: wrote {output_path}", file=out)
    else:
        print(text, file=out)
    return 0
