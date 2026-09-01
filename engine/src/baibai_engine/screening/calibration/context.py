"""Materialize the expiring E[r] realized-distribution context."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from itertools import pairwise
from math import ceil, isfinite
from statistics import median
from typing import cast
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from baibai_engine.foundation.er_calibration_context import (
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

from .evaluation import MIN_AXIS_SAMPLE, TRAP_EXCESS_THRESHOLD
from .evidence import ESTIMATOR_POLICY_MANDATORY_METRICS, ESTIMATOR_POLICY_SUBJECT
from .forward import TOTAL_RETURN_BASIS, ForwardReturnRow
from .horizons import require_horizon
from .panel import PanelRow


class CalibrationContextError(ValueError):
    """Raised when eligible evidence cannot materialize a safe context artifact."""


def build_er_distribution_context(
    evaluation: Mapping[str, object],
    panels: Mapping[str, Sequence[PanelRow]],
    forwards: Mapping[str, Sequence[ForwardReturnRow]],
    *,
    generated_at: datetime | None = None,
) -> dict[str, object]:
    """Build schema v2 from every horizon-specific eligible cohort."""

    readiness = evaluation.get("evidence_readiness")
    scope = evaluation.get("scope")
    if (
        not isinstance(readiness, Mapping)
        or readiness.get("evidence_status") != "eligible"
        or readiness.get("evidence_complete") is not True
    ):
        raise CalibrationContextError("evidence is not complete")
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
    eligible = _eligible_asofs(evaluation)

    horizons: list[dict[str, object]] = []
    for horizon in ("3y", "5y"):
        asofs = eligible[horizon]
        if not asofs:
            raise CalibrationContextError(f"{horizon} has no eligible E[r] level cohorts")
        missing = [asof for asof in asofs if asof not in panels or asof not in forwards]
        if missing:
            raise CalibrationContextError(f"{horizon} evidence is missing for {missing[0]}")
        horizons.append(
            _horizon_context(
                horizon,
                asofs,
                panels=panels,
                forwards=forwards,
            )
        )

    now = generated_at or datetime.now(ZoneInfo("Asia/Tokyo"))
    if now.tzinfo is None or now.utcoffset() is None:
        raise CalibrationContextError("generated_at must include a timezone")
    common_asofs = sorted(set(eligible["3y"]).intersection(eligible["5y"]))
    common_horizons = (
        [
            _horizon_context(
                horizon,
                common_asofs,
                panels=panels,
                forwards=forwards,
            )
            for horizon in ("3y", "5y")
        ]
        if common_asofs
        else []
    )
    payload: dict[str, object] = {
        "kind": ER_CALIBRATION_CONTEXT_KIND,
        "schema_version": ER_CALIBRATION_CONTEXT_SCHEMA_VERSION,
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
            "asof_start": common_asofs[0] if common_asofs else None,
            "asof_end": common_asofs[-1] if common_asofs else None,
            "cohort_count": len(common_asofs),
            "horizons": common_horizons,
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


def _eligible_asofs(evaluation: Mapping[str, object]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {"3y": [], "5y": []}
    level_eligible = _level_eligible_pairs(evaluation.get("results"))
    required_metrics = {*ESTIMATOR_POLICY_MANDATORY_METRICS, "er_level_calibration"}
    raw = evaluation.get("cohort_integrity")
    if not isinstance(raw, list):
        raise CalibrationContextError("cohort integrity is missing")
    for item in raw:
        if not isinstance(item, dict):
            continue
        horizon = item.get("horizon")
        asof = item.get("asof")
        metric_statuses = item.get("metric_statuses")
        if (
            horizon in result
            and isinstance(asof, str)
            and item.get("integrity_status") == "eligible"
            and isinstance(metric_statuses, Mapping)
            and all(metric_statuses.get(metric) == "eligible" for metric in required_metrics)
            and (asof, horizon) in level_eligible
        ):
            result[cast(str, horizon)].append(asof)
    for values in result.values():
        values.sort()
    return result


def _level_eligible_pairs(raw: object) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    if not isinstance(raw, dict):
        raise CalibrationContextError("evaluation results are missing")
    for horizon in ("3y", "5y"):
        result = raw.get(horizon)
        cohorts = result.get("cohorts") if isinstance(result, dict) else None
        if not isinstance(cohorts, list):
            raise CalibrationContextError(f"{horizon} evaluation cohorts are missing")
        for cohort in cohorts:
            if not isinstance(cohort, dict):
                continue
            asof = cohort.get("asof")
            statuses = cohort.get("metric_statuses")
            if (
                isinstance(asof, str)
                and isinstance(statuses, dict)
                and statuses.get("er_level_calibration") == "eligible"
            ):
                pairs.add((asof, horizon))
    return pairs


def _horizon_context(
    horizon: str,
    asofs: Sequence[str],
    *,
    panels: Mapping[str, Sequence[PanelRow]],
    forwards: Mapping[str, Sequence[ForwardReturnRow]],
) -> dict[str, object]:
    years = require_horizon(horizon).months / 12
    cohort_bands: dict[str, dict[str, list[_BandObservation]]] = {}
    boundaries: list[list[float]] = [[] for _ in range(4)]
    predicted: dict[str, list[float]] = {f"q{index}": [] for index in range(1, 6)}
    predicted["er_gte_8_5pct"] = []

    for asof in asofs:
        entries = _cohort_entries(panels[asof], forwards[asof], horizon=horizon, years=years)
        if len(entries) < MIN_AXIS_SAMPLE:
            raise CalibrationContextError(f"{horizon} {asof} has insufficient level observations")
        entries.sort(key=lambda item: (item.er_annual, item.ticker))
        chunks = _quintile_chunks(entries)
        band_rows: dict[str, list[_BandObservation]] = {}
        for index, chunk in enumerate(chunks, start=1):
            band_id = f"q{index}"
            band_rows[band_id] = chunk
            predicted[band_id].extend(item.er_annual for item in chunk)
            if index < 5:
                boundaries[index - 1].append(max(item.er_annual for item in chunk))
        hurdle = [item for item in entries if item.er_annual >= HURDLE_ER_ANNUAL]
        band_rows["er_gte_8_5pct"] = hurdle
        predicted["er_gte_8_5pct"].extend(item.er_annual for item in hurdle)
        cohort_bands[asof] = band_rows

    fixed_boundaries = [_nearest_rank(values, 0.5) for values in boundaries]
    if any(right <= left for left, right in pairwise(fixed_boundaries)):
        raise CalibrationContextError(f"{horizon} fixed quintile boundaries are not increasing")

    bands: list[dict[str, object]] = []
    for index in range(1, 6):
        band_id = f"q{index}"
        bands.append(
            _band_context(
                band_id,
                cohort_bands,
                predicted=predicted[band_id],
                quintile=index,
                lower_er_annual=None if index == 1 else fixed_boundaries[index - 2],
                upper_er_annual=None if index == 5 else fixed_boundaries[index - 1],
            )
        )
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
    __slots__ = ("er_annual", "price_annual", "price_trap", "ticker", "total_annual", "total_trap")

    def __init__(
        self,
        *,
        ticker: str,
        er_annual: float,
        total_annual: float,
        price_annual: float,
        total_trap: bool,
        price_trap: bool,
    ) -> None:
        self.ticker = ticker
        self.er_annual = er_annual
        self.total_annual = total_annual
        self.price_annual = price_annual
        self.total_trap = total_trap
        self.price_trap = price_trap


def _cohort_entries(
    panel: Sequence[PanelRow],
    forward_rows: Sequence[ForwardReturnRow],
    *,
    horizon: str,
    years: float,
) -> list[_BandObservation]:
    by_ticker = {
        row.ticker: row
        for row in forward_rows
        if row.horizon == horizon
        and row.resolved
        and row.total_return_status == "resolved"
        and row.total_return_basis == TOTAL_RETURN_BASIS
        and row.realized_dividend_sum is not None
        and row.realized_dividend_fy_count > 0
        and row.price_return is not None
        and row.total_return is not None
    }
    raw: list[tuple[PanelRow, ForwardReturnRow, float, float]] = []
    for row in panel:
        forward = by_ticker.get(row.ticker)
        if not row.in_population or forward is None or row.er_annual is None:
            continue
        assert forward.price_return is not None
        assert forward.total_return is not None
        price_annual = _annualize(forward.price_return, years=years)
        total_annual = _annualize(forward.total_return, years=years)
        if price_annual is None or total_annual is None or not isfinite(row.er_annual):
            continue
        raw.append((row, forward, total_annual, price_annual))
    if not raw:
        return []
    total_population_median = median(float(item[1].total_return or 0.0) for item in raw)
    price_population_median = median(float(item[1].price_return or 0.0) for item in raw)
    return [
        _BandObservation(
            ticker=row.ticker,
            er_annual=float(row.er_annual or 0.0),
            total_annual=total_annual,
            price_annual=price_annual,
            total_trap=(float(forward.total_return or 0.0) - total_population_median)
            <= TRAP_EXCESS_THRESHOLD,
            price_trap=(float(forward.price_return or 0.0) - price_population_median)
            <= TRAP_EXCESS_THRESHOLD,
        )
        for row, forward, total_annual, price_annual in raw
    ]


def _quintile_chunks(entries: Sequence[_BandObservation]) -> list[list[_BandObservation]]:
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
    if not predicted:
        raise CalibrationContextError(f"{band_id} has no observations")
    return {
        "band_id": band_id,
        "quintile": quintile,
        "lower_er_annual": _rounded(lower_er_annual),
        "upper_er_annual": _rounded(upper_er_annual),
        "median_predicted_er_annual": round(_nearest_rank(predicted, 0.5), 6),
        "cohort_count": len(cohort_bands),
        "median_n": int(
            _nearest_rank([float(len(bands[band_id])) for bands in cohort_bands.values()], 0.5)
        ),
        "bases": [
            _basis_context(
                ER_CALIBRATION_PRIMARY_REALIZED_BASIS,
                cohort_bands,
                band_id=band_id,
                value_field="total_annual",
                trap_field="total_trap",
            ),
            _basis_context(
                ER_CALIBRATION_SECONDARY_REALIZED_BASIS,
                cohort_bands,
                band_id=band_id,
                value_field="price_annual",
                trap_field="price_trap",
            ),
        ],
    }


def _basis_context(
    basis: str,
    cohort_bands: Mapping[str, Mapping[str, Sequence[_BandObservation]]],
    *,
    band_id: str,
    value_field: str,
    trap_field: str,
) -> dict[str, object]:
    pooled: list[float] = []
    pooled_traps: list[bool] = []
    cohort_stats: list[dict[str, float | int]] = []
    for bands in cohort_bands.values():
        rows = bands[band_id]
        values = [float(getattr(row, value_field)) for row in rows]
        traps = [bool(getattr(row, trap_field)) for row in rows]
        pooled.extend(values)
        pooled_traps.extend(traps)
        if values:
            cohort_stats.append(_stats(values, traps))
    if not pooled or len(cohort_stats) != len(cohort_bands):
        raise CalibrationContextError(f"{band_id} {basis} does not cover every eligible cohort")
    return {
        "basis": basis,
        "ticker_equal": _stats(pooled, pooled_traps),
        "cohort_equal": {
            field: (
                int(_nearest_rank([float(item[field]) for item in cohort_stats], 0.5))
                if field == "n"
                else round(_nearest_rank([float(item[field]) for item in cohort_stats], 0.5), 6)
            )
            for field in ("median", "q25", "q10", "trap_rate", "n")
        },
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


def _annualize(value: float, *, years: float) -> float | None:
    if years <= 0 or value < -1:
        return None
    result = (1 + value) ** (1 / years) - 1
    return float(result) if isfinite(result) else None


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 6)
