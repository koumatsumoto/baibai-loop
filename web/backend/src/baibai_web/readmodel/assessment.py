"""現行assessmentと保存履歴の共通metadata・詳細・抜粋を作る。"""

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
    indicator_series_names,
)
from baibai_web.sources.db_sources import (
    DbMarketContextSource,
)

from .assessment_history import build_historical_assessment
from .models import (
    AssessmentContentView,
    AssessmentExcerptView,
    AssessmentPublicationView,
)


def build_assessment_excerpt(
    raw_context: Mapping[str, object], *, as_of: date
) -> AssessmentExcerptView:
    context = build_assessment_publication(
        raw_context,
        as_of=as_of,
        series_names=indicator_series_names(),
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
    return AssessmentExcerptView(
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
        research_priority_hints=[]
        if context.connection is None
        else context.connection.research_priority_hints,
        bargain_topography=None
        if context.connection is None
        else context.connection.bargain_topography,
        estimate_caveats=[] if context.connection is None else context.connection.estimate_caveats,
        sizing_cautions=[] if context.connection is None else context.connection.sizing_cautions,
        warnings=warnings,
        assessment=context.assessment,
    )


def build_market_context_assessment_detail(
    source: DbMarketContextSource,
    *,
    context_id: str,
    as_of: date,
) -> AssessmentPublicationView:
    """Build one market assessment or archived report by its exact publication identity."""
    raw_context = source.assessment_by_id(context_id=context_id, as_of=as_of)
    return build_assessment_publication(
        raw_context,
        as_of=as_of,
        series_names=indicator_series_names(),
    )


def build_assessment_publication(
    raw_context: Mapping[str, object],
    *,
    as_of: date,
    series_names: Mapping[str, str],
) -> AssessmentPublicationView:
    context_as_of = date.fromisoformat(str(raw_context["as_of"]))
    age_days = (as_of - context_as_of).days
    if raw_context.get("schema_version") in (5, 6):
        return AssessmentPublicationView(
            context_id=str(raw_context["context_id"]),
            as_of=context_as_of,
            published_at=datetime.fromisoformat(str(raw_context["published_at"])),
            summary=str(raw_context["summary"]),
            age_days=age_days,
            stale=age_days > ASSESSMENT_STALE_DAYS,
            assessment=AssessmentContentView.model_validate(
                {
                    key: raw_context[key]
                    for key in (
                        "known_at",
                        "changes",
                        "counter_evidence",
                        "uncertainties",
                        "implications",
                        "inputs",
                    )
                }
            ),
        )
    return build_historical_assessment(raw_context, as_of=as_of, series_names=series_names)
