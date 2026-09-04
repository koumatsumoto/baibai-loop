"""Show macro readings, contexts, and series through macro read models."""

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
    MACRO_CONTEXT_STALE_DAYS,
    macro_registered_series,
    macro_series_names,
)
from baibai_web.sources.db_sources import (
    DbMacroSource,
)
from baibai_web.sources.types import (
    MacroSeriesConfig,
)

from .models import (
    MacroConnectionSectionView,
    MacroContextExcerptView,
    MacroContextRevisionView,
    MacroContextView,
    MacroCoreSectionView,
    MacroDominantForceView,
    MacroEconomicConnectionView,
    MacroEstimateCaveatView,
    MacroFactSummaryView,
    MacroForceInteractionView,
    MacroGroupView,
    MacroMaterialDeltaView,
    MacroMonitoringPointView,
    MacroPointView,
    MacroReadingView,
    MacroResearchPriorityHintView,
    MacroRiskEnvironmentView,
    MacroScenarioView,
    MacroSectionJudgmentView,
    MacroSectorTiltView,
    MacroSeriesReferenceView,
    MacroSeriesView,
    MacroSizingCautionView,
    MacroSynthesisView,
    MacroView,
)

_MACRO_SPARKLINE_LOOKBACK = timedelta(days=400)


_MACRO_SPARKLINE_MAX_POINTS = 13


def build_macro(
    source: DbMacroSource,
    *,
    as_of: date,
) -> MacroView:
    """Build the daily entrance with bounded sparklines, never full indicator history."""

    latest_context_raw = source.latest_context(as_of=as_of)
    fetch_health = source.fetch_health()
    current_payload = source.reading(as_of=as_of)
    reading = (
        None
        if current_payload is None
        else MacroReadingView.model_validate({**current_payload, "fetch_health": fetch_health})
    )
    latest_context_id = (
        None if latest_context_raw is None else str(latest_context_raw["context_id"])
    )
    reports = [
        MacroContextRevisionView(
            context_id=str(item["context_id"]),
            as_of=(report_asof := date.fromisoformat(str(item["as_of"]))),
            published_at=datetime.fromisoformat(str(item["published_at"])),
            summary=str(item["summary"]),
            age_days=(age_days := (as_of - report_asof).days),
            stale=age_days > MACRO_CONTEXT_STALE_DAYS,
        )
        for item in source.contexts()
        if str(item["context_id"]) != latest_context_id
    ]
    return MacroView(
        latest_context=(
            None
            if latest_context_raw is None
            else _macro_context_excerpt(latest_context_raw, as_of=as_of)
        ),
        reading=reading,
        reports=reports,
        groups=_build_macro_group_metadata(source, as_of=as_of),
    )


def _macro_context_excerpt(
    raw_context: Mapping[str, object], *, as_of: date
) -> MacroContextExcerptView:
    context = _build_macro_context_view(
        raw_context,
        as_of=as_of,
        series_names=macro_series_names(),
    )
    risk_section = next(
        (section for section in context.core if section.section_id == "risk_environment"),
        None,
    )
    warnings = ["context_stale"] if context.stale else []
    raw_inputs = raw_context.get("inputs")
    if isinstance(raw_inputs, Mapping):
        for input_group in (
            "articles",
            "indicator_series",
            "reading_snapshots",
            "machine_snapshots",
        ):
            items = raw_inputs.get(input_group)
            if not isinstance(items, list):
                continue
            warnings.extend(
                f"failed_input:{item.get('input_id')}"
                for item in items
                if isinstance(item, Mapping) and item.get("status") == "failed"
            )
    return MacroContextExcerptView(
        context_id=context.context_id,
        as_of=context.as_of,
        published_at=context.published_at,
        age_days=context.age_days,
        stale=context.stale,
        summary=context.summary,
        synthesis=context.synthesis,
        risk_environment=None if risk_section is None else risk_section.risk_environment,
        scenarios=[] if risk_section is None else risk_section.scenarios,
        material_deltas=[delta for section in context.core for delta in section.material_deltas],
        research_priority_hints=context.connection.research_priority_hints,
        bargain_topography=context.connection.bargain_topography,
        estimate_caveats=context.connection.estimate_caveats,
        sizing_cautions=context.connection.sizing_cautions,
        warnings=warnings,
    )


