from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import db
from .db import ObservationRecord
from .definitions import SeriesDefinition
from .providers import FetchContext, StatsProviderError, fetch_observations

DEFAULT_LATEST_LOOKBACK_DAYS = 370
DEFAULT_WEEKLY_LOOKBACK_DAYS = 28
LATEST_CACHE_MAX_AGE_DAYS = {
    "daily": 7,
    "weekly": 14,
    "monthly": 70,
}
WORLD_WEEKLY_SERIES = (
    "us.10y",
    "us.2y",
    "us.10y_2y_spread",
    "vix",
    "brent",
    "wti",
    "usd_jpy",
    "eur_jpy",
    "aud_jpy",
)


@dataclass(frozen=True)
class QueryResult:
    series: SeriesDefinition
    observations: tuple[ObservationRecord, ...]
    cache_hit: bool


class StatsService:
    def __init__(self, db_path: Path = db.DEFAULT_DB_PATH) -> None:
        self.db_path = db_path

    def list_series(self, *, domain: str | None = None) -> tuple[SeriesDefinition, ...]:
        conn = db.open_connection(self.db_path)
        try:
            return db.list_series(conn, domain=domain)
        finally:
            conn.close()

    def search(self, query: str) -> tuple[SeriesDefinition, ...]:
        conn = db.open_connection(self.db_path)
        try:
            return db.search_series(conn, query)
        finally:
            conn.close()

    def get_range(
        self,
        series_id: str,
        *,
        start: date,
        end: date,
        refresh: bool = False,
    ) -> QueryResult:
        if end < start:
            raise ValueError("--end must be on or after --start")
        conn = db.open_connection(self.db_path)
        try:
            series = db.get_series(conn, series_id)
            cache_hit = (not refresh) and db.has_ok_coverage(conn, series_id, start, end)
            if not cache_hit:
                observations = self._fetch_and_store(conn, series, start=start, end=end)
            else:
                observations = list(db.observations_in_range(conn, series_id, start, end))
            return QueryResult(series, tuple(observations), cache_hit=cache_hit)
        finally:
            conn.close()

    def get_latest(self, series_id: str, *, refresh: bool = False) -> QueryResult:
        end = datetime.now(UTC).date()
        conn = db.open_connection(self.db_path)
        try:
            series = db.get_series(conn, series_id)
            cached_latest = db.latest_observation(conn, series_id, on_or_before=end)
            if not refresh and cached_latest is not None:
                max_age = LATEST_CACHE_MAX_AGE_DAYS.get(series.frequency, 370)
                if cached_latest.observed_at >= end - timedelta(days=max_age):
                    return QueryResult(series, (cached_latest,), cache_hit=True)
        finally:
            conn.close()
        start = end - timedelta(days=DEFAULT_LATEST_LOOKBACK_DAYS)
        result = self.get_range(series_id, start=start, end=end, refresh=refresh)
        if not result.observations:
            return result
        return QueryResult(
            result.series,
            (max(result.observations, key=lambda item: item.observed_at),),
            cache_hit=result.cache_hit,
        )

    def macro_fragment(
        self,
        *,
        kind: str,
        start: date,
        end: date,
        refresh: bool = False,
    ) -> dict[str, Any]:
        if kind != "world-weekly":
            raise ValueError(f"unsupported stats macro fragment kind: {kind}")
        if end < start:
            raise ValueError("--end must be on or after --start")
        fetch_start = start - timedelta(days=DEFAULT_WEEKLY_LOOKBACK_DAYS)
        conn = db.open_connection(self.db_path)
        context = FetchContext()
        try:
            items: list[tuple[SeriesDefinition, ObservationRecord, ObservationRecord | None]] = []
            for series_id in WORLD_WEEKLY_SERIES:
                series = db.get_series(conn, series_id)
                if refresh or not db.has_ok_coverage(conn, series_id, fetch_start, end):
                    self._fetch_and_store(conn, series, start=fetch_start, end=end, context=context)
                current = db.latest_observation(
                    conn,
                    series_id,
                    on_or_after=start,
                    on_or_before=end,
                )
                if current is None:
                    raise StatsProviderError(
                        f"missing {series_id} observation between {start} and {end}"
                    )
                previous = db.previous_observation(conn, series_id, before=start)
                items.append((series, current, previous))
            return _render_world_weekly_fragment(start=start, end=end, items=items)
        finally:
            context.close()
            conn.close()

    def _fetch_and_store(
        self,
        conn: sqlite3.Connection,
        series: SeriesDefinition,
        *,
        start: date,
        end: date,
        context: FetchContext | None = None,
    ) -> list[ObservationRecord]:
        started_at = datetime.now(UTC)
        try:
            observations = fetch_observations(series, start=start, end=end, context=context)
            db.insert_observations(conn, observations)
            db.record_provider_run(
                conn,
                provider=series.provider,
                series_id=series.series_id,
                start=start,
                end=end,
                started_at=started_at,
                status="ok",
                record_count=len(observations),
            )
            conn.commit()
            return observations
        except Exception as exc:
            db.record_provider_run(
                conn,
                provider=series.provider,
                series_id=series.series_id,
                start=start,
                end=end,
                started_at=started_at,
                status="failed",
                record_count=0,
                error_message=str(exc),
            )
            conn.commit()
            raise


