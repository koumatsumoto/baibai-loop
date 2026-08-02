"""Build and evaluate the versioned long-horizon calibration cache."""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from shutil import rmtree
from statistics import median
from typing import TextIO, cast
from zoneinfo import ZoneInfo

import yaml

from ..estimates import EXPECTED_RETURN_MODEL_VERSION
from ..rule_config import ScreeningRules
from ..store_readiness import unreadable_store_reason
from .authority import (
    KNOWN_METRICS,
    PRODUCTION_REQUIRED_METRICS,
    CohortIntegrity,
    EvaluationScope,
    decide_authority,
)
from .evaluation import OPTIONAL_SENSITIVITY_METRICS, evaluate_cohorts
from .forward import HORIZONS, ForwardReturnRow, compute_forward_returns
from .grid import month_end_asof_grid
from .panel import (
    PANEL_BUILD_POLICIES,
    PRODUCTION_PANEL_POLICY,
    CalibrationError,
    PanelRow,
    PanelVariant,
    build_panel,
    rules_content_hash,
)
from .store import (
    CACHE_SCHEMA_VERSION,
    DEFAULT_CALIBRATION_DIR,
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
    panel_variant: PanelVariant = "production",
    stdout: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    unreadable = unreadable_store_reason(sqlite_path)
    if unreadable is not None:
        print(f"calibration build: {unreadable}", file=sys.stderr)
        return 1
    policy = PANEL_BUILD_POLICIES[panel_variant]
    if (
        not policy.production_authority
        and calibration_dir.resolve() == DEFAULT_CALIBRATION_DIR.resolve()
    ):
        print(
            "calibration build: diagnostic panel variant requires a separate --calibration-dir",
            file=sys.stderr,
        )
        return 1
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
    expected_rules_hash = rules_content_hash(rules, policy)
    for asof in asofs:
        path = panel_path(work_dir, asof)
        try:
            if path.exists() and not force:
                meta = read_panel_meta(work_dir, asof)
                if (
                    meta.get("rules_hash") != expected_rules_hash
                    or meta.get("panel_variant") != policy.variant
                    or meta.get("production_authority") is not policy.production_authority
                    or meta.get("self_range_history_sessions") != policy.valuation_history_sessions
                    or meta.get("bars_input_window_days") != policy.bars_input_window_days
                ):
                    raise CalibrationCacheError(
                        "calibration panel contract differs from the requested build; "
                        "run calibration-build --force"
                    )
                tickers_by_asof[asof] = {row.ticker for row in read_panel(work_dir, asof)}
                continue
            result = build_panel(asof, sqlite_path=sqlite_path, rules=rules, policy=policy)
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


def _optional_count(value: object) -> int | None:
    """Read a panel count, keeping "not measured" distinct from zero."""
    return value if type(value) is int else None


def _survivorship_status(mismatch: int | None) -> str:
    """Turn the population mismatch count into the cohort verdict.

    Deriving the verdict here rather than freezing it into the panel keeps a
    change in what counts as complete applicable to cohorts already on disk.
    """
    if mismatch is None:
        return "unavailable"
    return "complete" if mismatch == 0 else "incomplete"


def _required_metric_statuses(
    reported: object, required_metrics: tuple[str, ...]
) -> dict[str, str]:
    """Missing or unknown per-metric evidence cannot inherit cohort eligibility."""
    statuses = reported if isinstance(reported, dict) else {}
    return {
        metric: (
            str(statuses[metric])
            if statuses.get(metric) in {"eligible", "unresolved"}
            else "unresolved"
        )
        for metric in required_metrics
    }


def calibration_evaluate_command(
    *,
    calibration_dir: Path,
    horizons: list[str] | None = None,
    run_purpose: str = "diagnostic",
    required_asofs: list[str] | None = None,
    required_metrics: list[str] | None = None,
    output_path: Path | None = None,
    context_output_path: Path | None = None,
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
        if run_purpose == "production_decision" and not _is_production_panel_contract(
            metas, panels
        ):
            print(
                "calibration evaluate: diagnostic panel variant has no production authority",
                file=sys.stderr,
            )
            return 1
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
            mismatch = _optional_count(meta.get("asof_population_mismatch_count"))
            unevaluated = _optional_count(meta.get("priced_master_without_universe_count"))
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
                    # Survivorship belongs to the population the panel drew, so the
                    # panel measures it and the reader turns the counts into the
                    # verdict. A panel written before the measurement existed
                    # reports null rather than zero, so "not measured" cannot be
                    # read as "nothing missing".
                    "survivorship_coverage_status": _survivorship_status(mismatch),
                    "asof_priced_count": meta.get("asof_priced_count"),
                    "asof_population_mismatch_count": mismatch,
                    "policy_excluded_priced_count": meta.get("policy_excluded_priced_count"),
                    "priced_master_without_universe_count": unevaluated,
                    "entry_resolution_lag_days": meta.get("entry_resolution_lag_days"),
                    "panel_variant": meta.get("panel_variant", "unknown"),
                    "production_authority": meta.get("production_authority"),
                    "self_range_history_sessions": meta.get("self_range_history_sessions"),
                    "self_range_degraded_count": sum(
                        1 for row in panels[str(cohort["asof"])] if row.self_range_degraded
                    ),
                }
            )
            if horizon in {"3y", "5y"}:
                # One blocker per independent observation. A verdict derived from
                # another observation would count the same gap twice and make the
                # reason histogram unreadable.
                if meta.get("master_snapshot_status") != "exact_date":
                    blockers.append("master_snapshot")
                if coverage["survivorship_coverage_status"] != "complete":
                    blockers.append("survivorship")
                if coverage.get("adjustment_factor_coverage") != "complete":
                    blockers.append("adjustment_factor")
                if coverage["input_range_clamped"]:
                    blockers.append("input_range_clamped")
                if not coverage.get("candidate_partition_complete"):
                    blockers.append("candidate_partition_incomplete")
                unevaluated_sensitivity = coverage.get("priced_master_without_universe")
                if (
                    not isinstance(unevaluated, int)
                    or not isinstance(unevaluated_sensitivity, dict)
                    or unevaluated_sensitivity.get("excluded_count") != unevaluated
                ):
                    blockers.append("priced_master_without_universe_unmeasured")
                elif not unevaluated_sensitivity.get("direction_stable"):
                    blockers.append("priced_master_without_universe_flips_direction")
                # Each unresolved class blocks for its own reason, and a name the
                # panel could not price at asof blocks for none of them. The
                # residual keeps an unenumerated status from passing silently.
                for field, label in (
                    ("entry_price_gap_count", "entry_price_gap"),
                    ("future_horizon_count", "horizon_not_matured"),
                    ("unclassified_unresolved_count", "unclassified_unresolved"),
                ):
                    count = coverage.get(field)
                    if isinstance(count, int) and count:
                        blockers.append(label)
                # A name that left the market carries no exit value, and delistings
                # happen in every cohort, so blocking on their presence blocks
                # forever. What matters is whether they could have produced the
                # conclusions: the cohort blocks when the sign of a conclusion moves
                # between giving those names a total loss and giving them what the
                # rest of the cohort returned. Pre-registered in
                # reports/2026-07-31-delisting-exclusion-preregistration.md.
                sensitivity = coverage.get("delisting_exclusion")
                if isinstance(sensitivity, dict) and not sensitivity.get("direction_stable"):
                    blockers.append("unpriced_exit_flips_direction")
                optional_required = set(scope.required_metrics).intersection(
                    OPTIONAL_SENSITIVITY_METRICS
                )
                for coverage_key, reason_prefix in (
                    ("delisting_exclusion", "unpriced_exit_flips"),
                    ("priced_master_without_universe", "priced_master_without_universe_flips"),
                ):
                    sensitivity = coverage.get(coverage_key)
                    metric_stability = (
                        sensitivity.get("metric_direction_stable")
                        if isinstance(sensitivity, dict)
                        else None
                    )
                    for metric in sorted(optional_required):
                        if (
                            not isinstance(metric_stability, dict)
                            or metric_stability.get(metric) is not True
                        ):
                            blockers.append(f"{reason_prefix}:{metric}")
            metric_status = (
                "eligible" if cohort["metric_calculation_status"] == "resolved" else "unresolved"
            )
            integrity.append(
                CohortIntegrity(
                    asof=str(cohort["asof"]),
                    horizon=horizon,
                    integrity_status=("blocked" if blockers else metric_status),
                    metric_statuses=_required_metric_statuses(
                        cohort.get("metric_statuses"), scope.required_metrics
                    ),
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
    # The histogram answers "how many required cohorts show this", so the per-cohort
    # verdicts are counted here rather than taken from the decision's deduped classes.
    for item in required_integrity:
        if item.integrity_status != "eligible":
            key = f"integrity_{item.integrity_status}"
            integrity_reason_counts[key] = integrity_reason_counts.get(key, 0) + 1
        for metric in scope.required_metrics:
            if item.metric_statuses.get(metric) != "eligible":
                key = f"metric_unresolved:{metric}"
                integrity_reason_counts[key] = integrity_reason_counts.get(key, 0) + 1
    # Scope-level reasons hold for the run as a whole, so they count once.
    for reason in decision.blocking_reasons:
        integrity_reason_counts.setdefault(reason, 1)
    payload = {
        "kind": "estimate-calibration-evaluation",
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "screening_rules_hash": metas[0].get("rules_hash"),
        "er_model_version": EXPECTED_RETURN_MODEL_VERSION,
        "metric_basis": "price_return_only",
        "metric_bases": ["price_return_only", "fy_actual_dividend_total_return"],
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
    context_payload: dict[str, object] | None = None
    if context_output_path is not None:
        try:
            context_payload = _er_level_context_payload(payload)
        except ValueError as exc:
            print(f"calibration evaluate: context not written: {exc}", file=sys.stderr)
            return 1
    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
        print(f"calibration evaluate: wrote {output_path}", file=out)
    else:
        print(text, file=out)
    if context_output_path is not None and context_payload is not None:
        context_output_path.parent.mkdir(parents=True, exist_ok=True)
        context_output_path.write_text(
            yaml.safe_dump(context_payload, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        print(f"calibration evaluate: wrote {context_output_path}", file=out)
    return 0


def _er_level_context_payload(
    evaluation: dict[str, object], *, generated_at: datetime | None = None
) -> dict[str, object]:
    """Materialize the small, expiring E[r] context consumed by the review UI."""

    decision = evaluation.get("production_decision")
    scope = evaluation.get("scope")
    results = evaluation.get("results")
    rules_hash = evaluation.get("screening_rules_hash")
    er_model_version = evaluation.get("er_model_version")
    if (
        not isinstance(decision, dict)
        or decision.get("evidence_status") != "eligible"
        or decision.get("production_change_allowed") is not True
    ):
        raise ValueError("production authority is not eligible")
    if not isinstance(scope, dict) or scope.get("run_purpose") != "production_decision":
        raise ValueError("context requires a production_decision evaluation")
    required_metrics = scope.get("required_metrics")
    if not isinstance(required_metrics, list) or "er_level_calibration" not in required_metrics:
        raise ValueError("er_level_calibration is not a required metric")
    required_asofs = scope.get("required_asofs")
    if not isinstance(required_asofs, list) or not required_asofs:
        raise ValueError("required as-of scope is empty")
    required = {str(value) for value in required_asofs}
    if not isinstance(results, dict):
        raise ValueError("evaluation results are missing")
    if not isinstance(rules_hash, str) or not rules_hash:
        raise ValueError("screening rules provenance is missing")
    if not isinstance(er_model_version, str) or not er_model_version:
        raise ValueError("E[r] model provenance is missing")

    horizons: list[dict[str, object]] = []
    for horizon in ("3y", "5y"):
        result = results.get(horizon)
        cohorts = result.get("cohorts") if isinstance(result, dict) else None
        if not isinstance(cohorts, list):
            raise ValueError(f"{horizon} cohorts are missing")
        selected = [
            cohort
            for cohort in cohorts
            if isinstance(cohort, dict) and str(cohort.get("asof")) in required
        ]
        if len(selected) != len(required):
            raise ValueError(f"{horizon} does not cover every required as-of")
        quintile_rows: list[list[dict[str, object]]] = [[] for _ in range(5)]
        for cohort in selected:
            statuses = cohort.get("metric_statuses")
            calibration = cohort.get("er_level_calibration")
            cells = calibration.get("er_quintiles") if isinstance(calibration, dict) else None
            if (
                not isinstance(statuses, dict)
                or statuses.get("er_level_calibration") != "eligible"
                or not isinstance(cells, list)
                or len(cells) != 5
            ):
                raise ValueError(f"{horizon} contains an ineligible E[r] level cohort")
            for index, cell in enumerate(cells):
                if not isinstance(cell, dict):
                    raise ValueError(f"{horizon} quintile payload is invalid")
                quintile_rows[index].append(cell)

        quintiles: list[dict[str, object]] = []
        for index, cells in enumerate(quintile_rows):
            predicted = _context_float_values(cells, "median_predicted_er_annual", horizon)
            realized = _context_float_values(cells, "median_realized_total_return_annual", horizon)
            ns = _context_int_values(cells, "n", horizon)
            upper = _context_float_values(cells, "max_predicted_er_annual", horizon)
            quintiles.append(
                {
                    "quintile": index + 1,
                    "upper_er_annual": None if index == 4 else round(median(upper), 6),
                    "median_predicted_er_annual": round(median(predicted), 6),
                    "median_realized_total_return_annual": round(median(realized), 6),
                    "median_n": int(median(ns)),
                }
            )
        cutoff_values = [item.get("upper_er_annual") for item in quintiles[:-1]]
        if not all(isinstance(value, int | float) for value in cutoff_values):
            raise ValueError(f"{horizon} E[r] band edges are incomplete")
        cutoffs = [float(value) for value in cutoff_values if isinstance(value, int | float)]
        if any(index > 0 and value <= cutoffs[index - 1] for index, value in enumerate(cutoffs)):
            raise ValueError(f"{horizon} E[r] band edges are not strictly increasing")
        ordered_asofs = sorted(required)
        horizons.append(
            {
                "horizon": horizon,
                "asof_start": ordered_asofs[0],
                "asof_end": ordered_asofs[-1],
                "cohort_count": len(selected),
                "quintiles": quintiles,
            }
        )

    now = generated_at or datetime.now(ZoneInfo("Asia/Tokyo"))
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("generated_at must include a timezone")
    return {
        "kind": "er-level-calibration-context",
        "schema_version": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "valid_through": (now.date() + timedelta(days=45)).isoformat(),
        "reference_horizon": "3y",
        "screening_rules_hash": rules_hash,
        "er_model_version": er_model_version,
        "realized_basis": "fy_actual_dividend_total_return_annualized_absolute",
        "horizons": horizons,
    }


def _context_float_values(cells: list[dict[str, object]], field: str, horizon: str) -> list[float]:
    values = [cell.get(field) for cell in cells]
    if not all(isinstance(value, int | float) for value in values):
        raise ValueError(f"{horizon} quintile values are incomplete")
    return [float(value) for value in values if isinstance(value, int | float)]


def _context_int_values(cells: list[dict[str, object]], field: str, horizon: str) -> list[int]:
    values = [cell.get(field) for cell in cells]
    if not all(isinstance(value, int) for value in values):
        raise ValueError(f"{horizon} quintile values are incomplete")
    return [value for value in values if isinstance(value, int)]


def _is_production_panel_contract(
    metas: list[dict[str, object]], panels: dict[str, list[PanelRow]]
) -> bool:
    policy = PRODUCTION_PANEL_POLICY
    return all(
        meta.get("panel_variant") == policy.variant
        and meta.get("production_authority") is policy.production_authority
        and meta.get("self_range_history_sessions") == policy.valuation_history_sessions
        and meta.get("bars_input_window_days") == policy.bars_input_window_days
        for meta in metas
    ) and all(not row.self_range_degraded for rows in panels.values() for row in rows)
