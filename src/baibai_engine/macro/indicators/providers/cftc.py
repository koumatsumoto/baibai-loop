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
    record_observation,
)

# CFTC Commitments of Traders, legacy futures-only report (Socrata JSON, no auth).
# provider_series_id is the stable cftc_contract_market_code; the market name
# changes over time (043602 was "10-YEAR U.S. TREASURY NOTES", now "UST 10Y
# NOTE") so the code, not the name, is the identity. The observation is the
# non-commercial (speculative) net position = long - short, in contracts.
_MAX_ROWS = 50_000
_PLAUSIBLE_ABS = 3_000_000


class CftcProvider:
    """CFTC COT non-commercial net position (contracts) for one contract code."""

    spec = ProviderSpec(name="cftc", all_history_start=date(2000, 1, 1))
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
        code = series.provider_series_id
        where = (
            f"cftc_contract_market_code='{code}' "
            f"AND report_date_as_yyyy_mm_dd between '{start.isoformat()}T00:00:00' "
            f"and '{end.isoformat()}T23:59:59'"
        )
        text = fetch_text(
            session,
            series.source_url,
            params={
                "$select": (
                    "report_date_as_yyyy_mm_dd,cftc_contract_market_code,"
                    "noncomm_positions_long_all,noncomm_positions_short_all"
                ),
                "$where": where,
                "$order": "report_date_as_yyyy_mm_dd",
                "$limit": str(_MAX_ROWS),
            },
            max_bytes=MAX_CSV_RESPONSE_BYTES,
            context=context,
        )
        return parse_cftc_json(series, text, start=start, end=end)


def parse_cftc_json(
    series: SeriesDefinition, text: str, *, start: date, end: date
) -> list[ObservationRecord]:
    try:
        payload: object = json.loads(text)
    except json.JSONDecodeError as exc:
        raise IndicatorsProviderError(f"CFTC response is not valid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise IndicatorsProviderError("CFTC response root is not a list")
    by_date: dict[date, float] = {}
    for entry in payload:
        if not isinstance(entry, dict):
            raise IndicatorsProviderError("CFTC row is not an object")
        row = cast(Mapping[str, object], entry)
        code = row.get("cftc_contract_market_code")
        if code != series.provider_series_id:
            raise IndicatorsProviderError(
                f"CFTC row code {code!r} does not match requested {series.provider_series_id!r}"
            )
        observed_at = _parse_date(row.get("report_date_as_yyyy_mm_dd"))
        long_positions = _parse_int(row.get("noncomm_positions_long_all"), "long")
        short_positions = _parse_int(row.get("noncomm_positions_short_all"), "short")
        net = float(long_positions - short_positions)
        if abs(net) > _PLAUSIBLE_ABS:
            raise IndicatorsProviderError(
                f"CFTC net position {net} for {observed_at} exceeds plausible {_PLAUSIBLE_ABS}"
            )
        if start <= observed_at <= end:
            if observed_at in by_date and by_date[observed_at] != net:
                raise IndicatorsProviderError(f"CFTC has conflicting values for {observed_at}")
            by_date[observed_at] = net
    return [
        record_observation(series, observed_at=observed_at, value=by_date[observed_at])
        for observed_at in sorted(by_date)
    ]


def _parse_date(raw: object) -> date:
    if not isinstance(raw, str) or len(raw) < 10:
        raise IndicatorsProviderError(f"CFTC report date is invalid: {raw!r}")
    try:
        return date.fromisoformat(raw[:10])
    except ValueError as exc:
        raise IndicatorsProviderError(f"CFTC report date is invalid: {raw!r}") from exc


def _parse_int(raw: object, label: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise IndicatorsProviderError(f"CFTC {label} position is not numeric: {raw!r}")
    try:
        return int(str(raw))
    except ValueError as exc:
        raise IndicatorsProviderError(f"CFTC {label} position is not an integer: {raw!r}") from exc
