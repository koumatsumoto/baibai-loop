from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping
from datetime import date
from typing import cast

from baibai_engine.collection.economic_sources import SourceDefinition

from ..observations import NativeRecords
from .base import (
    MAX_CSV_RESPONSE_BYTES,
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    ProviderSpec,
    fetch_text,
)


class BojTimeSeriesSource:
    """Bank of Japan Time-Series Data Search API (no authentication)."""

    spec = ProviderSpec(name="boj_timeseries", all_history_start=date(1998, 1, 1))
    name = spec.name

    def fetch_records(
        self,
        series: SourceDefinition,
        *,
        start: date,
        end: date,
        session: HttpSession,
        context: FetchContext | None = None,
    ) -> NativeRecords:
        database, series_code = _split_provider_series_id(series.provider_series_id)
        params = {
            "format": "json",
            "lang": "en",
            "db": database,
            "code": series_code,
        }
        # The BOJ API rejects YYYYMM period params for quarterly series
        # ("Invalid frequency"); fetch the full quarterly series and filter in
        # the parser. Daily/monthly series accept and are narrowed by the params.
        if series.frequency != "quarterly":
            params["startDate"] = start.strftime("%Y%m")
            params["endDate"] = end.strftime("%Y%m")
        text = fetch_text(
            session,
            series.source_url,
            params=params,
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
    series: SourceDefinition,
    text: str,
    *,
    start: date,
    end: date,
    expected_series_code: str | None = None,
) -> NativeRecords:
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

    records: dict[date, list[dict[str, object]]] = defaultdict(list)
    # Keep the original period labels, numeric/null cells and response metadata.
    # The date key is a lookup coordinate, not a claimed publication timestamp.
    response_metadata = {key: value for key, value in root.items() if key != "RESULTSET"}
    series_metadata = {key: value for key, value in result.items() if key != "VALUES"}
    values_metadata = {
        key: value for key, value in values.items() if key not in {"SURVEY_DATES", "VALUES"}
    }
    for raw_date, raw_value in zip(survey_dates, raw_values, strict=True):
        observed_at = _parse_survey_date(raw_date, series.frequency)
        if start <= observed_at <= end:
            records[observed_at].append(
                {
                    "response_metadata": response_metadata,
                    "series_metadata": series_metadata,
                    "values_metadata": values_metadata,
                    "survey_date": raw_date,
                    "value": raw_value,
                }
            )
    return dict(records)


def _split_provider_series_id(provider_series_id: str) -> tuple[str, str]:
    database, separator, series_code = provider_series_id.partition(":")
    if not separator or not database or not series_code or ":" in series_code:
        raise IndicatorsProviderError("BOJ provider_series_id must use DATABASE:SERIES_CODE")
    return database, series_code


def _require_mapping(node: object, label: str) -> Mapping[str, object]:
    if not isinstance(node, dict):
        raise IndicatorsProviderError(f"BOJ response missing {label} object")
    return cast(Mapping[str, object], node)


def _parse_survey_date(raw: object, frequency: str) -> date:
    # BOJ SURVEY_DATES labels a data period: daily YYYYMMDD, monthly YYYYMM,
    # quarterly YYYYQQ (01..04). Frequency disambiguates the six-digit forms.
    # Month-start and the last-quarter-month start are our representative dates,
    # not provider-stated observation days or publication/availability timestamps.
    # BOJ documents the quarter-to-month display convention, but no day=01 rule.
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise IndicatorsProviderError(f"BOJ response has invalid survey date: {raw!r}")
    text = str(raw)
    if not text.isdigit():
        raise IndicatorsProviderError(f"BOJ response has invalid survey date: {raw!r}")
    try:
        match frequency:
            case "daily":
                if len(text) != 8:
                    raise ValueError("daily survey date must be YYYYMMDD")
                return date(int(text[:4]), int(text[4:6]), int(text[6:]))
            case "monthly":
                if len(text) != 6:
                    raise ValueError("monthly survey date must be YYYYMM")
                return date(int(text[:4]), int(text[4:6]), 1)
            case "quarterly":
                if len(text) != 6 or text[4] != "0" or not 1 <= int(text[5]) <= 4:
                    raise ValueError("quarterly survey date must be YYYY0Q with Q in 1..4")
                return date(int(text[:4]), int(text[5]) * 3, 1)
            case _:
                raise ValueError(f"unsupported boj_timeseries frequency: {frequency}")
    except ValueError as exc:
        raise IndicatorsProviderError(
            f"BOJ response has invalid survey date {raw!r} for frequency {frequency!r}: {exc}"
        ) from exc
