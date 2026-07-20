from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import date
from typing import cast

from ..db import ObservationRecord
from ..definitions import SeriesDefinition
from .base import (
    MAX_CSV_RESPONSE_BYTES,
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    fetch_text,
    record_observation,
)


class BojTimeSeriesProvider:
    """Bank of Japan Time-Series Data Search API (no authentication)."""

    name = "boj_timeseries"

    def fetch(
        self,
        series: SeriesDefinition,
        *,
        start: date,
        end: date,
        session: HttpSession,
        context: FetchContext | None = None,
    ) -> list[ObservationRecord]:
        database, series_code = _split_provider_series_id(series.provider_series_id)
        text = fetch_text(
            session,
            series.source_url,
            params={
                "format": "json",
                "lang": "en",
                "db": database,
                "startDate": start.strftime("%Y%m"),
                "endDate": end.strftime("%Y%m"),
                "code": series_code,
            },
            max_bytes=MAX_CSV_RESPONSE_BYTES,
            context=context,
        )
        return parse_boj_timeseries_json(
            series,
            text,
            start=start,
            end=end,
            expected_series_code=series_code,
        )


def parse_boj_timeseries_json(
    series: SeriesDefinition,
    text: str,
    *,
    start: date,
    end: date,
    expected_series_code: str | None = None,
) -> list[ObservationRecord]:
    try:
        payload: object = json.loads(text)
    except json.JSONDecodeError as exc:
        raise IndicatorsProviderError(f"BOJ response is not valid JSON: {exc}") from exc
    root = _require_mapping(payload, "root")
    if root.get("STATUS") != 200:
        raise IndicatorsProviderError(
            f"BOJ API failed with status {root.get('STATUS')!r}: {root.get('MESSAGE')!r}"
        )
    result_set = root.get("RESULTSET")
    if not isinstance(result_set, list) or not result_set:
        raise IndicatorsProviderError("BOJ response missing RESULTSET list")
    expected = expected_series_code or _split_provider_series_id(series.provider_series_id)[1]
    result = next(
        (
            _require_mapping(item, "RESULTSET entry")
            for item in result_set
            if isinstance(item, dict) and item.get("SERIES_CODE") == expected
        ),
        None,
    )
    if result is None:
        raise IndicatorsProviderError(f"BOJ response missing series {expected}")
    values = _require_mapping(result.get("VALUES"), "VALUES")
    survey_dates = values.get("SURVEY_DATES")
    raw_values = values.get("VALUES")
    if not isinstance(survey_dates, list) or not isinstance(raw_values, list):
        raise IndicatorsProviderError("BOJ response VALUES must contain two lists")
    if len(survey_dates) != len(raw_values):
        raise IndicatorsProviderError("BOJ response date and value lengths differ")

    observations: list[ObservationRecord] = []
    for raw_date, raw_value in zip(survey_dates, raw_values, strict=True):
        if raw_value is None:
            continue
        observed_at = _parse_survey_date(raw_date)
        value = _parse_value(raw_value)
        if start <= observed_at <= end:
            observations.append(record_observation(series, observed_at=observed_at, value=value))
    return observations


def _split_provider_series_id(provider_series_id: str) -> tuple[str, str]:
    database, separator, series_code = provider_series_id.partition(":")
    if not separator or not database or not series_code or ":" in series_code:
        raise IndicatorsProviderError("BOJ provider_series_id must use DATABASE:SERIES_CODE")
    return database, series_code


def _require_mapping(node: object, label: str) -> Mapping[str, object]:
    if not isinstance(node, dict):
        raise IndicatorsProviderError(f"BOJ response missing {label} object")
    return cast(Mapping[str, object], node)


def _parse_survey_date(raw: object) -> date:
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise IndicatorsProviderError(f"BOJ response has invalid survey date: {raw!r}")
    text = str(raw)
    if len(text) != 8 or not text.isdigit():
        raise IndicatorsProviderError(f"BOJ response has invalid survey date: {raw!r}")
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError as exc:
        raise IndicatorsProviderError(f"BOJ response has invalid survey date: {raw!r}") from exc


def _parse_value(raw: object) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise IndicatorsProviderError(f"BOJ response has invalid value: {raw!r}")
    value = float(raw)
    if not math.isfinite(value):
        raise IndicatorsProviderError(f"BOJ response has invalid value: {raw!r}")
    return value
