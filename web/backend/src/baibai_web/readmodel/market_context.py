"""経済指標・Readingと保存済みassessmentを同じ市場環境画面へ組み立てる。"""

from __future__ import annotations

from collections.abc import (
    Mapping,
)
from datetime import (
    date,
    datetime,
    timedelta,
)

from baibai_engine.read_api import (
    ASSESSMENT_STALE_DAYS,
    registered_indicator,
)
from baibai_web.sources.db_sources import (
    DbMarketContextSource,
)
from baibai_web.sources.types import (
    IndicatorSeriesConfig,
)

from .assessment import build_assessment_excerpt
from .models import (
    AssessmentRevisionView,
    IndicatorGroupView,
    IndicatorPointView,
    IndicatorSeriesView,
    MarketContextView,
    ReadingView,
)

_INDICATOR_SPARKLINE_LOOKBACK = timedelta(days=400)


_INDICATOR_SPARKLINE_MAX_POINTS = 13


def build_market_context(
    source: DbMarketContextSource,
    *,
    as_of: date,
    known_at: datetime,
) -> MarketContextView:
    """Build the daily entrance with bounded sparklines, never full indicator history."""

    latest_context_raw = source.latest_assessment(as_of=as_of, known_at=known_at)
    fetch_health = source.fetch_health()
    current_payload = source.reading(as_of=as_of)
    reading = (
        None
        if current_payload is None
        else ReadingView.model_validate({**current_payload, "fetch_health": fetch_health})
    )
    latest_context_id = (
        None if latest_context_raw is None else str(latest_context_raw["context_id"])
    )
    reports = [
        AssessmentRevisionView(
            context_id=str(item["context_id"]),
            as_of=(report_asof := date.fromisoformat(str(item["as_of"]))),
            published_at=datetime.fromisoformat(str(item["published_at"])),
            summary=str(item["summary"]),
            age_days=(age_days := (as_of - report_asof).days),
            stale=age_days > ASSESSMENT_STALE_DAYS,
        )
        for item in source.assessment_history()
        if str(item["context_id"]) != latest_context_id
    ]
    return MarketContextView(
        latest_context=(
            None
            if latest_context_raw is None
            else build_assessment_excerpt(latest_context_raw, as_of=as_of)
        ),
        reading=reading,
        reports=reports,
        groups=_build_indicator_groups(source, as_of=as_of),
    )


def _build_indicator_groups(
    source: DbMarketContextSource, *, as_of: date
) -> list[IndicatorGroupView]:
    """Return configured rows with a fixed, bounded monthly sparkline sample."""

    groups: list[IndicatorGroupView] = []
    for group in source.groups:
        groups.append(
            IndicatorGroupView(
                title=group.title,
                series=[
                    _build_indicator_sparkline(source, configured=item, as_of=as_of)
                    for item in group.series
                ],
            )
        )
    return groups


def _build_indicator_sparkline(
    source: DbMarketContextSource,
    *,
    configured: IndicatorSeriesConfig,
    as_of: date,
) -> IndicatorSeriesView:
    raw_series = source.series(
        configured.series_id,
        start=as_of - _INDICATOR_SPARKLINE_LOOKBACK,
        end=as_of,
        granularity="monthly",
    )
    if raw_series is None:
        return _unfetched_indicator_view(configured)
    return _indicator_series_view(
        configured,
        raw_series,
        points_limit=_INDICATOR_SPARKLINE_MAX_POINTS,
    )


def build_indicator_series(
    source: DbMarketContextSource,
    *,
    series_id: str,
    as_of: date,
) -> IndicatorSeriesView | None:
    """Materialize the full daily history of exactly one configured series."""

    configured = next(
        (item for group in source.groups for item in group.series if item.series_id == series_id),
        None,
    )
    if configured is None:
        return None
    raw_series = source.series(
        series_id,
        start=None,
        end=as_of,
        granularity="daily",
    )
    if raw_series is None:
        return _unfetched_indicator_view(configured)
    return _indicator_series_view(configured, raw_series)


def _indicator_series_view(
    configured: IndicatorSeriesConfig,
    raw_series: Mapping[str, object],
    *,
    points_limit: int | None = None,
) -> IndicatorSeriesView:
    name = str(raw_series["name"])
    points = [
        IndicatorPointView.model_validate(item) for item in _mapping_items(raw_series["points"])
    ]
    if points_limit is not None:
        points = points[-points_limit:]
    return IndicatorSeriesView(
        series_id=configured.series_id,
        label=configured.label or name,
        name=name,
        unit=str(raw_series["unit"]),
        tradingview_symbol=(
            str(raw_series["tradingview_symbol"])
            if raw_series.get("tradingview_symbol") is not None
            else None
        ),
        points=points,
    )


def _unfetched_indicator_view(configured: IndicatorSeriesConfig) -> IndicatorSeriesView:
    """A configured series the indicator store does not carry, shown as having no data.

    The registry defines a series before any store fetches it, so a store that has not
    caught up is a freshness state: the panel keeps the configured row and the card reads
    as having no observations. An id the registry does not define is configuration drift
    and fails the build rather than rendering as permanently empty.
    """

    registered = registered_indicator(configured.series_id)
    if registered is None:
        raise ValueError(f"configured macro series is not registered: {configured.series_id}")
    name = str(registered["name"])
    return IndicatorSeriesView(
        series_id=configured.series_id,
        label=configured.label or name,
        name=name,
        unit=str(registered["unit"]),
        tradingview_symbol=registered["tradingview_symbol"],
        points=[],
    )


def _mapping_items(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise ValueError("expected an array of objects")
    return [item for item in value if isinstance(item, Mapping)]
