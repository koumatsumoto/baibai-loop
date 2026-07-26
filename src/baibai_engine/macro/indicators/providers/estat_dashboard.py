from __future__ import annotations

import json
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

# The dashboard marks a cell with no usable number using these tokens; each is skipped.
_DASHBOARD_NULL_MARKERS = frozenset({"", "-", "***", "X", "…", "..."})

# Selectors a registry entry must pin, because one IndicatorCode serves several
# series at once: monthly / quarterly / annual cycles crossed with raw,
# seasonally adjusted, and month-on-month forms. A request without them returns
# all of those in one list, so the selectors are required rather than defaulted.
_REQUIRED_SELECTORS = frozenset({"Cycle", "IsSeasonalAdjustment", "RegionCode"})

# Only the monthly cycle is read. Quarterly and annual codes exist in the same
# API, but nothing is registered against them and their time codes need their own
# parsing, so an unregistered cycle fails instead of being guessed at.
_MONTHLY_CYCLE = "1"


class EStatDashboardProvider:
    """統計ダッシュボード getData JSON (no credential). Japanese official monthly series.

    The dashboard is operated by 総務省統計局 and mirrors ministry statistics with a
    stable IndicatorCode, which makes it readable where the publishing ministry
    offers only per-release files. ``provider_series_id`` carries the IndicatorCode
    plus the selectors that pin one series out of the code's cycle / seasonal-
    adjustment family; every returned row is checked against them so a filter the
    API ignores fails the fetch instead of mixing two series into one.
    """

    spec = ProviderSpec(name="estat_dashboard", all_history_start=date(1948, 1, 1))
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
        indicator_code, selectors = _split_indicator_code(series.provider_series_id)
        params = {
            "Lang": "JP",
            "IndicatorCode": indicator_code,
            **selectors,
            # The API windows on monthly time codes, so a refresh asks only for the
            # window it stores.
            "TimeFrom": _monthly_time_code(start),
            "TimeTo": _monthly_time_code(end),
        }
        text = fetch_text(
            session,
            series.source_url,
            params=params,
            max_bytes=MAX_CSV_RESPONSE_BYTES,
            context=context,
        )
        return parse_dashboard_json(
            series,
            text,
            start=start,
            end=end,
            indicator_code=indicator_code,
            selectors=selectors,
        )


def _split_indicator_code(provider_series_id: str) -> tuple[str, dict[str, str]]:
    # provider_series_id is "<IndicatorCode>?Cycle=1&IsSeasonalAdjustment=2&RegionCode=00000".
    indicator_code, separator, query = provider_series_id.partition("?")
    if not indicator_code or not separator:
        raise IndicatorsProviderError(
            f"e-Stat dashboard series must pin selectors: {provider_series_id!r}"
        )
    selectors: dict[str, str] = {}
    for pair in query.split("&"):
        key, _, value = pair.partition("=")
        if not key or not value:
            continue
        if key not in _REQUIRED_SELECTORS:
            raise IndicatorsProviderError(f"unsupported e-Stat dashboard selector: {key}")
        selectors[key] = value
    missing = _REQUIRED_SELECTORS - selectors.keys()
    if missing:
        listed = ", ".join(sorted(missing))
        raise IndicatorsProviderError(f"e-Stat dashboard series is missing selector(s): {listed}")
    if selectors["Cycle"] != _MONTHLY_CYCLE:
        raise IndicatorsProviderError(
            f"e-Stat dashboard reads the monthly cycle only: Cycle={selectors['Cycle']}"
        )
    return indicator_code, selectors


def parse_dashboard_json(
    series: SeriesDefinition,
    text: str,
    *,
    start: date,
    end: date,
    indicator_code: str,
    selectors: Mapping[str, str],
) -> list[ObservationRecord]:
    entries = _extract_value_entries(text)
    observations: list[ObservationRecord] = []
    units: set[str] = set()
    for entry in entries:
        _require_requested_series(entry, indicator_code=indicator_code, selectors=selectors)
        units.add(str(entry.get("@unit")))
        observed_at = _parse_dashboard_month(entry.get("@time"))
        value = _parse_dashboard_value(entry.get("$"))
        if value is None or not start <= observed_at <= end:
            continue
        observations.append(record_observation(series, observed_at=observed_at, value=value))
    if len(units) > 1:
        listed = ", ".join(sorted(units))
        raise IndicatorsProviderError(f"e-Stat dashboard mixed units in one series: {listed}")
    return observations


