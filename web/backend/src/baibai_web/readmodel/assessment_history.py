"""過去に保存された判断本文・固定章・主張を、その意味のまま表示する。"""

from __future__ import annotations

from collections.abc import (
    Mapping,
)
from datetime import (
    date,
    datetime,
)

from baibai_engine.read_api import (
    ASSESSMENT_STALE_DAYS,
)

from .models import (
    AssessmentPublicationView,
    HistoricalConnectionSectionView,
    HistoricalCoreSectionView,
    HistoricalDominantForceView,
    HistoricalEconomicConnectionView,
    HistoricalEstimateCaveatView,
    HistoricalFactSummaryView,
    HistoricalForceInteractionView,
    HistoricalMaterialDeltaView,
    HistoricalMonitoringPointView,
    HistoricalResearchPriorityHintView,
    HistoricalRiskEnvironmentView,
    HistoricalScenarioView,
    HistoricalSectionJudgmentView,
    HistoricalSectorTiltView,
    HistoricalSeriesReferenceView,
    HistoricalSizingCautionView,
    HistoricalSynthesisView,
)


def build_historical_assessment(
    raw_context: Mapping[str, object], *, as_of: date, series_names: Mapping[str, str]
) -> AssessmentPublicationView:
    """Render the saved report body without applying a current writer contract."""
    context_as_of = date.fromisoformat(str(raw_context["as_of"]))
    age_days = (as_of - context_as_of).days
    connection = raw_context.get("connection")
    return AssessmentPublicationView(
        context_id=str(raw_context["context_id"]),
        as_of=context_as_of,
        published_at=datetime.fromisoformat(str(raw_context["published_at"])),
        summary=str(raw_context["summary"]),
        age_days=age_days,
        stale=age_days > ASSESSMENT_STALE_DAYS,
        synthesis=_historical_synthesis_view(
            raw_context.get("synthesis"), series_names=series_names
        ),
        core=[
            _historical_core_section_view(item, series_names=series_names)
            for item in _mapping_items(raw_context.get("core", []))
        ],
        connection=_historical_connection_section_view(connection, series_names=series_names)
        if isinstance(connection, Mapping)
        else None,
        historical_payload=dict(raw_context),
    )


def _string_items(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("expected an array of strings")
    return value


def _historical_connection_section_view(
    raw: Mapping[str, object], *, series_names: Mapping[str, str]
) -> HistoricalConnectionSectionView:
    return HistoricalConnectionSectionView(
        section_id=str(raw["section_id"]),
        series=_historical_series_references(raw, series_names=series_names),
        core_section_ids=_string_items(raw.get("core_section_ids")),
        fact_summary=[
            HistoricalFactSummaryView.model_validate(item)
            for item in _mapping_items(raw.get("fact_summary"))
        ],
        judgment=HistoricalSectionJudgmentView.model_validate(raw["judgment"]),
        research_priority_hints=[
            HistoricalResearchPriorityHintView.model_validate(item)
            for item in _mapping_items(raw.get("research_priority_hints"))
        ],
        sector_tilts=[
            HistoricalSectorTiltView.model_validate(item)
            for item in _mapping_items(raw.get("sector_tilts"))
        ],
        sizing_cautions=[
            HistoricalSizingCautionView.model_validate(item)
            for item in _mapping_items(raw.get("sizing_cautions"))
        ],
        bargain_topography=(
            HistoricalFactSummaryView.model_validate(raw["bargain_topography"])
            if isinstance(raw.get("bargain_topography"), Mapping)
            else None
        ),
        estimate_caveats=[
            HistoricalEstimateCaveatView.model_validate(item)
            # Revisions published before the integrated layer carry no caveats key.
            for item in _mapping_items(raw.get("estimate_caveats") or [])
        ],
    )


def _historical_core_section_view(
    raw: Mapping[str, object], *, series_names: Mapping[str, str]
) -> HistoricalCoreSectionView:
    return HistoricalCoreSectionView(
        section_id=str(raw["section_id"]),
        series=_historical_series_references(raw, series_names=series_names),
        fact_summary=[
            HistoricalFactSummaryView.model_validate(item)
            for item in _mapping_items(raw.get("fact_summary"))
        ],
        judgment=HistoricalSectionJudgmentView.model_validate(raw["judgment"]),
        economic_connection=HistoricalEconomicConnectionView.model_validate(
            raw["economic_connection"]
        ),
        change_since_previous=_optional_text(raw.get("change_since_previous")),
        previous_scorecard_review=_optional_text(raw.get("previous_scorecard_review")),
        material_deltas=[
            HistoricalMaterialDeltaView.model_validate(item)
            for item in _mapping_items(raw.get("material_deltas"))
        ],
        risk_environment=(
            HistoricalRiskEnvironmentView.model_validate(raw["risk_environment"])
            if raw.get("risk_environment") is not None
            else None
        ),
        scenarios=[
            HistoricalScenarioView.model_validate(item)
            for item in _mapping_items(raw.get("scenarios"))
        ],
        monitoring_points=[
            HistoricalMonitoringPointView.model_validate(item)
            for item in _mapping_items(raw.get("monitoring_points"))
        ],
    )


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _historical_synthesis_view(
    raw: object, *, series_names: Mapping[str, str]
) -> HistoricalSynthesisView | None:
    # Revisions published before the integrated layer carry no synthesis key.
    if not isinstance(raw, Mapping):
        return None
    return HistoricalSynthesisView(
        dominant_forces=[
            HistoricalDominantForceView(
                force_id=str(item["force_id"]),
                title=str(item["title"]),
                summary=str(item["summary"]),
                transmission=str(item["transmission"]),
                core_section_ids=_string_items(item.get("core_section_ids")),
                series=_historical_series_references(item, series_names=series_names),
                counter_evidence=str(item["counter_evidence"]),
                direction=str(item["direction"]),
                confidence=str(item["confidence"]),
                source_ids=_string_items(item.get("source_ids")),
            )
            for item in _mapping_items(raw.get("dominant_forces"))
        ],
        interactions=[
            HistoricalForceInteractionView.model_validate(item)
            for item in _mapping_items(raw.get("interactions"))
        ],
    )


def _historical_series_references(
    raw: Mapping[str, object], *, series_names: Mapping[str, str]
) -> list[HistoricalSeriesReferenceView]:
    return [
        HistoricalSeriesReferenceView(
            series_id=series_id, name=series_names.get(series_id, series_id)
        )
        for series_id in _string_items(raw.get("series_ids"))
    ]


def _mapping_items(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise ValueError("expected an array of objects")
    return [item for item in value if isinstance(item, Mapping)]
