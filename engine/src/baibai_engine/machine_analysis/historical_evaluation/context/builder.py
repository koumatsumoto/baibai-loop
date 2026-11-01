"""Materialize the expiring E[r] realized-distribution context."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import datetime, timedelta
from itertools import pairwise
from math import ceil, isfinite
from statistics import median
from typing import cast
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from baibai_engine.machine_analysis.historical_evaluation.context.contracts import (
    ER_CALIBRATION_CONTEXT_KIND,
    ER_CALIBRATION_CONTEXT_SCHEMA_VERSION,
    ER_CALIBRATION_CONTEXT_VALID_DAYS,
    ER_CALIBRATION_PRIMARY_REALIZED_BASIS,
    ER_CALIBRATION_PRIMARY_WEIGHTING,
    ER_CALIBRATION_REFERENCE_HORIZON,
    ER_CALIBRATION_SECONDARY_REALIZED_BASIS,
    ER_CALIBRATION_SECONDARY_WEIGHTING,
    ER_CALIBRATION_TRAP_BASIS,
    HURDLE_ER_ANNUAL,
    ErCalibrationContextArtifact,
)
from baibai_engine.machine_analysis.historical_evaluation.context.query import (
    er_quintile_for_bounds,
)
from baibai_engine.machine_analysis.historical_evaluation.estimates import (
    EXPECTED_RETURN_MODEL_VERSION,
)
from baibai_engine.machine_analysis.historical_evaluation.evaluation.policy import (
    TRAP_EXCESS_THRESHOLD,
)
from baibai_engine.machine_analysis.historical_evaluation.evaluation.statistics import (
    annualize_return,
)
from baibai_engine.machine_analysis.historical_evaluation.evidence import (
    ESTIMATOR_POLICY_MANDATORY_METRICS,
    ESTIMATOR_POLICY_SUBJECT,
)
from baibai_engine.machine_analysis.historical_evaluation.forward import (
    RESOLVED_STATUSES,
    TOTAL_RETURN_BASIS,
    TOTAL_RETURN_STATUSES,
    ForwardReturnRow,
)
from baibai_engine.machine_analysis.historical_evaluation.horizons import require_horizon
from baibai_engine.machine_analysis.historical_evaluation.panel import PanelRow
from baibai_engine.machine_analysis.historical_evaluation.store import forward_row_from_mapping


class CalibrationContextError(ValueError):
    """Raised when declared inputs cannot support a descriptive context artifact."""


def build_er_distribution_context(
    evaluation: Mapping[str, object],
    panels: Mapping[str, Sequence[PanelRow]],
    forwards: Mapping[str, Sequence[ForwardReturnRow]],
    *,
    generated_at: datetime | None = None,
) -> dict[str, object]:
    """Keep the declared prediction bands while reporting observed outcomes.

    機械priorの誤差を示す分布で、L3の要求率決定や4 Approachの優越性を代行しない。
    docs/reference/estimate-calibration.md
    """

    readiness = evaluation.get("evidence_readiness")
    scope = evaluation.get("scope")
    if not isinstance(readiness, Mapping) or not isinstance(
        readiness.get("evidence_complete"), bool
    ):
        raise CalibrationContextError("evidence readiness is missing")
    if not isinstance(scope, Mapping) or scope.get("run_purpose") != "empirical_change_evidence":
        raise CalibrationContextError("context requires an empirical_change_evidence evaluation")
    if scope.get("decision_subject") != ESTIMATOR_POLICY_SUBJECT:
        raise CalibrationContextError("E[r] context requires estimator_policy evidence")
    required_metrics = scope.get("required_metrics")
    mandatory_metrics = scope.get("mandatory_metrics")
    required = {*ESTIMATOR_POLICY_MANDATORY_METRICS, "er_level_calibration"}
    declared = set(required_metrics) if isinstance(required_metrics, list) else set()
    if isinstance(mandatory_metrics, list):
        declared.update(mandatory_metrics)
    if not required.issubset(declared):
        raise CalibrationContextError("context required metrics are incomplete")
    rules_hash = evaluation.get("screening_rules_hash")
    er_model_version = evaluation.get("er_model_version")
    if not isinstance(rules_hash, str) or not rules_hash:
        raise CalibrationContextError("screening rules provenance is missing")
    if not isinstance(er_model_version, str) or not er_model_version:
        raise CalibrationContextError("E[r] model provenance is missing")
    if er_model_version != EXPECTED_RETURN_MODEL_VERSION:
        raise CalibrationContextError("E[r] model identity does not match the producer")
    asofs = _declared_asofs(evaluation, panels, forwards)
    fixed_boundaries = _prediction_boundaries(panels, asofs)

    horizons: list[dict[str, object]] = []
    for horizon in ("3y", "5y"):
        horizons.append(
            _horizon_context(
                horizon,
                asofs,
                panels=panels,
                forwards=forwards,
                fixed_boundaries=fixed_boundaries,
            )
        )

    now = generated_at or datetime.now(ZoneInfo("Asia/Tokyo"))
    if now.tzinfo is None or now.utcoffset() is None:
        raise CalibrationContextError("generated_at must include a timezone")
    payload: dict[str, object] = {
        "kind": ER_CALIBRATION_CONTEXT_KIND,
        "schema_version": ER_CALIBRATION_CONTEXT_SCHEMA_VERSION,
        "interpretation": "observed_distribution_only",
        "required_asofs": asofs,
        "evidence_complete": readiness["evidence_complete"],
        "evidence_blocking_reasons": list(readiness.get("blocking_reasons", [])),
        "generated_at": now.isoformat(timespec="seconds"),
        "valid_through": (
            now.astimezone(ZoneInfo("Asia/Tokyo")).date()
            + timedelta(days=ER_CALIBRATION_CONTEXT_VALID_DAYS)
        ).isoformat(),
        "reference_horizon": ER_CALIBRATION_REFERENCE_HORIZON,
        "screening_rules_hash": rules_hash,
        "er_model_version": er_model_version,
        "primary_realized_basis": ER_CALIBRATION_PRIMARY_REALIZED_BASIS,
        "secondary_realized_basis": ER_CALIBRATION_SECONDARY_REALIZED_BASIS,
        "trap_basis": ER_CALIBRATION_TRAP_BASIS,
        "weighting": {
            "primary": ER_CALIBRATION_PRIMARY_WEIGHTING,
            "secondary": ER_CALIBRATION_SECONDARY_WEIGHTING,
        },
        "common_window": {
            "asof_start": asofs[0],
            "asof_end": asofs[-1],
            "cohort_count": len(asofs),
            "horizons": horizons,
        },
        "horizons": horizons,
    }
    try:
        ErCalibrationContextArtifact.model_validate(payload)
    except ValidationError as exc:
        raise CalibrationContextError(
            "generated E[r] context violates the shared contract"
        ) from exc
    return payload


def _declared_asofs(
    evaluation: Mapping[str, object],
    panels: Mapping[str, Sequence[PanelRow]],
    forwards: Mapping[str, Sequence[ForwardReturnRow]],
) -> list[str]:
    scope = cast(Mapping[str, object], evaluation["scope"])
    raw = scope.get("required_asofs")
    if not isinstance(raw, list) or not raw or any(not isinstance(value, str) for value in raw):
        raise CalibrationContextError("context requires explicit declared dates")
    asofs = sorted(set(cast(list[str], raw)))
    integrity = evaluation.get("cohort_integrity")
    results = evaluation.get("results")
    if not isinstance(integrity, list) or not isinstance(results, Mapping):
        raise CalibrationContextError("cohort evidence is missing")
    complete = cast(Mapping[str, object], evaluation["evidence_readiness"])["evidence_complete"]
    for horizon in ("3y", "5y"):
        result = results.get(horizon)
        cohorts = result.get("cohorts") if isinstance(result, Mapping) else None
        if not isinstance(cohorts, list):
            raise CalibrationContextError(f"{horizon} cohort evidence is missing")
        for asof in asofs:
            if asof not in panels or not panels[asof] or asof not in forwards:
                raise CalibrationContextError(f"declared panel or forward rows missing: {asof}")
            items = [
                item
                for item in integrity
                if isinstance(item, Mapping)
                and item.get("asof") == asof
                and item.get("horizon") == horizon
            ]
            matches = [
                item for item in cohorts if isinstance(item, Mapping) and item.get("asof") == asof
            ]
            if len(items) != 1 or len(matches) != 1:
                raise CalibrationContextError(
                    f"declared cohort evidence is missing or duplicated: {asof} {horizon}"
                )
            item = items[0]
            statuses = item.get("metric_statuses")
            if complete and (
                item.get("integrity_status") != "eligible"
                or not isinstance(statuses, Mapping)
                or any(
                    statuses.get(metric) != "eligible"
                    for metric in (*ESTIMATOR_POLICY_MANDATORY_METRICS, "er_level_calibration")
                )
            ):
                raise CalibrationContextError("complete evidence contradicts cohort integrity")
            coverage = matches[0].get("coverage")
            if not isinstance(coverage, Mapping) or any(
                coverage.get(key) != expected
                for key, expected in {
                    "master_snapshot_status": "exact_date",
                    "survivorship_coverage_status": "complete",
                    "adjustment_factor_coverage": "complete",
                    "input_range_clamped": False,
                    "candidate_partition_complete": True,
                }.items()
            ):
                raise CalibrationContextError(f"cohort input is incomplete: {asof} {horizon}")
            if coverage.get("unclassified_unresolved_count", 0):
                raise CalibrationContextError("unknown unresolved outcomes cannot be partial")
            tickers = [row.ticker for row in panels[asof]]
            rows = [row for row in forwards[asof] if row.horizon == horizon]
            observed = [row.ticker for row in rows]
            if (
                len(tickers) != len(set(tickers))
                or len(observed) != len(set(observed))
                or not set(tickers).issubset(observed)
            ):
                raise CalibrationContextError(
                    f"declared forward partition is incomplete: {asof} {horizon}"
                )
            if any(row.asof != asof for row in panels[asof]) or any(
                row.asof != asof for row in rows
            ):
                raise CalibrationContextError("cohort row date contradicts its declared date")
            for row in rows:
                try:
                    forward_row_from_mapping(asdict(row))
                except ValueError as exc:
                    raise CalibrationContextError("forward observation is corrupt") from exc
    return asofs


def _prediction_boundaries(
    panels: Mapping[str, Sequence[PanelRow]], asofs: Sequence[str]
) -> list[float]:
    signals = {
        asof: [
            row
            for row in panels[asof]
            if row.in_population and row.er_annual is not None and isfinite(row.er_annual)
        ]
        for asof in asofs
    }
    if any(len(rows) < 5 for rows in signals.values()):
        raise CalibrationContextError("declared cohort has insufficient finite E[r] predictions")
    boundaries = [
        [
            max(row.er_annual for row in chunk if row.er_annual is not None)
            for chunk in _quintile_chunks(
                sorted(rows, key=lambda row: (float(row.er_annual or 0.0), row.ticker))
            )[:4]
        ]
        for rows in signals.values()
    ]
    fixed_boundaries = [
        round(_nearest_rank([cuts[index] for cuts in boundaries], 0.5), 6) for index in range(4)
    ]
    if any(right <= left for left, right in pairwise(fixed_boundaries)):
        raise CalibrationContextError("fixed prediction boundaries are not increasing")

    return fixed_boundaries


def _horizon_context(
    horizon: str,
    asofs: Sequence[str],
    *,
    panels: Mapping[str, Sequence[PanelRow]],
    forwards: Mapping[str, Sequence[ForwardReturnRow]],
    fixed_boundaries: Sequence[float],
) -> dict[str, object]:
    years = require_horizon(horizon).months / 12
    cohort_bands: dict[str, dict[str, list[_BandObservation]]] = {}
    predicted: dict[str, list[float]] = {f"q{index}": [] for index in range(1, 6)}
    predicted["er_gte_8_5pct"] = []
    for asof in asofs:
        rows: dict[str, list[_BandObservation]] = {key: [] for key in predicted}
        for item in _cohort_entries(panels[asof], forwards[asof], horizon=horizon, years=years):
            ids = [f"q{er_quintile_for_bounds(item.er_annual, fixed_boundaries)}"]
            if item.er_annual >= HURDLE_ER_ANNUAL:
                ids.append("er_gte_8_5pct")
            for band_id in ids:
                rows[band_id].append(item)
                predicted[band_id].append(item.er_annual)
        cohort_bands[asof] = rows
    bands = [
        _band_context(
            f"q{index}",
            cohort_bands,
            predicted=predicted[f"q{index}"],
            quintile=index,
            lower_er_annual=None if index == 1 else fixed_boundaries[index - 2],
            upper_er_annual=None if index == 5 else fixed_boundaries[index - 1],
        )
        for index in range(1, 6)
    ]
    bands.append(
        _band_context(
            "er_gte_8_5pct",
            cohort_bands,
            predicted=predicted["er_gte_8_5pct"],
            quintile=None,
            lower_er_annual=HURDLE_ER_ANNUAL,
            upper_er_annual=None,
        )
    )
    return {
        "horizon": horizon,
        "asof_start": asofs[0],
        "asof_end": asofs[-1],
        "cohort_count": len(asofs),
        "bands": bands,
    }


class _BandObservation:
    def __init__(
        self,
        *,
        ticker: str,
        er_annual: float,
        total_annual: float | None,
        price_annual: float | None,
        total_reason: str | None,
        price_reason: str | None,
    ) -> None:
        self.ticker = ticker
        self.er_annual = er_annual
        self.total_annual = total_annual
        self.price_annual = price_annual
        self.total_reason = total_reason
        self.price_reason = price_reason
        self.total_trap = False
        self.price_trap = False


def _cohort_entries(
    panel: Sequence[PanelRow],
    forward_rows: Sequence[ForwardReturnRow],
    *,
    horizon: str,
    years: float,
) -> list[_BandObservation]:
    by_ticker = {row.ticker: row for row in forward_rows if row.horizon == horizon}
    entries: list[_BandObservation] = []
    cumulative: dict[str, dict[str, float]] = {"price": {}, "total": {}}
    for row in panel:
        if not row.in_population or row.er_annual is None or not isfinite(row.er_annual):
            continue
        forward = by_ticker[row.ticker]
        if (
            forward.resolved != (forward.status in RESOLVED_STATUSES)
            or forward.total_return_status not in TOTAL_RETURN_STATUSES
        ):
            raise CalibrationContextError("forward status is inconsistent")
        price_annual = (
            annualize_return(forward.price_return, years=years)
            if forward.resolved and forward.price_return is not None
            else None
        )
        if forward.resolved and price_annual is None:
            raise CalibrationContextError("resolved price outcome is invalid")
        total_annual = None
        total_reason: str | None = (
            forward.status if not forward.resolved else forward.total_return_status
        )
        if forward.total_return_status == "resolved":
            if (
                not forward.resolved
                or forward.total_return_basis != TOTAL_RETURN_BASIS
                or forward.realized_dividend_sum is None
                or forward.realized_dividend_fy_count <= 0
                or forward.total_return is None
            ):
                raise CalibrationContextError("resolved dividend outcome is invalid")
            total_annual = annualize_return(forward.total_return, years=years)
            if total_annual is None:
                raise CalibrationContextError("resolved total outcome is invalid")
            total_reason = None
        entry = _BandObservation(
            ticker=row.ticker,
            er_annual=float(row.er_annual),
            total_annual=total_annual,
            price_annual=price_annual,
            total_reason=total_reason,
            price_reason=None if price_annual is not None else forward.status,
        )
        entries.append(entry)
        if price_annual is not None:
            assert forward.price_return is not None
            cumulative["price"][row.ticker] = forward.price_return
        if total_annual is not None:
            assert forward.total_return is not None
            cumulative["total"][row.ticker] = forward.total_return
    for basis, values in cumulative.items():
        if values:
            population_median = median(values.values())
            for entry in entries:
                if entry.ticker in values:
                    setattr(
                        entry,
                        f"{basis}_trap",
                        values[entry.ticker] - population_median <= TRAP_EXCESS_THRESHOLD,
                    )
    return entries


def _quintile_chunks(entries: Sequence[PanelRow]) -> list[list[PanelRow]]:
    step = len(entries) / 5
    return [list(entries[int(index * step) : int((index + 1) * step)]) for index in range(5)]


def _band_context(
    band_id: str,
    cohort_bands: Mapping[str, Mapping[str, Sequence[_BandObservation]]],
    *,
    predicted: Sequence[float],
    quintile: int | None,
    lower_er_annual: float | None,
    upper_er_annual: float | None,
) -> dict[str, object]:
    return {
        "band_id": band_id,
        "quintile": quintile,
        "lower_er_annual": _rounded(lower_er_annual),
        "upper_er_annual": _rounded(upper_er_annual),
        "median_predicted_er_annual": round(_nearest_rank(predicted, 0.5), 6)
        if predicted
        else None,
        "cohort_count": len(cohort_bands),
        # Prediction counts include the declared months with no observed outcomes.
        "median_n": int(
            _nearest_rank([float(len(bands[band_id])) for bands in cohort_bands.values()], 0.5)
        ),
        "bases": [
            _basis_context(
                basis,
                cohort_bands,
                band_id=band_id,
                value_field=value,
                trap_field=trap,
                reason_field=reason,
            )
            for basis, value, trap, reason in (
                (
                    ER_CALIBRATION_PRIMARY_REALIZED_BASIS,
                    "total_annual",
                    "total_trap",
                    "total_reason",
                ),
                (
                    ER_CALIBRATION_SECONDARY_REALIZED_BASIS,
                    "price_annual",
                    "price_trap",
                    "price_reason",
                ),
            )
        ],
    }


def _basis_context(
    basis: str,
    cohort_bands: Mapping[str, Mapping[str, Sequence[_BandObservation]]],
    *,
    band_id: str,
    value_field: str,
    trap_field: str,
    reason_field: str,
) -> dict[str, object]:
    pooled: list[float] = []
    pooled_traps: list[bool] = []
    cohort_stats: list[dict[str, float | int]] = []
    observed_counts: list[float] = []
    missing_reasons: Counter[str] = Counter()
    declared_n = 0
    for bands in cohort_bands.values():
        rows = bands[band_id]
        declared_n += len(rows)
        observed = [row for row in rows if getattr(row, value_field) is not None]
        values = [float(getattr(row, value_field)) for row in observed]
        traps = [bool(getattr(row, trap_field)) for row in observed]
        observed_counts.append(float(len(values)))
        pooled.extend(values)
        pooled_traps.extend(traps)
        for row in rows:
            if getattr(row, value_field) is None:
                reason = getattr(row, reason_field)
                assert isinstance(reason, str)
                missing_reasons[reason] += 1
        if values:
            cohort_stats.append(_stats(values, traps))
    return {
        "basis": basis,
        "coverage": {
            "declared_n": declared_n,
            "observed_n": len(pooled),
            "missing_n": declared_n - len(pooled),
            "missing_reason_counts": dict(sorted(missing_reasons.items())),
        },
        "observed_cohort_count": len(cohort_stats),
        "median_n": int(_nearest_rank(observed_counts, 0.5)),
        "ticker_equal": _stats(pooled, pooled_traps) if pooled else None,
        "cohort_equal": {
            field: (
                int(_nearest_rank([float(item[field]) for item in cohort_stats], 0.5))
                if field == "n"
                else round(_nearest_rank([float(item[field]) for item in cohort_stats], 0.5), 6)
            )
            for field in ("median", "q25", "q10", "trap_rate", "n")
        }
        if cohort_stats
        else None,
    }


def _stats(values: Sequence[float], traps: Sequence[bool]) -> dict[str, float | int]:
    if not values or len(values) != len(traps):
        raise CalibrationContextError("distribution values and traps must be non-empty and aligned")
    return {
        "median": round(_nearest_rank(values, 0.5), 6),
        "q25": round(_nearest_rank(values, 0.25), 6),
        "q10": round(_nearest_rank(values, 0.10), 6),
        "trap_rate": round(sum(traps) / len(traps), 6),
        "n": len(values),
    }


def _nearest_rank(values: Sequence[float], probability: float) -> float:
    if not values or not 0 < probability <= 1:
        raise CalibrationContextError("nearest-rank quantile requires values and 0 < p <= 1")
    ordered = sorted(values)
    return ordered[max(0, ceil(probability * len(ordered)) - 1)]


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 6)
