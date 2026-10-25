"""Deterministic monthly bankruptcy counts from retained publisher JSON."""

from __future__ import annotations

import json
import re
from datetime import date, datetime

from baibai_engine.machine_analysis.market_context.indicators.definitions import SeriesDefinition
from baibai_engine.machine_analysis.market_context.indicators.observations import ObservationRecord

from .observations import reading_observation

_MONTH_RE = re.compile(r"(?P<year>20\d{2})年(?:（[^）]+）)?\s*(?P<month>\d{1,2})月")
_TITLE_VALUE_RE = re.compile(r"全国企業倒産(?:状況)?\s*(?P<value>[\d,]+)\s*件")
_TABLE_VALUE_RE = re.compile(
    r"<th[^>]*>\s*倒産件数\s*</th>\s*"
    r"<td[^>]*>\s*(?P<value>[\d,]+)\s*件",
    re.DOTALL,
)


def parse_tsr_bankruptcies_json(
    series: SeriesDefinition,
    text: str,
    *,
    start: date,
    end: date,
    vintage_at: datetime | None = None,
) -> list[ObservationRecord]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("tsr_bankruptcies: invalid JSON") from exc
    if not isinstance(payload, list):
        raise ValueError("tsr_bankruptcies: response root is not a list")

    values: dict[date, float] = {}
    for entry in payload:
        if not isinstance(entry, dict) or entry.get("period_division") != "月次":
            continue
        title = entry.get("title")
        if not isinstance(title, str):
            raise ValueError("tsr_bankruptcies: monthly entry has no title")
        observed_match = _MONTH_RE.search(title)
        value_match = _TITLE_VALUE_RE.search(title)
        if value_match is None:
            free_word = entry.get("free_word")
            if isinstance(free_word, list):
                value_match = _TABLE_VALUE_RE.search(" ".join(str(item) for item in free_word))
        if observed_match is None or value_match is None:
            raise ValueError(f"tsr_bankruptcies: cannot parse monthly entry: {title!r}")
        observed_at = date(
            int(observed_match.group("year")),
            int(observed_match.group("month")),
            1,
        )
        value = float(value_match.group("value").replace(",", ""))
        prior_value = values.get(observed_at)
        if prior_value is not None and prior_value != value:
            raise ValueError(f"tsr_bankruptcies: conflicting values for {observed_at}")
        values[observed_at] = value

    # Keep every usable published month. Missing/old history is represented by
    # the returned dates; L2 Reading owns history sufficiency and freshness.
    # Do not reject a new month because an unrelated older month is absent.
    return [
        reading_observation(series, observed_at=observed_at, value=value, vintage_at=vintage_at)
        for observed_at, value in sorted(values.items())
        if start <= observed_at <= end
    ]
