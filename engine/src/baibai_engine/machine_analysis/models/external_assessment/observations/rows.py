"""Encode received observations as SQL rows without opening a store."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from baibai_engine.machine_analysis.models.external_assessment.observations.contract import (
    ANALYST_FORECAST_COLUMNS,
    ANALYST_FORECAST_TABLE,
    ANALYST_RATING_COLUMNS,
    ANALYST_RATING_TABLE,
    MODEL_VALUATION_COLUMNS,
    MODEL_VALUATION_TABLE,
    QUANTS_SCORE_COLUMNS,
    QUANTS_SCORE_TABLE,
)
from baibai_engine.machine_analysis.models.external_assessment.observations.models import (
    AnalystForecastObservation,
    AnalystRatingObservation,
    ModelValuationObservation,
    Observation,
    QuantsScoreObservation,
)


def observation_row(observation: Observation) -> tuple[str, tuple[str, ...], tuple[Any, ...]]:
    """Use the same column order and timestamp spelling for append and L1 replay."""
    if isinstance(observation, ModelValuationObservation):
        table, columns = MODEL_VALUATION_TABLE, MODEL_VALUATION_COLUMNS
    elif isinstance(observation, AnalystRatingObservation):
        table, columns = ANALYST_RATING_TABLE, ANALYST_RATING_COLUMNS
    elif isinstance(observation, AnalystForecastObservation):
        table, columns = ANALYST_FORECAST_TABLE, ANALYST_FORECAST_COLUMNS
    elif isinstance(observation, QuantsScoreObservation):
        table, columns = QUANTS_SCORE_TABLE, QUANTS_SCORE_COLUMNS
    else:
        raise TypeError("unsupported external assessment observation")
    names = tuple(column[0] for column in columns)
    payload = observation.model_dump()
    values = tuple(
        value.isoformat(timespec="microseconds")
        if isinstance(value, datetime)
        else value.isoformat()
        if isinstance(value, date)
        else value
        for name in names
        for value in (payload[name],)
    )
    return table, names, values
