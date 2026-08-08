from __future__ import annotations

import json
import re
from datetime import date, datetime
from itertools import pairwise
from zoneinfo import ZoneInfo

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

_HISTORY_FLOOR = date(2003, 1, 1)
# Official releases may arrive as late as day 27. The tail gate waits until day
# 28 so a normal late publication is not treated as stale.
_PUBLICATION_DEADLINE_DAY = 28
_MONTH_RE = re.compile(r"(?P<year>20\d{2})年(?:（[^）]+）)?\s*(?P<month>\d{1,2})月")
_TITLE_VALUE_RE = re.compile(r"全国企業倒産(?:状況)?\s*(?P<value>[\d,]+)\s*件")
_TABLE_VALUE_RE = re.compile(
    r"<th[^>]*>\s*倒産件数\s*</th>\s*"
    r"<td[^>]*>\s*(?P<value>[\d,]+)\s*件",
    re.DOTALL,
)


class TsrBankruptciesProvider:
    """東京商工リサーチの公式 JSON から月次の全国企業倒産件数を読む。"""

    spec = ProviderSpec(name="tsr_bankruptcies", all_history_start=date(2003, 1, 1))
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
        text = fetch_text(
            session,
            series.source_url,
            params={"data": "data"},
            max_bytes=MAX_CSV_RESPONSE_BYTES,
            context=context,
        )
        return parse_tsr_bankruptcies_json(series, text, start=start, end=end)


def parse_tsr_bankruptcies_json(
    series: SeriesDefinition,
    text: str,
    *,
    start: date,
    end: date,
) -> list[ObservationRecord]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise IndicatorsProviderError("tsr_bankruptcies: invalid JSON") from exc
    if not isinstance(payload, list):
        raise IndicatorsProviderError("tsr_bankruptcies: response root is not a list")

    values: dict[date, float] = {}
    for entry in payload:
        if not isinstance(entry, dict) or entry.get("period_division") != "月次":
            continue
        title = entry.get("title")
        if not isinstance(title, str):
            raise IndicatorsProviderError("tsr_bankruptcies: monthly entry has no title")
        observed_match = _MONTH_RE.search(title)
        value_match = _TITLE_VALUE_RE.search(title)
        if value_match is None:
            free_word = entry.get("free_word")
            if isinstance(free_word, list):
                value_match = _TABLE_VALUE_RE.search(" ".join(str(item) for item in free_word))
        if observed_match is None or value_match is None:
            raise IndicatorsProviderError(
                f"tsr_bankruptcies: cannot parse monthly entry: {title!r}"
            )
        observed_at = date(
            int(observed_match.group("year")),
            int(observed_match.group("month")),
            1,
        )
        value = float(value_match.group("value").replace(",", ""))
        prior_value = values.get(observed_at)
        if prior_value is not None and prior_value != value:
            raise IndicatorsProviderError(f"tsr_bankruptcies: conflicting values for {observed_at}")
        values[observed_at] = value

    ordered_dates = sorted(values)
    for previous_date, current_date in pairwise(ordered_dates):
        expected = (
            date(previous_date.year + 1, 1, 1)
            if previous_date.month == 12
            else date(previous_date.year, previous_date.month + 1, 1)
        )
        if current_date != expected:
            raise IndicatorsProviderError(
                "tsr_bankruptcies: missing monthly entries between "
                f"{previous_date} and {current_date}"
            )
    if start <= _HISTORY_FLOOR and (not ordered_dates or ordered_dates[0] != _HISTORY_FLOOR):
        raise IndicatorsProviderError(f"tsr_bankruptcies: history must start at {_HISTORY_FLOOR}")
    reference_date = min(end, _today_jst())
    requested_month = reference_date.replace(day=1)
    expected_latest = _previous_month(requested_month)
    if reference_date.day < _PUBLICATION_DEADLINE_DAY:
        expected_latest = _previous_month(expected_latest)
    if not ordered_dates or ordered_dates[-1] < expected_latest:
        raise IndicatorsProviderError(
            f"tsr_bankruptcies: history ends before the latest expected release {expected_latest}"
        )

    return [
        record_observation(series, observed_at=observed_at, value=value)
        for observed_at, value in sorted(values.items())
        if start <= observed_at <= end
    ]


def _today_jst() -> date:
    return datetime.now(ZoneInfo("Asia/Tokyo")).date()


def _previous_month(value: date) -> date:
    return date(value.year - 1, 12, 1) if value.month == 1 else date(value.year, value.month - 1, 1)
