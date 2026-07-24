from __future__ import annotations

import json
import os
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
    ProviderSpec,
    fetch_text,
    parse_float,
    record_observation,
)

# e-Stat marks cells with no usable number using these tokens; each is skipped.
_ESTAT_NULL_MARKERS = frozenset({"", "-", "***", "X", "…", "..."})


class EStatProvider:
    """e-Stat getStatsData JSON (appId via ESTAT_APP_ID). Japan official statistics.

    The credential is read from the environment, not stored in the registry, so
    ``source_url`` carries only the base getStatsData endpoint and the appId plus
    statsDataId travel as query params.
    """

    spec = ProviderSpec(
        name="estat",
        all_history_start=date(1970, 1, 1),
        required_env=("ESTAT_APP_ID",),
    )
    name = spec.name

    def fetch(
        self,
        series: SeriesDefinition,
        *,
        start: date,
        end: date,
        session: HttpSession,
        context: FetchContext | None = None,
    ) -> list[ObservationRecord]:
        app_id = os.environ.get("ESTAT_APP_ID")
        if not app_id:
            raise IndicatorsProviderError("e-Stat appId not set: export ESTAT_APP_ID")
        stats_data_id, narrowing = _split_stats_data_id(series.provider_series_id)
        params = {
            "appId": app_id,
            "statsDataId": stats_data_id,
            "metaGetFlg": "N",
            "cntGetFlg": "N",
        }
        params.update(narrowing)
        text = fetch_text(
            session,
            series.source_url,
            params=params,
            max_bytes=MAX_CSV_RESPONSE_BYTES,
            context=context,
        )
        return parse_estat_json(series, text, start=start, end=end)


def _split_stats_data_id(provider_series_id: str) -> tuple[str, dict[str, str]]:
    # provider_series_id may carry e-Stat narrowing params after '?', e.g.
    # "0003427113?cdCat01=0001&cdArea=00000" to select the 全国 総合 CPI cell.
    if "?" not in provider_series_id:
        return provider_series_id, {}
    stats_data_id, query = provider_series_id.split("?", 1)
    narrowing: dict[str, str] = {}
    for pair in query.split("&"):
        key, _, value = pair.partition("=")
        # skip empty pairs; never let narrowing override the request identity params
        if key and value and key not in {"appId", "statsDataId"}:
            narrowing[key] = value
    return stats_data_id, narrowing


def parse_estat_json(
    series: SeriesDefinition, text: str, *, start: date, end: date
) -> list[ObservationRecord]:
    try:
        payload: object = json.loads(text)
    except json.JSONDecodeError as exc:
        raise IndicatorsProviderError(f"e-Stat response is not valid JSON: {exc}") from exc
    observations: list[ObservationRecord] = []
    for entry in _extract_value_entries(payload):
        if not isinstance(entry, dict):
            continue
        row = cast(Mapping[str, object], entry)
        observed_at = _parse_estat_month(row.get("@time"))
        if observed_at is None or not start <= observed_at <= end:
            continue
        value = _parse_estat_value(row.get("$"))
        if value is None:
            continue
        observations.append(record_observation(series, observed_at=observed_at, value=value))
    return observations


def _extract_value_entries(payload: object) -> list[object]:
    if not isinstance(payload, dict):
        raise IndicatorsProviderError("e-Stat response is not a JSON object")
    root = cast(Mapping[str, object], payload)
    get_stats_data = _require_mapping(root.get("GET_STATS_DATA"), "GET_STATS_DATA")
    statistical_data = _require_mapping(get_stats_data.get("STATISTICAL_DATA"), "STATISTICAL_DATA")
    data_inf = _require_mapping(statistical_data.get("DATA_INF"), "DATA_INF")
    value_node = data_inf.get("VALUE")
    if isinstance(value_node, dict):
        return [value_node]
    if isinstance(value_node, list):
        return cast(list[object], value_node)
    raise IndicatorsProviderError("e-Stat response missing DATA_INF.VALUE list")


def _require_mapping(node: object, label: str) -> Mapping[str, object]:
    if not isinstance(node, dict):
        raise IndicatorsProviderError(f"e-Stat response missing {label} object")
    return cast(Mapping[str, object], node)


def _parse_estat_month(raw: object) -> date | None:
    # e-Stat monthly time codes lead with the 4-digit year and end with the
    # 2-digit month (e.g. "2026000101" -> January 2026); anything else is skipped.
    if not isinstance(raw, str) or len(raw) < 6 or not raw.isdigit():
        return None
    month = int(raw[-2:])
    if not 1 <= month <= 12:
        return None
    return date(int(raw[:4]), month, 1)


def _parse_estat_value(raw: object) -> float | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text in _ESTAT_NULL_MARKERS:
        return None
    return parse_float(text)
