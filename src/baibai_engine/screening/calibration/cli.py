"""Build and evaluate the versioned long-horizon calibration cache."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from shutil import rmtree
from typing import TextIO, cast

import yaml

from ..rule_config import ScreeningRules
from .authority import (
    KNOWN_METRICS,
    PRODUCTION_REQUIRED_METRICS,
    CohortIntegrity,
    EvaluationScope,
    decide_authority,
)
from .evaluation import evaluate_cohorts
from .forward import HORIZONS, ForwardReturnRow, compute_forward_returns
from .grid import month_end_asof_grid
from .panel import CalibrationError, build_panel
from .store import (
    CACHE_SCHEMA_VERSION,
    CalibrationCacheError,
    panel_path,
    read_forward,
    read_panel,
    read_panel_meta,
    write_forward,
    write_panel,
)


def calibration_build_command(
    *,
    sqlite_path: Path,
    calibration_dir: Path,
    rules: ScreeningRules,
    start: date,
    end: date,
    force: bool = False,
    stdout: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    asofs = month_end_asof_grid(sqlite_path, start=start, end=end)
    if not asofs:
        print("no month-end trading days found in the requested window", file=sys.stderr)
        return 1
    work_dir = calibration_dir
    if force:
        work_dir = calibration_dir.with_name(f".{calibration_dir.name}.rebuild")
        if work_dir.exists():
            rmtree(work_dir)
    tickers_by_asof: dict[date, set[str]] = {}
    built = 0
    for asof in asofs:
        path = panel_path(work_dir, asof)
        try:
            if path.exists() and not force:
                tickers_by_asof[asof] = {row.ticker for row in read_panel(work_dir, asof)}
                continue
            result = build_panel(asof, sqlite_path=sqlite_path, rules=rules)
        except (CalibrationError, CalibrationCacheError) as exc:
            print(f"calibration build: {asof.isoformat()} failed: {exc}", file=sys.stderr)
            return 1
        write_panel(work_dir, asof, result.rows, result.diagnostics)
        tickers_by_asof[asof] = {row.ticker for row in result.rows}
        built += 1
    by_asof: dict[str, list[ForwardReturnRow]] = {}
    for asof in asofs:
        for row in compute_forward_returns(
            sqlite_path, asofs=(asof,), tickers=tickers_by_asof[asof]
        ):
            by_asof.setdefault(row.asof, []).append(row)
    for asof in asofs:
        write_forward(work_dir, asof, by_asof.get(asof.isoformat(), []))
    rows = [row for cohort_rows in by_asof.values() for row in cohort_rows]
    resolved = sum(row.status == "resolved" for row in rows)
    if force:
        backup_dir = calibration_dir.with_name(f".{calibration_dir.name}.backup")
        if backup_dir.exists():
            rmtree(backup_dir)
        if calibration_dir.exists():
            calibration_dir.replace(backup_dir)
        try:
            work_dir.replace(calibration_dir)
        except OSError:
            if backup_dir.exists():
                backup_dir.replace(calibration_dir)
            raise
        if backup_dir.exists():
            rmtree(backup_dir)
    print(
        f"calibration build: done (panels built={built}, forward rows={len(rows)}, "
        f"resolved={resolved})",
        file=out,
    )
    return 0


def calibration_evaluate_command(
    *,
    calibration_dir: Path,
    horizons: list[str] | None = None,
    run_purpose: str = "diagnostic",
    required_asofs: list[str] | None = None,
    required_metrics: list[str] | None = None,
    output_path: Path | None = None,
    start: date | None = None,
    end: date | None = None,
    stdout: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    horizons = horizons or list(HORIZONS)
    unknown = [horizon for horizon in horizons if horizon not in HORIZONS]
    if unknown:
        print(f"unknown horizon(s): {', '.join(unknown)}", file=sys.stderr)
        return 1
    if run_purpose not in {"diagnostic", "production_decision"}:
        print("--run-purpose must be diagnostic or production_decision", file=sys.stderr)
        return 1
    if run_purpose == "production_decision":
        if not required_asofs or not required_metrics:
            print(
                "production_decision requires explicit --required-asof and --required-metric",
                file=sys.stderr,
            )
            return 1
        unknown_metrics = set(required_metrics) - KNOWN_METRICS
        missing_core_metrics = set(PRODUCTION_REQUIRED_METRICS) - set(required_metrics)
        if unknown_metrics or missing_core_metrics:
            print(
                "production_decision required metrics must include "
                f"{', '.join(PRODUCTION_REQUIRED_METRICS)}",
                file=sys.stderr,
            )
            return 1
    all_asofs = [
        date.fromisoformat(path.stem.removeprefix("panel-"))
        for path in calibration_dir.glob("panel-*.csv")
    ]
    asofs = sorted(
        asof
        for asof in all_asofs
        if (start is None or asof >= start) and (end is None or asof <= end)
    )
    if not asofs:
        print(f"no panels found under {calibration_dir}", file=sys.stderr)
        return 1
    try:
        metas = [read_panel_meta(calibration_dir, asof) for asof in asofs]
        if len({meta.get("rules_hash") for meta in metas}) != 1:
            raise CalibrationCacheError(
                "panel store mixes rules provenance; run calibration-build --force"
            )
        panels = {asof.isoformat(): read_panel(calibration_dir, asof) for asof in asofs}
        forwards = {asof.isoformat(): read_forward(calibration_dir, asof) for asof in asofs}
    except CalibrationCacheError as exc:
        print(f"calibration evaluate: {exc}", file=sys.stderr)
        return 1
    results = evaluate_cohorts(panels, forwards, horizons=horizons)
    scope = EvaluationScope(
        run_purpose=run_purpose,
        requested_horizons=tuple(horizons),
        cohort_window={
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
        },
        required_asofs=tuple(required_asofs or [asof.isoformat() for asof in asofs]),
        required_metrics=tuple(required_metrics or PRODUCTION_REQUIRED_METRICS),
    )
    integrity: list[CohortIntegrity] = []
    integrity_reason_counts: dict[str, int] = {}
    for horizon, result in results.items():
        assert isinstance(result, dict)
        for cohort in result["cohorts"]:
            assert isinstance(cohort, dict)
            coverage = cast(dict[str, object], cohort["coverage"])
            blockers: list[str] = []
            meta = metas[[asof.isoformat() for asof in asofs].index(str(cohort["asof"]))]
            policy_reason_counts = meta.get("policy_exclusion_reason_counts")
            if not isinstance(policy_reason_counts, dict):
                policy_reason_counts = {}
            coverage.update(
                {
                    "master_snapshot_date": meta.get("master_snapshot_date"),
                    "master_snapshot_status": meta.get("master_snapshot_status", "unavailable"),
                    "master_population_count": meta.get("master_population_count", 0),
                    "candidate_population_count": meta.get("candidate_population_count", 0),
                    "policy_exclusion_reason_counts": policy_reason_counts,
                    "policy_excluded_count": sum(
                        int(value)
                        for value in policy_reason_counts.values()
                        if isinstance(value, int)
                    ),
                    "input_range_clamped": bool(
                        meta.get("bars_window_clamped") or meta.get("fin_window_clamped")
                    ),
                }
            )
            if horizon in {"3y", "5y"}:
                if meta.get("master_snapshot_status") != "exact_date":
                    blockers.append(
                        f"master_snapshot:{meta.get('master_snapshot_status', 'unavailable')}"
                    )
                for field in (
                    "survivorship_coverage_status",
                    "delisting_coverage_status",
                    "corporate_action_event_coverage_status",
                ):
                    if coverage.get(field) != "complete":
                        blockers.append(f"{field}:{coverage.get(field, 'unknown')}")
                if coverage["input_range_clamped"]:
                    blockers.append("input_range_clamped")
                if not coverage.get("candidate_partition_complete"):
                    blockers.append("candidate_partition_incomplete")
                if coverage.get("data_unresolved_count"):
                    blockers.append("unresolved_forward_rows")
            metric_status = (
                "eligible" if cohort["metric_calculation_status"] == "resolved" else "unresolved"
            )
            reported_metric_statuses = cohort.get("metric_statuses")
            metric_statuses = (
                cast(dict[str, str], reported_metric_statuses)
                if isinstance(reported_metric_statuses, dict)
                else {}
            )
            integrity.append(
                CohortIntegrity(
                    asof=str(cohort["asof"]),
                    horizon=horizon,
                    integrity_status=("blocked" if blockers else metric_status),
                    metric_statuses={
                        metric: metric_statuses.get(metric, metric_status)
                        for metric in scope.required_metrics
                    },
                    blocking_reasons=tuple(blockers),
                )
            )
            for reason in blockers:
                integrity_reason_counts[reason] = integrity_reason_counts.get(reason, 0) + 1
    decision = decide_authority(scope, tuple(integrity))
    required_pairs = {(asof, horizon) for asof in scope.required_asofs for horizon in ("3y", "5y")}
    required_integrity = [item for item in integrity if (item.asof, item.horizon) in required_pairs]
    eligible_pairs = {
        (item.asof, item.horizon)
        for item in required_integrity
        if item.integrity_status == "eligible"
        and all(item.metric_statuses.get(metric) == "eligible" for metric in scope.required_metrics)
    }
    for reason in decision.blocking_reasons:
        integrity_reason_counts.setdefault(reason, 1)
    payload = {
        "kind": "estimate-calibration-evaluation",
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "metric_basis": "price_return_only",
        "scope": {
            "run_purpose": scope.run_purpose,
            "requested_horizons": list(scope.requested_horizons),
            "cohort_window": scope.cohort_window,
            "required_asofs": list(scope.required_asofs),
            "required_metrics": list(scope.required_metrics),
        },
        "production_decision": decision.payload(),
        "authority_coverage": {
            "required_cohort_count": len(required_pairs),
            "eligible_metric_cohort_count": len(eligible_pairs),
            "blocked_or_unresolved_count": len(required_pairs - eligible_pairs),
            "reason_counts": integrity_reason_counts,
        },
        "results": results,
    }
    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
        print(f"calibration evaluate: wrote {output_path}", file=out)
    else:
        print(text, file=out)
    return 0
