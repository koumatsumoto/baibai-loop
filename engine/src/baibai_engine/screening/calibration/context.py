"""Materialize the expiring E[r] realized-distribution context."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from itertools import pairwise
from math import ceil, isclose, isfinite
from statistics import median
from typing import cast
from zoneinfo import ZoneInfo

from .authority import PRODUCTION_REQUIRED_METRICS
from .evaluation import MIN_AXIS_SAMPLE, TRAP_EXCESS_THRESHOLD
from .forward import TOTAL_RETURN_BASIS, ForwardReturnRow
from .horizons import require_horizon
from .panel import PanelRow

CONTEXT_KIND = "er-level-calibration-context"
CONTEXT_SCHEMA_VERSION = 2
CONTEXT_VALID_DAYS = 45
PRIMARY_REALIZED_BASIS = "fy_actual_dividend_total_return"
SECONDARY_REALIZED_BASIS = "price_return_only"
HURDLE_ER_ANNUAL = 0.085


class CalibrationContextError(ValueError):
    """Raised when eligible evidence cannot materialize a safe context artifact."""


def validate_er_distribution_context_payload(raw: Mapping[str, object]) -> bool:
    """Validate the versioned artifact before a judgment surface consumes it."""

    try:
        if (
            raw.get("kind") != CONTEXT_KIND
            or raw.get("schema_version") != CONTEXT_SCHEMA_VERSION
            or raw.get("primary_realized_basis") != PRIMARY_REALIZED_BASIS
            or raw.get("secondary_realized_basis") != SECONDARY_REALIZED_BASIS
            or raw.get("trap_basis") != "cohort_population_cumulative_return_excess_lte_minus_0_20"
            or raw.get("reference_horizon") != "3y"
        ):
            return False
        weighting = raw.get("weighting")
        if not isinstance(weighting, Mapping) or dict(weighting) != {
            "primary": "ticker_asof_observation_equal",
            "secondary": "cohort_equal",
        }:
            return False
        generated_at = raw.get("generated_at")
        valid_through = raw.get("valid_through")
        if not isinstance(generated_at, str) or not isinstance(valid_through, str):
            return False
        generated = datetime.fromisoformat(generated_at)
        if generated.tzinfo is None or generated.utcoffset() is None:
            return False
        expires = date.fromisoformat(valid_through)
        if expires != generated.astimezone(ZoneInfo("Asia/Tokyo")).date() + timedelta(
            days=CONTEXT_VALID_DAYS
        ):
            return False
        common = raw.get("common_window")
        if not isinstance(common, Mapping) or not _valid_common_window(common):
            return False
        horizons = raw.get("horizons")
        if (
            not isinstance(horizons, list)
            or len(horizons) != 2
            or not all(isinstance(item, Mapping) for item in horizons)
        ):
            return False
        typed_horizons = cast(list[Mapping[object, object]], horizons)
        if [item.get("horizon") for item in typed_horizons] != ["3y", "5y"]:
            return False
        return all(_valid_horizon_payload(item) for item in typed_horizons)
    except (TypeError, ValueError):
        return False


def _valid_common_window(raw: Mapping[object, object]) -> bool:
    count = raw.get("cohort_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return False
    start = raw.get("asof_start")
    end = raw.get("asof_end")
    horizons = raw.get("horizons")
    if not isinstance(horizons, list) or not all(isinstance(item, Mapping) for item in horizons):
        return False
    typed_horizons = cast(list[Mapping[object, object]], horizons)
    if count == 0:
        return start is None and end is None and not typed_horizons
    if not isinstance(start, str) or not isinstance(end, str):
        return False
    return (
        date.fromisoformat(start) <= date.fromisoformat(end)
        and [item.get("horizon") for item in typed_horizons] == ["3y", "5y"]
        and all(item.get("cohort_count") == count for item in typed_horizons)
        and all(_valid_horizon_payload(item) for item in typed_horizons)
    )


def _valid_horizon_payload(raw: Mapping[object, object]) -> bool:
    cohort_count = raw.get("cohort_count")
    start = raw.get("asof_start")
    end = raw.get("asof_end")
    bands = raw.get("bands")
    if (
        isinstance(cohort_count, bool)
        or not isinstance(cohort_count, int)
        or cohort_count <= 0
        or not isinstance(start, str)
        or not isinstance(end, str)
        or date.fromisoformat(start) > date.fromisoformat(end)
        or not isinstance(bands, list)
        or len(bands) != 6
        or not all(isinstance(item, Mapping) for item in bands)
    ):
        return False
    typed_bands = cast(list[Mapping[object, object]], bands)
    if [item.get("band_id") for item in typed_bands] != [
        "q1",
        "q2",
        "q3",
        "q4",
        "q5",
        "er_gte_8_5pct",
    ]:
        return False
    cutoffs = [item.get("upper_er_annual") for item in typed_bands[:4]]
    if not all(_finite_number(value) for value in cutoffs):
        return False
    numeric_cutoffs = [float(cast(float | int, value)) for value in cutoffs]
    if any(right <= left for left, right in pairwise(numeric_cutoffs)):
        return False
    expected_bounds: list[tuple[float | None, float | None]] = [
        (None, numeric_cutoffs[0]),
        (numeric_cutoffs[0], numeric_cutoffs[1]),
        (numeric_cutoffs[1], numeric_cutoffs[2]),
        (numeric_cutoffs[2], numeric_cutoffs[3]),
        (numeric_cutoffs[3], None),
        (HURDLE_ER_ANNUAL, None),
    ]
    return all(
        _valid_band_payload(
            item,
            expected_quintile=index if index <= 5 else None,
            expected_bounds=expected_bounds[index - 1],
            cohort_count=cohort_count,
        )
        for index, item in enumerate(typed_bands, start=1)
    )


def _valid_band_payload(
    raw: Mapping[object, object],
    *,
    expected_quintile: int | None,
    expected_bounds: tuple[float | None, float | None],
    cohort_count: int,
) -> bool:
    if (
        raw.get("quintile") != expected_quintile
        or not _same_optional_number(raw.get("lower_er_annual"), expected_bounds[0])
        or not _same_optional_number(raw.get("upper_er_annual"), expected_bounds[1])
        or raw.get("cohort_count") != cohort_count
        or not _finite_number(raw.get("median_predicted_er_annual"))
        or isinstance(raw.get("median_n"), bool)
        or not isinstance(raw.get("median_n"), int)
        or cast(int, raw.get("median_n")) <= 0
    ):
        return False
    bases = raw.get("bases")
    if (
        not isinstance(bases, list)
        or len(bases) != 2
        or not all(isinstance(item, Mapping) for item in bases)
    ):
        return False
    typed_bases = cast(list[Mapping[object, object]], bases)
    if [item.get("basis") for item in typed_bases] != [
        PRIMARY_REALIZED_BASIS,
        SECONDARY_REALIZED_BASIS,
    ]:
        return False
    return all(
        _valid_stats_payload(basis.get(weighting))
        for basis in typed_bases
        for weighting in ("ticker_equal", "cohort_equal")
    )


def _valid_stats_payload(raw: object) -> bool:
    if not isinstance(raw, Mapping):
        return False
    median_value = raw.get("median")
    q25 = raw.get("q25")
    q10 = raw.get("q10")
    trap_rate = raw.get("trap_rate")
    n = raw.get("n")
    return (
        _finite_number(median_value)
        and _finite_number(q25)
        and _finite_number(q10)
        and float(cast(float | int, q10))
        <= float(cast(float | int, q25))
        <= float(cast(float | int, median_value))
        and _finite_number(trap_rate)
        and 0 <= float(cast(float | int, trap_rate)) <= 1
        and not isinstance(n, bool)
        and isinstance(n, int)
        and n > 0
    )


def _finite_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int | float) and isfinite(value)


def _same_optional_number(value: object, expected: float | None) -> bool:
    if expected is None:
        return value is None
    return _finite_number(value) and isclose(
        float(cast(float | int, value)), expected, abs_tol=1e-12
    )


def build_er_distribution_context(
    evaluation: Mapping[str, object],
    panels: Mapping[str, Sequence[PanelRow]],
    forwards: Mapping[str, Sequence[ForwardReturnRow]],
    *,
    generated_at: datetime | None = None,
) -> dict[str, object]:
    """Build schema v2 from every horizon-specific eligible cohort."""

    decision = evaluation.get("production_decision")
    scope = evaluation.get("scope")
    if (
        not isinstance(decision, Mapping)
        or decision.get("evidence_status") != "eligible"
        or decision.get("production_change_allowed") is not True
    ):
        raise CalibrationContextError("production authority is not eligible")
    if not isinstance(scope, Mapping) or scope.get("run_purpose") != "production_decision":
        raise CalibrationContextError("context requires a production_decision evaluation")
    required_metrics = scope.get("required_metrics")
    required = {*PRODUCTION_REQUIRED_METRICS, "er_level_calibration"}
    if not isinstance(required_metrics, list) or not required.issubset(required_metrics):
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
    return {
        "kind": CONTEXT_KIND,
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "generated_at": now.isoformat(timespec="seconds"),
        "valid_through": (
            now.astimezone(ZoneInfo("Asia/Tokyo")).date() + timedelta(days=45)
        ).isoformat(),
        "reference_horizon": "3y",
        "screening_rules_hash": rules_hash,
        "er_model_version": er_model_version,
        "primary_realized_basis": PRIMARY_REALIZED_BASIS,
        "secondary_realized_basis": SECONDARY_REALIZED_BASIS,
        "trap_basis": "cohort_population_cumulative_return_excess_lte_minus_0_20",
        "weighting": {
            "primary": "ticker_asof_observation_equal",
            "secondary": "cohort_equal",
        },
        "common_window": {
            "asof_start": common_asofs[0] if common_asofs else None,
            "asof_end": common_asofs[-1] if common_asofs else None,
            "cohort_count": len(common_asofs),
            "horizons": common_horizons,
        },
        "horizons": horizons,
    }


def _eligible_asofs(evaluation: Mapping[str, object]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {"3y": [], "5y": []}
    level_eligible = _level_eligible_pairs(evaluation.get("results"))
    required_metrics = {*PRODUCTION_REQUIRED_METRICS, "er_level_calibration"}
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
        and row.status == "resolved"
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
                PRIMARY_REALIZED_BASIS,
                cohort_bands,
                band_id=band_id,
                value_field="total_annual",
                trap_field="total_trap",
            ),
            _basis_context(
                SECONDARY_REALIZED_BASIS,
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
