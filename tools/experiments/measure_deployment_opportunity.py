"""Measure opportunity-cycle counterfactuals and book-cost deployment pace."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from pathlib import Path
from typing import TextIO
from zoneinfo import ZoneInfo

import yaml

from baibai_engine.position.ledger import (
    PortfolioLedgerDocument,
    replay_events_through,
    replayed_deployed_cost_yen,
)
from baibai_engine.read_api.assessment import list_bargain_assessment_payloads
from baibai_engine.read_api.operations import list_operation_sessions
from baibai_engine.read_api.position import portfolio_ledger_document
from baibai_engine.read_api.shortlist import list_shortlist_payloads
from baibai_engine.screening.calibration.forward import compute_forward_returns
from baibai_engine.screening.calibration.horizons import require_horizon
from baibai_engine.screening.run_store import ScreeningRunReader, run_store_path
from baibai_engine.screening.shortlist_outcome import (
    ShortlistCohort,
    cohort_from_payload,
    evaluate_machine_counterfactual,
    with_machine_estimates,
)
from baibai_engine.screening.store_readiness import unreadable_store_reason

JST = ZoneInfo("Asia/Tokyo")
DEFAULT_START = date(2026, 5, 1)
DEFAULT_HORIZONS: tuple[str, ...] = ("3m", "6m", "1y", "3y")
MACHINE_TOP_N = 1


class DeploymentMeasurementError(ValueError):
    """Raised when canonical inputs cannot support a truthful measurement."""


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an ISO date") from error


def _references(operation: Mapping[str, object]) -> tuple[str, ...]:
    payload = operation.get("payload")
    if not isinstance(payload, Mapping):
        return ()
    raw = payload.get("canonical_refs")
    if not isinstance(raw, list):
        return ()
    return tuple(str(item) for item in raw if isinstance(item, str))


def _bound_payloads(
    references: Sequence[str],
    payloads: Sequence[Mapping[str, object]],
    *,
    id_field: str,
) -> list[Mapping[str, object]]:
    return [
        payload
        for payload in payloads
        if payload.get(id_field)
        and any(_reference_mentions(reference, str(payload[id_field])) for reference in references)
    ]


def _reference_mentions(reference: str, identifier: str) -> bool:
    return (
        re.search(
            rf"(?<![0-9A-Za-z-]){re.escape(identifier)}(?![0-9A-Za-z-])",
            reference,
        )
        is not None
    )


def _legacy_conclusion(operation: Mapping[str, object]) -> str:
    payload = operation.get("payload")
    raw = payload.get("result") if isinstance(payload, Mapping) else None
    if not isinstance(raw, str):
        return "unknown"
    normalized = " ".join(raw.lower().replace("_", " ").split())
    if normalized.startswith("no actionable bargain"):
        return "no_action"
    if normalized.startswith("defer"):
        return "defer"
    if normalized.startswith(("deploy", "proposal")):
        return "deploy"
    return "unknown"


def _conclusion(
    operation: Mapping[str, object], assessments: Sequence[Mapping[str, object]]
) -> tuple[str, str, Mapping[str, object] | None]:
    if operation.get("status") != "completed":
        return "unknown", "operation_incomplete", None
    bound = _bound_payloads(_references(operation), assessments, id_field="assessment_id")
    if len(bound) > 1:
        return "unknown", "assessment_binding_ambiguous", None
    if len(bound) == 1:
        result = bound[0].get("result")
        mapped = {
            "proposal": "deploy",
            "defer": "defer",
            "no_actionable_bargain": "no_action",
        }.get(str(result), "unknown")
        return mapped, "bargain_assessment", bound[0]
    return _legacy_conclusion(operation), "operation_result_legacy", None


def _bound_shortlist(
    operation: Mapping[str, object],
    assessment: Mapping[str, object] | None,
    shortlists: Sequence[Mapping[str, object]],
) -> tuple[Mapping[str, object] | None, str]:
    if assessment is not None:
        shortlist_id = assessment.get("shortlist_id")
        matches = [item for item in shortlists if item.get("shortlist_id") == shortlist_id]
    else:
        matches = _bound_payloads(_references(operation), shortlists, id_field="shortlist_id")
    if not matches:
        return None, "shortlist_binding_missing"
    if len(matches) > 1:
        return None, "shortlist_binding_ambiguous"
    return matches[0], "bound"


def _machine_estimates(runs_db_path: Path | None, run_revision_id: str) -> dict[str, float]:
    resolved = run_store_path(runs_db_path)
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
        value = metrics.get("er_annual") if isinstance(metrics, Mapping) else None
        ticker = candidate.get("ticker")
        if ticker is not None and isinstance(value, int | float) and not isinstance(value, bool):
            estimates[str(ticker)] = float(value)
    return estimates


def _cohort(
    shortlist: Mapping[str, object], runs_db_path: Path | None
) -> tuple[ShortlistCohort | None, str]:
    cohort = cohort_from_payload(shortlist)
    entries = shortlist.get("entries")
    if cohort is None or not isinstance(entries, list) or len(cohort.judgments) != len(entries):
        return None, "shortlist_unreadable"
    embedded = sum(item.er_annual is not None for item in cohort.judgments)
    if embedded == len(cohort.judgments):
        return cohort, "shortlist_burned_in"
    estimates = _machine_estimates(runs_db_path, cohort.run_revision_id)
    cohort = with_machine_estimates(cohort, estimates)
    completed = sum(item.er_annual is not None for item in cohort.judgments)
    return cohort, "run_store_fallback" if completed == len(
        cohort.judgments
    ) else "estimate_missing"


def _instant(operation: Mapping[str, object]) -> datetime:
    raw = operation.get("completed_at") or operation.get("started_at")
    if raw is None:
        raise DeploymentMeasurementError("operation has no observation instant")
    parsed = datetime.fromisoformat(str(raw))
    if parsed.utcoffset() is None:
        raise DeploymentMeasurementError("operation instant must include a timezone")
    return parsed


def _capital_snapshot(ledger: PortfolioLedgerDocument, instant: datetime) -> dict[str, object]:
    state = replay_events_through(ledger.events, instant)
    deployed = replayed_deployed_cost_yen(state)
    cash_total = state.available_cash_yen + state.reserved_cash_yen
    book_capital = deployed + cash_total
    return {
        "observed_at": instant.isoformat(),
        "available_cash_yen": state.available_cash_yen,
        "reserved_cash_yen": state.reserved_cash_yen,
        "cash_total_yen": cash_total,
        "deployed_cost_yen": deployed,
        "book_capital_yen": book_capital,
        "deployment_ratio_pct": (
            None if book_capital <= 0 else round(deployed * 100 / book_capital, 2)
        ),
    }


def _cash_timeline(ledger: PortfolioLedgerDocument, start: date) -> list[dict[str, object]]:
    event_days = sorted(
        {
            event.occurred_at.astimezone(JST).date()
            for event in ledger.events
            if event.occurred_at.astimezone(JST).date() >= start
        }
    )
    return [
        {
            "date": day.isoformat(),
            **_capital_snapshot(
                ledger,
                datetime.combine(day, time.max, tzinfo=JST),
            ),
        }
        for day in event_days
    ]


def _counterfactual_status(cycle: Mapping[str, object]) -> str:
    counterfactual = cycle.get("counterfactual")
    if not isinstance(counterfactual, Mapping):
        return "missing"
    return str(counterfactual.get("status", "missing"))


def build_measurement(
    *,
    operations: Sequence[Mapping[str, object]],
    shortlists: Sequence[Mapping[str, object]],
    assessments: Sequence[Mapping[str, object]],
    ledger: PortfolioLedgerDocument,
    runs_db_path: Path | None,
    market_db_path: Path,
    start: date = DEFAULT_START,
    horizons: Sequence[str] = DEFAULT_HORIZONS,
) -> dict[str, object]:
    """Build the deterministic measurement without dropping unknown cycles."""

    selected_horizons = list(horizons)
    for horizon in selected_horizons:
        require_horizon(horizon)
    opportunities = sorted(
        (
            item
            for item in operations
            if item.get("session_kind") == "opportunity"
            and date.fromisoformat(str(item["as_of"])) >= start
        ),
        key=lambda item: (str(item["as_of"]), str(item.get("started_at", ""))),
    )
    cycles: list[dict[str, object]] = []
    for operation in opportunities:
        conclusion, conclusion_source, assessment = _conclusion(operation, assessments)
        shortlist, binding_status = _bound_shortlist(operation, assessment, shortlists)
        instant = _instant(operation)
        cycle: dict[str, object] = {
            "operation_id": str(operation.get("operation_id", "")),
            "as_of": str(operation["as_of"]),
            "status": str(operation.get("status", "")),
            "conclusion": conclusion,
            "conclusion_source": conclusion_source,
            "shortlist_binding_status": binding_status,
            "shortlist_id": None if shortlist is None else shortlist.get("shortlist_id"),
            "capital": _capital_snapshot(ledger, instant),
        }
        if shortlist is None:
            cycle["counterfactual"] = {"status": binding_status}
            cycles.append(cycle)
            continue
        cohort, estimate_source = _cohort(shortlist, runs_db_path)
        if cohort is None:
            cycle["counterfactual"] = {"status": estimate_source}
            cycles.append(cycle)
            continue
        forward_rows = compute_forward_returns(
            market_db_path,
            asofs=[cohort.as_of],
            tickers=[item.ticker for item in cohort.judgments],
            horizons=selected_horizons,
        )
        cycle["counterfactual"] = {
            "status": estimate_source,
            "machine_top_n": MACHINE_TOP_N,
            "horizons": [
                evaluate_machine_counterfactual(
                    cohort,
                    forward_rows,
                    horizon=horizon,
                    top_n=MACHINE_TOP_N,
                )
                for horizon in selected_horizons
            ],
        }
        cycles.append(cycle)

    earliest = min((date.fromisoformat(str(item["as_of"])) for item in opportunities), default=None)
    ledger_start = min(
        (event.occurred_at.astimezone(JST).date() for event in ledger.events),
        default=None,
    )
    return {
        "kind": "deployment-opportunity-cost",
        "measurement_start": start.isoformat(),
        "metric_basis": "price_return_only",
        "comparison_basis": "descriptive_non_random_assignment",
        "cash_benchmark_return_pct": 0.0,
        "machine_top_n": MACHINE_TOP_N,
        "horizons": selected_horizons,
        "capital_basis": "fifo_open_lot_cost_plus_available_and_reserved_cash",
        "coverage": {
            "opportunity_cycle_count": len(opportunities),
            "unknown_conclusion_count": sum(item["conclusion"] == "unknown" for item in cycles),
            "counterfactual_unavailable_count": sum(
                _counterfactual_status(item)
                in {
                    "shortlist_binding_missing",
                    "shortlist_binding_ambiguous",
                    "shortlist_unreadable",
                    "estimate_missing",
                }
                for item in cycles
            ),
            "operation_record_start": None if earliest is None else earliest.isoformat(),
            "pre_operation_record_period": (
                None
                if earliest is None or earliest <= start
                else {
                    "start": start.isoformat(),
                    "end": date.fromordinal(earliest.toordinal() - 1).isoformat(),
                    "status": "unavailable_not_inferred_from_ledger",
                }
            ),
            "ledger_record_start": None if ledger_start is None else ledger_start.isoformat(),
            "pre_ledger_record_period": (
                None
                if ledger_start is None or ledger_start <= start
                else {
                    "start": start.isoformat(),
                    "end": date.fromordinal(ledger_start.toordinal() - 1).isoformat(),
                    "status": "unavailable_before_opening_balance",
                }
            ),
        },
        "cycles": cycles,
        "cash_timeline": _cash_timeline(ledger, start),
    }


def measure_command(
    *,
    db_path: Path,
    runs_db_path: Path | None,
    market_db_path: Path,
    start: date,
    horizons: Sequence[str],
    output_path: Path | None,
    stdout: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    unreadable = unreadable_store_reason(market_db_path)
    if unreadable is not None:
        print(f"deployment-opportunity: {unreadable}", file=sys.stderr)
        return 1
    ledger = portfolio_ledger_document(db_path)
    if ledger is None:
        print("deployment-opportunity: canonical ledger is unavailable", file=sys.stderr)
        return 1
    try:
        payload = build_measurement(
            operations=list_operation_sessions(db_path),
            shortlists=list_shortlist_payloads(db_path),
            assessments=list_bargain_assessment_payloads(db_path),
            ledger=ledger,
            runs_db_path=runs_db_path,
            market_db_path=market_db_path,
            start=start,
            horizons=horizons,
        )
    except (DeploymentMeasurementError, ValueError) as error:
        print(f"deployment-opportunity: {error}", file=sys.stderr)
        return 1
    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    if output_path is None:
        print(text, file=out)
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
        print(f"deployment-opportunity: wrote {output_path}", file=out)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("stores/application/baibai.sqlite"))
    parser.add_argument("--runs-db", type=Path, default=Path("stores/screening/runs.sqlite"))
    parser.add_argument("--market-db", type=Path, default=Path("stores/market/market.sqlite"))
    parser.add_argument("--start", type=_parse_date, default=DEFAULT_START)
    parser.add_argument("--horizon", action="append", dest="horizons")
    parser.add_argument("--out", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return measure_command(
        db_path=args.db,
        runs_db_path=args.runs_db,
        market_db_path=args.market_db,
        start=args.start,
        horizons=args.horizons or DEFAULT_HORIZONS,
        output_path=args.out,
    )


if __name__ == "__main__":
    raise SystemExit(main())