def build_macro_context_detail(
    source: DbMacroSource,
    *,
    context_id: str,
    as_of: date,
) -> MacroContextView:
    """Build one published macro report (core 10 + connection) for the detail page."""
    raw_context = source.context_by_id(context_id=context_id, as_of=as_of)
    return _build_macro_context_view(
        raw_context,
        as_of=as_of,
        series_names=macro_series_names(),
    )


def _build_macro_context_view(
    raw_context: Mapping[str, object],
    *,
    as_of: date,
    series_names: Mapping[str, str],
) -> MacroContextView:
    context_as_of = date.fromisoformat(str(raw_context["as_of"]))
    age_days = (as_of - context_as_of).days
    connection = raw_context["connection"]
    if not isinstance(connection, Mapping):
        raise ValueError("macro context connection must be an object")
    return MacroContextView(
        context_id=str(raw_context["context_id"]),
        as_of=context_as_of,
        published_at=datetime.fromisoformat(str(raw_context["published_at"])),
        summary=str(raw_context["summary"]),
        age_days=age_days,
        stale=age_days > MACRO_CONTEXT_STALE_DAYS,
        synthesis=_macro_synthesis_view(raw_context.get("synthesis"), series_names=series_names),
        core=[
            _macro_core_section_view(item, series_names=series_names)
            for item in _mapping_items(raw_context["core"])
        ],
        connection=_macro_connection_section_view(connection, series_names=series_names),
    )


def _macro_synthesis_view(
    raw: object, *, series_names: Mapping[str, str]
) -> MacroSynthesisView | None:
    # Revisions published before the integrated layer carry no synthesis key.
    if not isinstance(raw, Mapping):
        return None
    return MacroSynthesisView(
        dominant_forces=[
            MacroDominantForceView(
                force_id=str(item["force_id"]),
                title=str(item["title"]),
                summary=str(item["summary"]),
                transmission=str(item["transmission"]),
                core_section_ids=_string_items(item.get("core_section_ids")),
                series=_macro_series_references(item, series_names=series_names),
                counter_evidence=str(item["counter_evidence"]),
                direction=str(item["direction"]),
                confidence=str(item["confidence"]),
                source_ids=_string_items(item.get("source_ids")),
            )
            for item in _mapping_items(raw.get("dominant_forces"))
        ],
        interactions=[
            MacroForceInteractionView.model_validate(item)
            for item in _mapping_items(raw.get("interactions"))
        ],
    )


def _build_macro_group_metadata(source: DbMacroSource, *, as_of: date) -> list[MacroGroupView]:
    """Return configured rows with a fixed, bounded monthly sparkline sample."""

    groups: list[MacroGroupView] = []
    for group in source.groups:
        groups.append(
            MacroGroupView(
                title=group.title,
                series=[
                    _build_macro_sparkline_series(source, configured=item, as_of=as_of)
                    for item in group.series
                ],
            )
        )
    return groups


def _build_macro_sparkline_series(
    source: DbMacroSource,
    *,
    configured: MacroSeriesConfig,
    as_of: date,
) -> MacroSeriesView:
    raw_series = source.series(
        configured.series_id,
        start=as_of - _MACRO_SPARKLINE_LOOKBACK,
        end=as_of,
        granularity="monthly",
    )
    if raw_series is None:
        return _unfetched_macro_series_view(configured)
    return _macro_series_view(
        configured,
        raw_series,
        points_limit=_MACRO_SPARKLINE_MAX_POINTS,
    )


def build_macro_series(
    source: DbMacroSource,
    *,
    series_id: str,
    as_of: date,
) -> MacroSeriesView | None:
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
        return _unfetched_macro_series_view(configured)
    return _macro_series_view(configured, raw_series)


