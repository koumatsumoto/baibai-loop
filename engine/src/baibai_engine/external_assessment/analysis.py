"""提供元ごとの機械比較。出所・尺度・basisの未知を推測で補完しない。"""

from __future__ import annotations

from datetime import datetime

from baibai_engine.external_assessment.observations.models import (
    QUANTS_METRICS,
    VALUATION_METRICS,
    Observation,
    utc_datetime,
)
from baibai_engine.external_assessment.tradingview.observations import FIELDS

from .models import (
    AnalystRatingAssessment,
    AnalystRatingHistory,
    ExternalAssessment,
    MetricComparison,
    ModelValuationAssessment,
    ModelValuationHistory,
    ObservationHistory,
    OriginGroup,
    OriginReference,
    QuantsScoreAssessment,
    QuantsScoreHistory,
    TradingViewAssessment,
    TradingViewHistory,
)

FORECAST_METRICS = tuple(
    name
    for name in FIELDS
    if name not in {"analyst_rating", "recommendation_total", "quote_close", "quote_currency"}
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
    latest_count = getattr(latest, f"{metric}_count", None)
    previous_count = getattr(previous, f"{metric}_count", None) if previous is not None else None
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


def _tradingview_comparison(metric: str, history: TradingViewHistory) -> MetricComparison:
    # Row-level period metadata does not establish a period for each metric.
    # Preserve received values and missingness, without any numerical delta path.
    latest, previous = history.latest, history.previous
    latest_value = getattr(latest, metric) if latest is not None else None
    previous_value = getattr(previous, metric) if previous is not None else None
    state = (
        "not_observed"
        if latest is None
        else "missing"
        if latest_value is None
        else "first_observation"
        if previous is None
        else "recovered"
        if previous_value is None
        else "not_comparable"
    )
    return MetricComparison.model_validate(
        {
            "metric": metric,
            "latest_value": latest_value,
            "previous_value": previous_value,
            "state": state,
            "reason": {
                "not_observed": "no_observation",
                "missing": "latest_missing",
                "first_observation": "no_previous_observation",
                "recovered": "previous_missing",
                "not_comparable": "metric_period_mapping_unconfirmed",
            }[state],
        }
    )


def analyze(history: ObservationHistory, *, evaluated_at: datetime) -> ExternalAssessment:
    evaluated_at, cutoff = utc_datetime(evaluated_at), utc_datetime(history.cutoff)
    if cutoff > evaluated_at:
        raise ValueError("analysis cutoff cannot be later than evaluation time")
    all_series: tuple[
        ModelValuationHistory | AnalystRatingHistory | QuantsScoreHistory | TradingViewHistory, ...
    ] = (
        *history.model_valuations,
        *history.analyst_ratings,
        *history.quants_scores,
        history.tradingview,
    )
    for series in all_series:
        for row in (series.latest, series.previous):
            if row is not None and (
                row.ticker != history.ticker or utc_datetime(row.fetched_at_utc) > cutoff
            ):
                raise ValueError("analysis input outside ticker/cutoff")
        if series.previous is not None and (
            series.latest is None or series.previous.fetched_at_utc >= series.latest.fetched_at_utc
        ):
            raise ValueError("previous observation must precede latest")
        if (
            series.latest is not None
            and series.previous is not None
            and getattr(series.latest, "distributor", None)
            != getattr(series.previous, "distributor", None)
        ):
            raise ValueError("previous observation must belong to the same distributor")
    grouped: dict[str, list[OriginReference]] = {}

    def origin_reference(
        origin: str | None, row: Observation | None, kind: str, metric: str
    ) -> None:
        if origin and row is not None:
            grouped.setdefault(origin, []).append(
                OriginReference(
                    distributor=row.distributor,
                    kind=kind,
                    metric=metric,
                    fetched_at_utc=row.fetched_at_utc,
                    source_url=row.source_url,
                )
            )

    models = []
    for model_pair in history.model_valuations:
        models.append(
            ModelValuationAssessment(
                inputs=model_pair,
                comparisons=tuple(
                    _compare(
                        metric,
                        model_pair.latest,
                        model_pair.previous,
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
            )
        )
        for metric in VALUATION_METRICS:
            row = model_pair.latest
            if row is not None and getattr(row, metric) is not None:
                origin_reference(getattr(row, f"{metric}_origin"), row, "model_valuation", metric)
    ratings = []
    for rating_pair in history.analyst_ratings:
        if rating_pair.latest is None:
            continue
        ratings.append(
            AnalystRatingAssessment(
                distributor=rating_pair.latest.distributor,
                inputs=rating_pair,
                comparison=_compare(
                    "rating",
                    rating_pair.latest,
                    rating_pair.previous,
                    (
                        "distributor",
                        "origin",
                        "rating_scale_min",
                        "rating_scale_max",
                        "rating_scale_direction",
                    ),
                ),
            )
        )
        if rating_pair.latest.rating is not None:
            origin_reference(
                rating_pair.latest.origin, rating_pair.latest, "analyst_rating", "rating"
            )
    # Absence of a saved response is not a received null. Keep the expected
    # Rakuten series visible without manufacturing an observation or receipt.
    if not any(item.distributor == "rakuten" for item in ratings):
        ratings.append(
            AnalystRatingAssessment(
                distributor="rakuten",
                inputs=AnalystRatingHistory(),
                comparison=_compare("rating", None, None, ()),
            )
        )
    scores = []
    for score_pair in history.quants_scores:
        scores.append(
            QuantsScoreAssessment(
                inputs=score_pair,
                comparisons=tuple(
                    _compare(
                        metric,
                        score_pair.latest,
                        score_pair.previous,
                        ("distributor", "origin", "score_scale_max", "composite_scale_max"),
                    )
                    for metric in (*QUANTS_METRICS, "composite_score", "industry_average_score")
                ),
            )
        )
        if score_pair.latest is not None:
            for metric in (*QUANTS_METRICS, "composite_score", "industry_average_score"):
                if getattr(score_pair.latest, metric) is not None:
                    origin_reference(
                        score_pair.latest.origin, score_pair.latest, "quants_score", metric
                    )
    tv = history.tradingview
    forecast_fields = (
        ()
        if tv.latest is None or tv.latest.fetch_status != "ok"
        else tuple(metric for metric in FORECAST_METRICS if getattr(tv.latest, metric) is not None)
    )
    availability = (
        "not_observed"
        if tv.latest is None
        else "unresolved"
        if tv.latest.fetch_status != "ok"
        else "forecast_fields_present"
        if forecast_fields
        else "all_null"
    )
    return ExternalAssessment(
        ticker=history.ticker,
        cutoff=cutoff,
        evaluated_at=evaluated_at,
        model_valuations=tuple(models),
        analyst_ratings=tuple(ratings),
        quants_scores=tuple(scores),
        tradingview=TradingViewAssessment(
            inputs=tv,
            availability=availability,
            forecast_fields=forecast_fields,
            comparisons=tuple(_tradingview_comparison(metric, tv) for metric in FORECAST_METRICS),
        ),
        origin_groups=tuple(
            OriginGroup(origin=origin, references=tuple(refs))
            for origin, refs in sorted(grouped.items())
        ),
    )
