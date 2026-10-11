"""Pure same-series changes; unknown bases do not become improvement signals."""

from __future__ import annotations

from datetime import datetime

from .models import (
    VALUATION_METRICS,
    MetricComparison,
    Observation,
    ObservationHistory,
    RetailDynamicsAnalysis,
    utc_datetime,
)


def _compare(
    metric: str,
    latest: Observation | None,
    previous: Observation | None,
    basis: tuple[str, ...],
) -> MetricComparison:
    if latest is None:
        return MetricComparison(metric=metric, state="not_observed", reason="no_observation")
    latest_value = getattr(latest, metric)
    previous_value = getattr(previous, metric) if previous is not None else None
    latest_count = getattr(latest, f"{metric}_count")
    previous_count = getattr(previous, f"{metric}_count") if previous is not None else None
    values = {
        "metric": metric,
        "latest_value": latest_value,
        "previous_value": previous_value,
        "latest_count": latest_count,
        "previous_count": previous_count,
    }
    if latest_value is None:
        return MetricComparison.model_validate(
            {**values, "state": "missing", "reason": "latest_missing"}
        )
    if previous is None:
        return MetricComparison.model_validate(
            {**values, "state": "first_observation", "reason": "no_previous_observation"}
        )
    if previous_value is None:
        return MetricComparison.model_validate(
            {**values, "state": "recovered", "reason": "previous_missing"}
        )
    if (latest_count is None) != (previous_count is None):
        return MetricComparison.model_validate(
            {**values, "state": "not_comparable", "reason": "unknown_count"}
        )
    if latest_count is not None and previous_count is not None and latest_count != previous_count:
        return MetricComparison.model_validate(
            {**values, "state": "count_changed", "reason": "count_changed"}
        )
    for field in basis:
        current, old = getattr(latest, field), getattr(previous, field)
        if current is None or old is None or current == "" or old == "":
            return MetricComparison.model_validate(
                {**values, "state": "not_comparable", "reason": f"unknown_{field}"}
            )
        if current != old:
            return MetricComparison.model_validate(
                {**values, "state": "not_comparable", "reason": f"changed_{field}"}
            )
    delta = latest_value - previous_value
    return MetricComparison.model_validate(
        {
            **values,
            "comparable": True,
            "delta": delta,
            "state": "same_value" if delta == 0 else "increased" if delta > 0 else "decreased",
        }
    )


def analyze(history: ObservationHistory, *, evaluated_at: datetime) -> RetailDynamicsAnalysis:
    """Preserve both input rows; never combine distributor observations into votes."""
    evaluated_at = utc_datetime(evaluated_at)
    cutoff = utc_datetime(history.cutoff)
    if cutoff > evaluated_at:
        raise ValueError("analysis cutoff cannot be later than evaluation time")
    for series in (history.model_valuation, history.analyst_rating):
        for row in (series.latest, series.previous):
            if row is not None and (row.ticker != history.ticker or row.fetched_at_utc > cutoff):
                raise ValueError("analysis input outside ticker/cutoff")
        if series.previous is not None and (
            series.latest is None or series.previous.fetched_at_utc >= series.latest.fetched_at_utc
        ):
            raise ValueError("previous observation must precede latest")
    return RetailDynamicsAnalysis(
        evaluated_at=evaluated_at,
        inputs=history,
        model_valuation=tuple(
            _compare(
                metric,
                history.model_valuation.latest,
                history.model_valuation.previous,
                (
                    "distributor",
                    f"{metric}_origin",
                    "currency",
                    "unit",
                    "share_basis",
                    f"{metric}_period",
                ),
            )
            for metric in VALUATION_METRICS
        ),
        analyst_rating=_compare(
            "rating",
            history.analyst_rating.latest,
            history.analyst_rating.previous,
            (
                "distributor",
                "origin",
                "rating_scale_min",
                "rating_scale_max",
                "rating_scale_direction",
            ),
        ),
    )