def _macro_series_view(
    configured: MacroSeriesConfig,
    raw_series: Mapping[str, object],
    *,
    points_limit: int | None = None,
) -> MacroSeriesView:
    name = str(raw_series["name"])
    points = [MacroPointView.model_validate(item) for item in _mapping_items(raw_series["points"])]
    if points_limit is not None:
        points = points[-points_limit:]
    return MacroSeriesView(
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


def _unfetched_macro_series_view(configured: MacroSeriesConfig) -> MacroSeriesView:
    """A configured series the indicator store does not carry, shown as having no data.

    The registry defines a series before any store fetches it, so a store that has not
    caught up is a freshness state: the panel keeps the configured row and the card reads
    as having no observations. An id the registry does not define is configuration drift
    and fails the build rather than rendering as permanently empty.
    """

    registered = macro_registered_series(configured.series_id)
    if registered is None:
        raise ValueError(f"configured macro series is not registered: {configured.series_id}")
    name = str(registered["name"])
    return MacroSeriesView(
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


def _macro_core_section_view(
    raw: Mapping[str, object], *, series_names: Mapping[str, str]
) -> MacroCoreSectionView:
    return MacroCoreSectionView(
        section_id=str(raw["section_id"]),
        series=_macro_series_references(raw, series_names=series_names),
        fact_summary=[
            MacroFactSummaryView.model_validate(item)
            for item in _mapping_items(raw.get("fact_summary"))
        ],
        judgment=MacroSectionJudgmentView.model_validate(raw["judgment"]),
        economic_connection=MacroEconomicConnectionView.model_validate(raw["economic_connection"]),
        change_since_previous=_optional_text(raw.get("change_since_previous")),
        previous_scorecard_review=_optional_text(raw.get("previous_scorecard_review")),
        material_deltas=[
            MacroMaterialDeltaView.model_validate(item)
            for item in _mapping_items(raw.get("material_deltas"))
        ],
        risk_environment=(
            MacroRiskEnvironmentView.model_validate(raw["risk_environment"])
            if raw.get("risk_environment") is not None
            else None
        ),
        scenarios=[
            MacroScenarioView.model_validate(item) for item in _mapping_items(raw.get("scenarios"))
        ],
        monitoring_points=[
            MacroMonitoringPointView.model_validate(item)
            for item in _mapping_items(raw.get("monitoring_points"))
        ],
    )


def _macro_connection_section_view(
    raw: Mapping[str, object], *, series_names: Mapping[str, str]
) -> MacroConnectionSectionView:
    return MacroConnectionSectionView(
        section_id=str(raw["section_id"]),
        series=_macro_series_references(raw, series_names=series_names),
        core_section_ids=_string_items(raw.get("core_section_ids")),
        fact_summary=[
            MacroFactSummaryView.model_validate(item)
            for item in _mapping_items(raw.get("fact_summary"))
        ],
        judgment=MacroSectionJudgmentView.model_validate(raw["judgment"]),
        research_priority_hints=[
            MacroResearchPriorityHintView.model_validate(item)
            for item in _mapping_items(raw.get("research_priority_hints"))
        ],
        sector_tilts=[
            MacroSectorTiltView.model_validate(item)
            for item in _mapping_items(raw.get("sector_tilts"))
        ],
        sizing_cautions=[
            MacroSizingCautionView.model_validate(item)
            for item in _mapping_items(raw.get("sizing_cautions"))
        ],
        bargain_topography=(
            MacroFactSummaryView.model_validate(raw["bargain_topography"])
            if isinstance(raw.get("bargain_topography"), Mapping)
            else None
        ),
        estimate_caveats=[
            MacroEstimateCaveatView.model_validate(item)
            # Revisions published before the integrated layer carry no caveats key.
            for item in _mapping_items(raw.get("estimate_caveats") or [])
        ],
    )


def _macro_series_references(
    raw: Mapping[str, object], *, series_names: Mapping[str, str]
) -> list[MacroSeriesReferenceView]:
    return [
        MacroSeriesReferenceView(series_id=series_id, name=series_names.get(series_id, series_id))
        for series_id in _string_items(raw.get("series_ids"))
    ]


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _string_items(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("expected an array of strings")
    return value
