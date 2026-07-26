from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from datetime import date
from typing import cast

from baibai_engine.foundation.env import load_project_env

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

# Time codes are the year, the period kind, and the first and last month of the
# period. "00" is the monthly kind; a year total is either the calendar-year kind
# or the monthly kind with no month named.
_ESTAT_TIME_CODE_RE = re.compile(r"(?P<year>\d{4})(?P<kind>\d{2})(?P<first>\d{2})(?P<last>\d{2})")
_ESTAT_MONTHLY_KIND = "00"
_ESTAT_NO_MONTH = "00"

# Every returned cell echoes the dimension codes it belongs to, so the narrowing
# a registry entry asks for can be checked against what came back. e-Stat answers
# a narrowing code the table does not define by leaving that dimension open
# rather than by failing, and the store's upsert would then let the last cell of
# each period silently win. Only dimensions that echo a single code are mappable;
# an unmapped narrowing key is refused so that adding one is a deliberate act.
_NARROWING_ROW_ATTRIBUTES = {
    "cdTab": "@tab",
    "cdArea": "@area",
    "cdTime": "@time",
} | {f"cdCat{index:02d}": f"@cat{index:02d}" for index in range(1, 16)}


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
        # Self-load .env so the provider works whether or not the caller already
        # did (the jquants providers follow the same pattern); CI passes the key
        # via the environment directly.
        load_project_env()
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
    _, narrowing = _split_stats_data_id(series.provider_series_id)
    expectations = _narrowing_row_expectations(narrowing)
    observations: list[ObservationRecord] = []
    for entry in _extract_value_entries(payload):
        if not isinstance(entry, dict):
            continue
        row = cast(Mapping[str, object], entry)
        _require_narrowed_cell(row, expectations=expectations, series=series)
        observed_at = _parse_estat_month(row.get("@time"), series=series)
        if observed_at is None or not start <= observed_at <= end:
            continue
        value = _parse_estat_value(row.get("$"))
        if value is None:
            continue
        observations.append(record_observation(series, observed_at=observed_at, value=value))
    return observations


def _narrowing_row_expectations(narrowing: Mapping[str, str]) -> dict[str, str]:
    expectations: dict[str, str] = {}
    for key, value in narrowing.items():
        attribute = _NARROWING_ROW_ATTRIBUTES.get(key)
        if attribute is None:
            raise IndicatorsProviderError(
                f"e-Stat narrowing key {key} has no row attribute to check the answer against"
            )
        expectations[attribute] = value
    return expectations


def _require_narrowed_cell(
    row: Mapping[str, object],
    *,
    expectations: Mapping[str, str],
    series: SeriesDefinition,
) -> None:
    for attribute, wanted in expectations.items():
        answered = row.get(attribute)
        if answered != wanted:
            raise IndicatorsProviderError(
                f"e-Stat answered {series.series_id} with a cell outside the requested "
                f"narrowing ({attribute}={answered!r}, requested {wanted!r})"
            )


def _extract_value_entries(payload: object) -> list[object]:
    if not isinstance(payload, dict):
        raise IndicatorsProviderError("e-Stat response is not a JSON object")
    root = cast(Mapping[str, object], payload)
    get_stats_data = _require_mapping(root.get("GET_STATS_DATA"), "GET_STATS_DATA")
    _require_successful_result(get_stats_data)
    statistical_data = _require_mapping(get_stats_data.get("STATISTICAL_DATA"), "STATISTICAL_DATA")
    _require_whole_series(statistical_data)
    data_inf = _require_mapping(statistical_data.get("DATA_INF"), "DATA_INF")
    value_node = data_inf.get("VALUE")
    if isinstance(value_node, dict):
        return [value_node]
    if isinstance(value_node, list):
        return cast(list[object], value_node)
    raise IndicatorsProviderError("e-Stat response missing DATA_INF.VALUE list")


def _require_successful_result(get_stats_data: Mapping[str, object]) -> None:
    # e-Stat reports a rejected request with a 200 and a non-zero STATUS, so the
    # message it carries is the only readable account of what was wrong.
    result = _require_mapping(get_stats_data.get("RESULT"), "RESULT")
    status = result.get("STATUS")
    if status != 0:
        message = result.get("ERROR_MSG")
        raise IndicatorsProviderError(f"e-Stat request failed (status={status!r}): {message!r}")


def _require_whole_series(statistical_data: Mapping[str, object]) -> None:
    # A response past e-Stat's row cap carries a resume key. Reading only the
    # first page would look like a series that simply stops, so it is refused.
    result_inf = statistical_data.get("RESULT_INF")
    if isinstance(result_inf, dict) and "NEXT_KEY" in result_inf:
        raise IndicatorsProviderError(
            "e-Stat response is one page of a longer result; narrow the series or add paging"
        )


def _require_mapping(node: object, label: str) -> Mapping[str, object]:
    if not isinstance(node, dict):
        raise IndicatorsProviderError(f"e-Stat response missing {label} object")
    return cast(Mapping[str, object], node)


def _parse_estat_month(raw: object, *, series: SeriesDefinition) -> date | None:
    """The month an e-Stat cell belongs to, or None when the cell is an aggregate.

    A time code is the year, a two-digit period kind, and the first and last month
    of the period ("2026000101" is January 2026). Calendar-year and fiscal-year
    totals share the table with the months and carry no month, so they are
    skipped. Everything else is refused, because a month read as an aggregate
    disappears from the series without a trace.
    """

    if not isinstance(raw, str):
        raise IndicatorsProviderError(f"e-Stat gave {series.series_id} a non-string time code")
    match = _ESTAT_TIME_CODE_RE.fullmatch(raw)
    if match is None:
        raise IndicatorsProviderError(
            f"e-Stat gave {series.series_id} a time code it cannot place (time={raw!r})"
        )
    if match["kind"] != _ESTAT_MONTHLY_KIND or match["first"] == _ESTAT_NO_MONTH:
        return None
    month = int(match["first"])
    # The publisher does not always fill the closing month in, so an open close
    # is read as the month itself; a close that widens the period is not a month.
    if not 1 <= month <= 12 or match["last"] not in {match["first"], _ESTAT_NO_MONTH}:
        raise IndicatorsProviderError(
            f"e-Stat gave {series.series_id} a monthly cell spanning more than "
            f"one month (time={raw!r})"
        )
    return date(int(match["year"]), month, 1)


def _parse_estat_value(raw: object) -> float | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text in _ESTAT_NULL_MARKERS:
        return None
    return parse_float(text)