def _render_world_weekly_fragment(
    *,
    start: date,
    end: date,
    items: Iterable[tuple[SeriesDefinition, ObservationRecord, ObservationRecord | None]],
) -> dict[str, Any]:
    sources: dict[str, dict[str, str]] = {}
    world_market: list[dict[str, Any]] = []
    japan_fx: list[dict[str, Any]] = []
    breaches: list[dict[str, Any]] = []
    for series, current, previous in items:
        sources[series.source_id] = {
            "id": series.source_id,
            "name": _source_name(series),
            "url": series.source_url,
            "status": "ok",
        }
        rendered = {
            "name": series.name,
            "value": _format_value(series, current.value),
            "as_of": current.observed_at.isoformat(),
            "source_ids": [series.source_id],
        }
        if previous is not None:
            rendered["wow_comment"] = _change_comment(series, current, previous)
            breach = _threshold_breach(series, current, previous)
            if breach is not None:
                breaches.append(breach)
        if series.domain == "fx":
            japan_fx.append(rendered)
        else:
            world_market.append(rendered)
    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "sources": list(sources.values()),
        "layers": {
            "world": {"market_indicators": world_market},
            "japan": {"fx": japan_fx},
            "japan_equity": {},
        },
        "deltas": {"threshold_breaches": breaches},
    }


def _source_name(series: SeriesDefinition) -> str:
    match series.provider:
        case "frb_h15":
            return "Federal Reserve H.15 Selected Interest Rates"
        case "ecb_fx":
            return "ECB euro reference rates historical CSV"
        case "fred_csv":
            return f"FRED {series.provider_series_id}"
        case _:
            return series.provider


def _format_value(series: SeriesDefinition, value: float) -> str:
    match series.unit:
        case "percent":
            return f"{value:.2f}%"
        case "bp":
            return f"{value:+.0f}bp"
        case "usd-per-barrel":
            return f"${value:.2f}"
        case "jpy-per-usd" | "jpy-per-eur" | "jpy-per-aud":
            return f"{value:.2f}"
        case "index":
            return f"{value:.2f}"
        case _:
            return f"{value:.2f}"


def _change_comment(
    series: SeriesDefinition,
    current: ObservationRecord,
    previous: ObservationRecord,
) -> str:
    delta = current.value - previous.value
    if series.unit == "percent" and series.domain == "rates":
        return f"previous {_format_value(series, previous.value)} から {delta * 100:+.0f}bp"
    if series.unit == "bp":
        return f"previous {_format_value(series, previous.value)} から {delta:+.0f}bp"
    if previous.value:
        pct = delta / previous.value * 100
        return f"previous {_format_value(series, previous.value)} から {pct:+.1f}%"
    return f"previous {_format_value(series, previous.value)} から {delta:+.2f}"


def _threshold_breach(
    series: SeriesDefinition,
    current: ObservationRecord,
    previous: ObservationRecord,
) -> dict[str, Any] | None:
    delta = current.value - previous.value
    severity: str | None = None
    if series.unit == "percent" and series.domain == "rates":
        change = f"{delta * 100:+.0f}bp"
        severity = "major" if abs(delta * 100) >= 15 else None
    elif series.unit == "bp":
        change = f"{delta:+.0f}bp"
        severity = "major" if abs(delta) >= 15 else None
    elif previous.value:
        pct = delta / previous.value * 100
        change = f"{pct:+.1f}%"
        if abs(pct) >= 5:
            severity = "major"
        elif abs(pct) >= 3:
            severity = "notable"
    else:
        return None
    if severity is None:
        return None
    return {
        "severity": severity,
        "indicator": series.name,
        "from": _format_value(series, previous.value),
        "to": _format_value(series, current.value),
        "change": change,
    }
