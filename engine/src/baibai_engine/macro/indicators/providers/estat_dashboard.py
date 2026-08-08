from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import date
from typing import cast
from urllib.parse import parse_qsl, urlsplit

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
# Anything else that is not one of these strings is a change in the response shape.
_DASHBOARD_NULL_MARKERS = frozenset({"", "-", "***", "X", "…", "..."})

# Selectors a registry entry must pin, because one IndicatorCode serves several
# series at once: monthly / quarterly / annual cycles crossed with raw and
# seasonally adjusted forms. A request without them returns all of those in one
# list, so the selectors are required rather than defaulted.
_REQUIRED_SELECTORS = ("Cycle", "IsSeasonalAdjustment", "RegionCode")
_SOURCE_URL_KEYS = frozenset({"Lang", "IndicatorCode", *_REQUIRED_SELECTORS})

# Only the monthly cycle is read. Quarterly and annual codes exist in the same
# API, but nothing is registered against them and their time codes need their own
# parsing, so an unregistered cycle fails instead of being guessed at.
_MONTHLY_CYCLE = "1"

# The dashboard marks a preliminary print with "1". Registered series declare a
# publication lag that belongs to the final print, so a preliminary row would make
# an observation appear weeks early and keep the series looking fresh after the
# final print stopped. A month that has only a preliminary print is therefore left
# unwritten and picked up when the final print lands — the declared lag already
# says the month is not due yet, so this is a publication state rather than a
# failed fetch.
_FINAL_PRINT = "0"

# Every request opens at this floor even when a narrower window is asked for. The
# API answers "no data" with the same status and message it uses for an unknown
# IndicatorCode or an invalid selector, so a request that can legitimately come back
# empty would make a mis-pinned series indistinguishable from a quiet one. A
# registered series always has observations from the floor, which keeps that status a
# real failure. The published history of one indicator is a few hundred KB.
_HISTORY_FLOOR = date(1948, 1, 1)

_LOGGER = logging.getLogger(__name__)


class EStatDashboardProvider:
    """統計ダッシュボード getData JSON (no credential). Japanese official monthly series.

    The dashboard is operated by 総務省統計局 and mirrors ministry statistics with a
    stable IndicatorCode, which makes it readable where the publishing ministry
    offers only per-release files. ``source_url`` carries the IndicatorCode plus the
    selectors that pin one series out of the code's cycle / seasonal-adjustment
    family, so a stored observation names the upstream series it came from and a
    corrected selector rewrites the series instead of layering onto it. Every
    returned row is checked against those selectors, so a filter the API ignores
    fails the fetch instead of mixing two series into one.

    An index the publisher rebases keeps its selectors, so a windowed refresh would
    write recent months on the new base while older months stay on the old one. A
    rebase is therefore refreshed with ``--all-history``, which rewrites the whole
    series on one base.
    """

    spec = ProviderSpec(name="estat_dashboard", all_history_start=_HISTORY_FLOOR)
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
        selectors = source_url_selectors(series)
        text = fetch_text(
            session,
            series.source_url,
            # The source URL already carries the series identity; only the window
            # rides on the request, and it always opens at the history floor.
            params={
                "TimeFrom": _monthly_time_code(_HISTORY_FLOOR),
                "TimeTo": _monthly_time_code(end),
            },
            max_bytes=MAX_CSV_RESPONSE_BYTES,
            context=context,
        )
        return parse_dashboard_json(
            series,
            text,
            start=start,
            end=end,
            selectors=selectors,
        )


def source_url_selectors(series: SeriesDefinition) -> Mapping[str, str]:
    """Read the series identity out of the source URL, or fail.

    The URL is the provenance recorded on every observation, so it has to be the
    one place the identity lives: a selector kept anywhere else would let the
    recorded provenance and the fetched series drift apart.
    """

    if series.frequency != "monthly":
        raise IndicatorsProviderError(
            f"e-Stat dashboard serves monthly series only: {series.series_id} is {series.frequency}"
        )
    query = urlsplit(series.source_url).query
    try:
        pairs = parse_qsl(query, strict_parsing=True, keep_blank_values=True)
    except ValueError as exc:
        raise IndicatorsProviderError(
            f"e-Stat dashboard source URL has an unreadable query: {series.source_url}"
        ) from exc
    selectors: dict[str, str] = {}
    for key, value in pairs:
        if key not in _SOURCE_URL_KEYS:
            raise IndicatorsProviderError(f"unsupported e-Stat dashboard selector: {key}")
        if key in selectors:
            raise IndicatorsProviderError(f"e-Stat dashboard selector is repeated: {key}")
        if not value:
            raise IndicatorsProviderError(f"e-Stat dashboard selector is empty: {key}")
        selectors[key] = value
    missing = [key for key in sorted(_SOURCE_URL_KEYS) if key not in selectors]
    if missing:
        raise IndicatorsProviderError(
            f"e-Stat dashboard source URL is missing selector(s): {', '.join(missing)}"
        )
    if selectors["IndicatorCode"] != series.provider_series_id:
        raise IndicatorsProviderError(
            f"e-Stat dashboard source URL requests {selectors['IndicatorCode']} "
            f"for series {series.provider_series_id}"
        )
    if selectors["Cycle"] != _MONTHLY_CYCLE:
        raise IndicatorsProviderError(
            f"e-Stat dashboard reads the monthly cycle only: Cycle={selectors['Cycle']}"
        )
    return selectors


def parse_dashboard_json(
    series: SeriesDefinition,
    text: str,
    *,
    start: date,
    end: date,
    selectors: Mapping[str, str],
) -> list[ObservationRecord]:
    entries = _extract_value_entries(text)
    observations: list[ObservationRecord] = []
    units: set[str] = set()
    preliminary: list[date] = []
    for entry in entries:
        _require_requested_series(entry, selectors=selectors)
        observed_at = _parse_dashboard_month(entry.get("@time"))
        if entry.get("@isProvisional") != _FINAL_PRINT:
            if start <= observed_at <= end:
                preliminary.append(observed_at)
            continue
        units.add(str(entry.get("@unit")))
        value = _parse_dashboard_value(entry.get("$"))
        if value is None or not start <= observed_at <= end:
            continue
        observations.append(record_observation(series, observed_at=observed_at, value=value))
    if preliminary:
        _LOGGER.warning(
            "e-Stat dashboard has only a preliminary print for %s: %s left unwritten "
            "until the final print",
            series.series_id,
            ", ".join(month.isoformat() for month in sorted(preliminary)),
        )
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
    selectors: Mapping[str, str],
) -> None:
    """Reject a row the request did not ask for.

    The selectors carry the series identity, so a row that answers a different
    cycle, adjustment, indicator, or region means the filter did not apply. Mixing
    those into one series is worse than failing the refresh. ``@isProvisional`` is
    not checked here: a preliminary row is the requested series, just an earlier
    print of one month, so it is skipped by the caller rather than failing the
    fetch that carries every final print alongside it.
    """

    expected = (
        ("@indicator", selectors["IndicatorCode"]),
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
    # Only the documented markers stand for "no number". A value that is not a
    # string at all is a changed response shape, and skipping those rows would
    # turn it into a short history instead of a failure.
    if not isinstance(raw, str):
        raise IndicatorsProviderError(f"e-Stat dashboard value is not a string: {raw!r}")
    text = raw.strip()
    if text in _DASHBOARD_NULL_MARKERS:
        return None
    return parse_float(text)