def _extract_value_entries(text: str) -> list[Mapping[str, object]]:
    try:
        payload: object = json.loads(text)
    except json.JSONDecodeError as exc:
        raise IndicatorsProviderError(
            f"e-Stat dashboard response is not valid JSON: {exc}"
        ) from exc
    root = _require_mapping(payload, "response root")
    get_stats = _require_mapping(root.get("GET_STATS"), "GET_STATS")
    result = _require_mapping(get_stats.get("RESULT"), "RESULT")
    status = result.get("status")
    if status != "0":
        message = result.get("errorMsg")
        raise IndicatorsProviderError(f"e-Stat dashboard returned status {status}: {message}")
    statistical_data = _require_mapping(get_stats.get("STATISTICAL_DATA"), "STATISTICAL_DATA")
    data_inf = _require_mapping(statistical_data.get("DATA_INF"), "DATA_INF")
    data_obj = data_inf.get("DATA_OBJ")
    if isinstance(data_obj, dict):
        rows: list[object] = [data_obj]
    elif isinstance(data_obj, list):
        rows = cast(list[object], data_obj)
    else:
        raise IndicatorsProviderError("e-Stat dashboard response missing DATA_INF.DATA_OBJ list")
    _require_complete_result(statistical_data, returned=len(rows))
    return [
        _require_mapping(_require_mapping(row, "DATA_OBJ").get("VALUE"), "VALUE") for row in rows
    ]


def _require_complete_result(statistical_data: Mapping[str, object], *, returned: int) -> None:
    """Fail when the response carries fewer rows than it reports holding.

    A truncated page reads as a shorter history rather than as an error, which
    would silently move a percentile window, so the declared total must match.
    """

    result_inf = _require_mapping(statistical_data.get("RESULT_INF"), "RESULT_INF")
    raw_total = result_inf.get("TOTAL_NUMBER")
    try:
        total = int(str(raw_total))
    except ValueError as exc:
        raise IndicatorsProviderError(
            f"e-Stat dashboard reported a non-numeric TOTAL_NUMBER: {raw_total!r}"
        ) from exc
    if total != returned:
        raise IndicatorsProviderError(
            f"e-Stat dashboard returned {returned} rows for a declared total of {total}"
        )


def _require_requested_series(
    entry: Mapping[str, object],
    *,
    indicator_code: str,
    selectors: Mapping[str, str],
) -> None:
    """Reject a row the request did not ask for.

    The selectors carry the series identity, so a row that answers a different
    cycle, adjustment, indicator, or region means the filter did not apply. Mixing
    those into one series is worse than failing the refresh.
    """

    expected = (
        ("@indicator", indicator_code),
        ("@cycle", selectors["Cycle"]),
        ("@isSeasonal", selectors["IsSeasonalAdjustment"]),
        ("@regionCode", selectors["RegionCode"]),
    )
    for attribute, wanted in expected:
        actual = entry.get(attribute)
        if actual != wanted:
            raise IndicatorsProviderError(
                f"e-Stat dashboard returned {attribute}={actual!r} for requested {wanted!r}"
            )


def _require_mapping(node: object, label: str) -> Mapping[str, object]:
    if not isinstance(node, dict):
        raise IndicatorsProviderError(f"e-Stat dashboard response missing {label} object")
    return cast(Mapping[str, object], node)


def _monthly_time_code(value: date) -> str:
    return f"{value.year:04d}{value.month:02d}00"


def _parse_dashboard_month(raw: object) -> date:
    # Monthly time codes are the 4-digit year, the 2-digit month, then "00"
    # (e.g. "20260500" -> May 2026). An unreadable code is an error rather than a
    # skipped row, so a changed time axis cannot pass as a shorter history.
    if not isinstance(raw, str) or len(raw) != 8 or not raw.isdigit():
        raise IndicatorsProviderError(f"e-Stat dashboard monthly time code is unreadable: {raw!r}")
    month = int(raw[4:6])
    if not 1 <= month <= 12 or raw[6:] != "00":
        raise IndicatorsProviderError(f"e-Stat dashboard monthly time code is unreadable: {raw!r}")
    return date(int(raw[:4]), month, 1)


def _parse_dashboard_value(raw: object) -> float | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text in _DASHBOARD_NULL_MARKERS:
        return None
    return parse_float(text)
