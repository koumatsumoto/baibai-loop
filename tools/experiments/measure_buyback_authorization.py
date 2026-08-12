"""Measure trailing share change against point-in-time buyback authorization state.

This is a read-only diagnostic panel join.  It combines the versioned calibration
panel/forward rows with form-220 filings that were public by each cohort as-of, then
compares three estimates of the same buyback carry component:

* trailing share-count change alone (the production component),
* authorization acquisition pace alone, and
* a composition that removes ended carry and admits an active pace not yet in YoY.

The artifact cannot change E[r], ranking, or screening rules.  Complete 3y and 5y
point-in-time cohorts are necessary source prerequisites, not production authority;
adoption still requires the repository's generic calibration decision process.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import median
from typing import Literal, TextIO

import yaml

from baibai_engine.screening.buyback_authorization import (
    BuybackAuthorization,
    build_buyback_authorization,
    read_buyback_status_filings,
    with_authorization_state,
)
from baibai_engine.screening.buyback_report import (
    BuybackDispositionPurpose,
    BuybackReportError,
    parse_buyback_report,
)
from baibai_engine.screening.buyback_store import StoredBuybackReport, read_buyback_reports
from baibai_engine.screening.calibration.forward import RESOLVED_STATUSES

from .measure_signal_cohorts import require_single_rules_hash

DEFAULT_CALIBRATION_DIR = Path("stores/screening/calibration")
DEFAULT_MARKET_SQLITE = Path("stores/market/market.sqlite")
DEFAULT_EDINET_ZIP_DIR = Path(".cache/screening/edinet/csv_zips")
DEFAULT_HORIZONS = ("3m", "6m", "1y", "3y", "5y")
HORIZON_YEARS: Mapping[str, float] = {
    "3m": 0.25,
    "6m": 0.5,
    "1y": 1.0,
    "3y": 3.0,
    "5y": 5.0,
}
HORIZON_AUTHORITY: Mapping[str, str] = {
    "3m": "regression_alert",
    "6m": "regression_alert",
    "1y": "leading_evidence",
    "3y": "production_decision_evidence",
    "5y": "production_decision_evidence",
}
MIN_MARKET_CAP_OKU = 100.0
MIN_AVG_TURNOVER_OKU = 1.0
MIN_LISTING_SPAN_DAYS = 182
MIN_GROUP_ROWS = 10
BUYBACK_CLIP = 0.05
POSITIVE_SIGNAL_FLOOR = 0.005

type AuthorizationState = Literal[
    "active",
    "ended",
    "no_filing",
    "filing_state_unresolved",
    "unknown",
]
type PurposeCoverage = Literal["complete", "partial", "unavailable", "not_applicable"]


class BuybackAuthorizationMeasurementError(ValueError):
    """The local stores cannot support a truthful point-in-time comparison."""


@dataclass(frozen=True, slots=True, kw_only=True)
class DiagnosticRow:
    asof: str
    ticker: str
    share_change_signal: float | None
    authorization_signal: float | None
    composition_signal: float | None
    authorization_state: AuthorizationState
    purpose_coverage: PurposeCoverage
    purposes: tuple[BuybackDispositionPurpose, ...]
    forward: Mapping[str, float]


@dataclass(frozen=True, slots=True, kw_only=True)
class _BasePanelRow:
    asof: str
    ticker: str
    share_change_signal: float | None


def _optional_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _annualized(cumulative_return: float, horizon: str) -> float:
    return float((1.0 + cumulative_return) ** (1.0 / HORIZON_YEARS[horizon]) - 1.0)


def _load_panel(calibration_dir: Path) -> Mapping[str, tuple[_BasePanelRow, ...]]:
    rows: dict[str, list[_BasePanelRow]] = defaultdict(list)
    identities: set[tuple[str, str]] = set()
    for path in sorted(calibration_dir.glob("panel-*.csv")):
        asof = path.name.removeprefix("panel-").removesuffix(".csv")
        with path.open(newline="", encoding="utf-8") as handle:
            for raw in csv.DictReader(handle):
                raw_asof = raw.get("asof") or ""
                ticker = raw.get("ticker") or ""
                if raw_asof != asof:
                    raise BuybackAuthorizationMeasurementError(
                        f"panel asof does not match filename: {path}: {raw_asof!r}"
                    )
                identity = (asof, ticker)
                if not ticker or identity in identities:
                    raise BuybackAuthorizationMeasurementError(
                        f"invalid or duplicate panel identity: {identity!r}"
                    )
                identities.add(identity)
                market_cap = _optional_float(raw.get("market_cap_oku"))
                turnover = _optional_float(raw.get("avg_turnover_oku"))
                listing_span = _optional_float(raw.get("listing_span_days"))
                if (
                    market_cap is None
                    or market_cap < MIN_MARKET_CAP_OKU
                    or turnover is None
                    or turnover < MIN_AVG_TURNOVER_OKU
                    or listing_span is None
                    or listing_span < MIN_LISTING_SPAN_DAYS
                ):
                    continue
                share_change = _optional_float(raw.get("net_share_change_yoy"))
                signal = (
                    None
                    if share_change is None
                    else max(-BUYBACK_CLIP, min(BUYBACK_CLIP, -share_change))
                )
                rows[asof].append(
                    _BasePanelRow(
                        asof=asof,
                        ticker=ticker,
                        share_change_signal=signal,
                    )
                )
    if not rows:
        raise BuybackAuthorizationMeasurementError(
            f"no liquidity-passing panel rows under {calibration_dir}"
        )
    return {asof: tuple(items) for asof, items in rows.items()}


def _load_forward(
    calibration_dir: Path, horizons: Sequence[str]
) -> Mapping[tuple[str, str], Mapping[str, float]]:
    wanted = set(horizons)
    forward: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    identities: set[tuple[str, str, str]] = set()
    paths = sorted(calibration_dir.glob("forward-*.csv"))
    if not paths:
        raise BuybackAuthorizationMeasurementError(f"no forward rows under {calibration_dir}")
    for path in paths:
        file_asof = path.name.removeprefix("forward-").removesuffix(".csv")
        with path.open(newline="", encoding="utf-8") as handle:
            for raw in csv.DictReader(handle):
                raw_asof = raw.get("asof") or ""
                ticker = raw.get("ticker") or ""
                horizon = raw.get("horizon") or ""
                if raw_asof != file_asof:
                    raise BuybackAuthorizationMeasurementError(
                        f"forward asof does not match filename: {path}: {raw_asof!r}"
                    )
                identity = (raw_asof, ticker, horizon)
                if not ticker or identity in identities:
                    raise BuybackAuthorizationMeasurementError(
                        f"invalid or duplicate forward identity: {identity!r}"
                    )
                identities.add(identity)
                value = _optional_float(raw.get("price_return"))
                if (
                    horizon not in wanted
                    or raw.get("status") not in RESOLVED_STATUSES
                    or value is None
                ):
                    continue
                forward[(raw_asof, ticker)][horizon] = value
    return forward


def _authorization_state(annotation: BuybackAuthorization, *, asof: date) -> AuthorizationState:
    if annotation.status == "unknown":
        return "unknown"
    if annotation.status == "no_filing":
        return "no_filing"
    if (
        annotation.authorization_window_end is not None
        and annotation.authorization_window_end < asof
    ) or annotation.remaining_share_ratio == 0:
        return "ended"
    if (
        annotation.authorization_window_end is not None
        and annotation.authorization_window_end >= asof
        and annotation.remaining_share_ratio is not None
        and annotation.remaining_share_ratio > 0
    ):
        return "active"
    return "filing_state_unresolved"


def _authorization_signal(
    annotation: BuybackAuthorization, state: AuthorizationState
) -> float | None:
    if state in {"ended", "no_filing"}:
        return 0.0
    if state != "active" or annotation.trailing_3m_acquired_ratio is None:
        return None
    return min(BUYBACK_CLIP, max(0.0, annotation.trailing_3m_acquired_ratio * 4.0))


def _composition_signal(
    trailing: float | None,
    authorization: float | None,
    state: AuthorizationState,
) -> float | None:
    if state in {"ended", "no_filing"}:
        return 0.0
    if state != "active" or authorization is None:
        return None
    return max(authorization, trailing or 0.0)


def _read_purposes(
    reports: Sequence[StoredBuybackReport], *, zip_dir: Path
) -> tuple[PurposeCoverage, tuple[BuybackDispositionPurpose, ...]]:
    if not reports:
        return "not_applicable", ()
    observed = 0
    missing = 0
    purposes: set[BuybackDispositionPurpose] = set()
    for report in reports:
        if report.doc_id is None:
            missing += 1
            continue
        path = zip_dir / f"{report.doc_id}.zip"
        if not path.exists():
            missing += 1
            continue
        try:
            parsed = parse_buyback_report(path.read_bytes())
        except (BuybackReportError, OSError):
            missing += 1
            continue
        if not parsed.disposition_observed:
            missing += 1
            continue
        observed += 1
        purposes.update(parsed.disposition_purposes)
    if observed == 0:
        coverage: PurposeCoverage = "unavailable"
    elif missing:
        coverage = "partial"
    else:
        coverage = "complete"
    order: tuple[BuybackDispositionPurpose, ...] = (
        "cancellation",
        "employee_compensation_esop",
        "other_rerelease",
    )
    return coverage, tuple(item for item in order if item in purposes)


def _join_diagnostic_rows(
    panel: Mapping[str, tuple[_BasePanelRow, ...]],
    forward: Mapping[tuple[str, str], Mapping[str, float]],
    *,
    market_sqlite: Path,
    zip_dir: Path,
) -> list[DiagnosticRow]:
    rows: list[DiagnosticRow] = []
    for asof_text in sorted(panel):
        asof = date.fromisoformat(asof_text)
        base_rows = panel[asof_text]
        tickers = [row.ticker for row in base_rows]
        filing_read = read_buyback_status_filings(market_sqlite, through=asof)
        reports_by_ticker = read_buyback_reports(
            market_sqlite,
            tickers=tickers,
            asof=asof,
            months=12,
        ).reports
        for base in base_rows:
            reports = reports_by_ticker.get(base.ticker, ())
            annotation = with_authorization_state(
                build_buyback_authorization(
                    asof=asof,
                    latest_filing_date=(
                        None
                        if filing_read is None
                        else filing_read.latest_filing_by_ticker.get(base.ticker)
                    ),
                    observed_from=(None if filing_read is None else filing_read.observed_from),
                ),
                reports,
            )
            state = _authorization_state(annotation, asof=asof)
            authorization = _authorization_signal(annotation, state)
            purpose_coverage, purposes = _read_purposes(reports, zip_dir=zip_dir)
            rows.append(
                DiagnosticRow(
                    asof=asof_text,
                    ticker=base.ticker,
                    share_change_signal=base.share_change_signal,
                    authorization_signal=authorization,
                    composition_signal=_composition_signal(
                        base.share_change_signal, authorization, state
                    ),
                    authorization_state=state,
                    purpose_coverage=purpose_coverage,
                    purposes=purposes,
                    forward=forward.get((asof_text, base.ticker), {}),
                )
            )
    return rows


def _resolved_values(rows: Iterable[DiagnosticRow], horizon: str) -> list[float]:
    return [_annualized(row.forward[horizon], horizon) for row in rows if horizon in row.forward]


def _group_summary(
    rows: Sequence[DiagnosticRow],
    horizon: str,
    *,
    label: str,
    population_medians: Mapping[str, float],
) -> Mapping[str, object]:
    values = _resolved_values(rows, horizon)
    excess = [
        _annualized(row.forward[horizon], horizon) - population_medians[row.asof]
        for row in rows
        if horizon in row.forward and row.asof in population_medians
    ]
    return {
        "group": label,
        "eligible_rows": len(rows),
        "resolved_rows": len(values),
        "resolved_cohorts": len({row.asof for row in rows if horizon in row.forward}),
        "median_annualized_return_pct": None if not values else round(median(values) * 100, 2),
        "median_annualized_excess_pct": None if not excess else round(median(excess) * 100, 2),
    }


def _population_medians(rows: Sequence[DiagnosticRow], horizon: str) -> Mapping[str, float]:
    by_asof: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if horizon in row.forward:
            by_asof[row.asof].append(_annualized(row.forward[horizon], horizon))
    return {asof: median(values) for asof, values in by_asof.items() if values}


def _variant_comparison(
    rows: Sequence[DiagnosticRow], horizon: str, *, field: str
) -> Mapping[str, object]:
    # All variants use exactly the same cohort rows.  A row missing any one signal is
    # excluded from every variant rather than changing the sample in its favour.
    common = [
        row
        for row in rows
        if row.share_change_signal is not None
        and row.authorization_signal is not None
        and row.composition_signal is not None
        and horizon in row.forward
    ]
    by_asof: dict[str, list[DiagnosticRow]] = defaultdict(list)
    for row in common:
        by_asof[row.asof].append(row)
    deltas: list[float] = []
    cohort_results: list[dict[str, object]] = []
    positive_rows = nonpositive_rows = 0
    compared = 0
    for asof in sorted(by_asof):
        cohort = by_asof[asof]
        positive = [row for row in cohort if float(getattr(row, field)) > POSITIVE_SIGNAL_FLOOR]
        nonpositive = [row for row in cohort if float(getattr(row, field)) <= POSITIVE_SIGNAL_FLOOR]
        positive_rows += len(positive)
        nonpositive_rows += len(nonpositive)
        if len(positive) < MIN_GROUP_ROWS or len(nonpositive) < MIN_GROUP_ROWS:
            continue
        compared += 1
        positive_median = median(_resolved_values(positive, horizon))
        nonpositive_median = median(_resolved_values(nonpositive, horizon))
        delta = positive_median - nonpositive_median
        deltas.append(delta)
        cohort_results.append(
            {
                "asof": asof,
                "positive_n": len(positive),
                "nonpositive_n": len(nonpositive),
                "positive_median_annualized_pct": round(positive_median * 100, 2),
                "nonpositive_median_annualized_pct": round(nonpositive_median * 100, 2),
                "delta_pct_points": round(delta * 100, 2),
            }
        )
    return {
        "signal": field,
        "common_resolved_rows": len(common),
        "positive_rows": positive_rows,
        "nonpositive_rows": nonpositive_rows,
        "cohorts_compared": compared,
        "cohorts_positive_ahead": sum(delta > 0 for delta in deltas),
        "median_annualized_delta_pct_points": (
            None if not deltas else round(median(deltas) * 100, 2)
        ),
        "cohorts": cohort_results,
    }


def _complete_cohort_count(rows: Sequence[DiagnosticRow], horizon: str) -> int:
    by_asof: dict[str, list[DiagnosticRow]] = defaultdict(list)
    for row in rows:
        by_asof[row.asof].append(row)
    complete = 0
    for cohort in by_asof.values():
        if len(cohort) < MIN_GROUP_ROWS * 2:
            continue
        if not all(
            horizon in row.forward
            and row.authorization_state not in {"unknown", "filing_state_unresolved"}
            and row.purpose_coverage in {"complete", "not_applicable"}
            and row.share_change_signal is not None
            and row.authorization_signal is not None
            and row.composition_signal is not None
            for row in cohort
        ):
            continue
        # A cohort with only one side of a signal is not comparison evidence, even when
        # every source cell is present.  Apply the same minimum used by the report itself.
        if not all(
            sum(float(getattr(row, field)) > POSITIVE_SIGNAL_FLOOR for row in cohort)
            >= MIN_GROUP_ROWS
            and sum(float(getattr(row, field)) <= POSITIVE_SIGNAL_FLOOR for row in cohort)
            >= MIN_GROUP_ROWS
            for field in (
                "share_change_signal",
                "authorization_signal",
                "composition_signal",
            )
        ):
            continue
        complete += 1
    return complete


def _source_coverage(market_sqlite: Path) -> Mapping[str, object]:
    try:
        with sqlite3.connect(market_sqlite) as connection:
            filing = connection.execute(
                "SELECT MIN(doc_date), MAX(doc_date), COUNT(*) FROM edinet_documents "
                "WHERE doc_type_code IN ('220', '230')"
            ).fetchone()
            report = connection.execute(
                "SELECT MIN(filed_on), MAX(filed_on), COUNT(*) FROM edinet_buyback_reports"
            ).fetchone()
    except sqlite3.Error as exc:
        raise BuybackAuthorizationMeasurementError(
            f"buyback source tables are unavailable in {market_sqlite}: {exc}"
        ) from exc
    return {
        "filing_list": {"from": filing[0], "to": filing[1], "rows": filing[2]},
        "parsed_reports": {"from": report[0], "to": report[1], "rows": report[2]},
    }


def build_measurement(
    *,
    calibration_dir: Path,
    market_sqlite: Path,
    edinet_zip_dir: Path,
    horizons: Sequence[str],
) -> Mapping[str, object]:
    for horizon in horizons:
        if horizon not in HORIZON_YEARS:
            raise BuybackAuthorizationMeasurementError(f"unsupported horizon: {horizon}")
    rules_hash = require_single_rules_hash(calibration_dir)
    panel = _load_panel(calibration_dir)
    forward = _load_forward(calibration_dir, horizons)
    rows = _join_diagnostic_rows(
        panel,
        forward,
        market_sqlite=market_sqlite,
        zip_dir=edinet_zip_dir,
    )
    complete = {horizon: _complete_cohort_count(rows, horizon) for horizon in horizons}
    horizon_results: dict[str, object] = {}
    for horizon in horizons:
        population_medians = _population_medians(rows, horizon)
        state_groups = {
            state: _group_summary(
                [row for row in rows if row.authorization_state == state],
                horizon,
                label=state,
                population_medians=population_medians,
            )
            for state in ("ended", "active", "no_filing", "filing_state_unresolved", "unknown")
        }
        purpose_groups = {
            purpose: _group_summary(
                [row for row in rows if purpose in row.purposes],
                horizon,
                label=purpose,
                population_medians=population_medians,
            )
            for purpose in (
                "employee_compensation_esop",
                "cancellation",
                "other_rerelease",
            )
        }
        horizon_results[horizon] = {
            "authority": HORIZON_AUTHORITY[horizon],
            "resolved_population_rows": sum(horizon in row.forward for row in rows),
            "complete_point_in_time_cohorts": complete[horizon],
            "same_cohort_component_comparison": [
                _variant_comparison(rows, horizon, field="share_change_signal"),
                _variant_comparison(rows, horizon, field="authorization_signal"),
                _variant_comparison(rows, horizon, field="composition_signal"),
            ],
            "authorization_groups": state_groups,
            "treasury_disposition_groups": purpose_groups,
            "purpose_coverage": {
                coverage: sum(row.purpose_coverage == coverage for row in rows)
                for coverage in ("complete", "partial", "unavailable", "not_applicable")
            },
        }

    long_complete = complete.get("3y", 0) > 0 and complete.get("5y", 0) > 0
    blocking_reasons: list[str] = []
    if complete.get("3y", 0) == 0:
        blocking_reasons.append("no_complete_3y_authorization_cohort")
    if complete.get("5y", 0) == 0:
        blocking_reasons.append("no_complete_5y_authorization_cohort")
    if long_complete:
        blocking_reasons.append("generic_estimate_calibration_decision_required")
    return {
        "kind": "buyback-authorization-calibration",
        "metric_basis": "price_return_only",
        "population": "liquidity_passing_panel_rows",
        "rules_hash": rules_hash,
        "source_coverage": _source_coverage(market_sqlite),
        "panel": {
            "asof_from": min(panel),
            "asof_to": max(panel),
            "cohorts": len(panel),
            "rows": len(rows),
        },
        "variant_contract": {
            "share_change_signal": "clip(-net_share_change_yoy, -0.05, 0.05)",
            "authorization_signal": (
                "active: clip(4 * trailing_3m_acquired_ratio, 0, 0.05); "
                "ended/no_filing: 0; unresolved: null"
            ),
            "composition_signal": (
                "active: max(share_change_signal, authorization_signal); "
                "ended/no_filing: 0; unresolved: null"
            ),
            "point_in_time": "filing date and report month must both be <= cohort as-of",
            "purpose_meaning": (
                "actual monthly treasury-share disposition, not future authorization intent"
            ),
        },
        "horizons": horizon_results,
        "production_decision": {
            # This experiment measures one proposed variant.  It cannot grant the
            # generic calibration authority that governs a production rule change.
            "adoption_allowed": False,
            "long_horizon_source_prerequisites_met": long_complete,
            "blocking_reasons": blocking_reasons,
            "authority": "generic_estimate_calibration_decision_required",
            "short_horizons": "3m/6m are regression alerts; 1y is leading evidence",
            "non_adoption_action": (
                "keep production E[r] unchanged and strip ended trailing carry "
                "in shortlist narrative"
            ),
        },
        "integrity": [
            "The three signal variants use the same resolved cohort rows.",
            "Missing filing, report-state, or disposition evidence remains unknown, never zero.",
            (
                "Form-220 corrections cannot reconstruct a superseded original at an "
                "earlier as-of; such rows are absent rather than backfilled from the future."
            ),
            "Monthly cohort forward windows overlap; cohort counts are not independent samples.",
            "Price return excludes cash dividends and does not directly observe buyback cash flow.",
        ],
    }


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Join point-in-time buyback authorization state to calibration cohorts."
    )
    parser.add_argument("--calibration-dir", type=Path, default=DEFAULT_CALIBRATION_DIR)
    parser.add_argument("--market-sqlite", type=Path, default=DEFAULT_MARKET_SQLITE)
    parser.add_argument("--edinet-zip-dir", type=Path, default=DEFAULT_EDINET_ZIP_DIR)
    parser.add_argument("--horizon", action="append", dest="horizons", choices=DEFAULT_HORIZONS)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    horizons = tuple(args.horizons) if args.horizons else DEFAULT_HORIZONS
    try:
        payload = build_measurement(
            calibration_dir=args.calibration_dir,
            market_sqlite=args.market_sqlite,
            edinet_zip_dir=args.edinet_zip_dir,
            horizons=horizons,
        )
    except (BuybackAuthorizationMeasurementError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    if args.out is None:
        print(text, file=stdout or sys.stdout, end="")
    else:
        args.out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
