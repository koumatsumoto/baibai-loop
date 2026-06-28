from __future__ import annotations

import re
from datetime import UTC, date, datetime

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

# multpl.com rate-limits the default requests User-Agent; a browser UA is required.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# Each page exposes one sentence: "Current <name> is <value>[%], a change of ...".
_CURRENT_RE = re.compile(r"Current\s+[^<>]*?\s+is\s+([\d,]+(?:\.\d+)?)")


class MultplProvider:
    """multpl.com valuation scrape (no auth). One current level per page, keyed by
    the page slug in ``provider_series_id`` (e.g. ``shiller-pe``).

    Used for S&P 500 valuation (Shiller CAPE / GAAP PE / earnings yield) which has
    no clean FRED/official feed but anchors the equity-risk-premium lens. Returns a
    single current-level observation dated today; intended for ``--latest`` use
    (a level indicator), not historical range backfill.
    """

    name = "multpl"

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
            params=None,
            max_bytes=MAX_CSV_RESPONSE_BYTES,
            headers={"User-Agent": _USER_AGENT},
            context=context,
        )
        value = parse_multpl_current(text, series.provider_series_id)
        observed_at = datetime.now(UTC).date()
        if start <= observed_at <= end:
            return [record_observation(series, observed_at=observed_at, value=value)]
        return []


def parse_multpl_current(text: str, slug: str) -> float:
    match = _CURRENT_RE.search(text)
    if match is None:
        raise IndicatorsProviderError(f"multpl: cannot parse current value for {slug}")
    return float(match.group(1).replace(",", ""))
