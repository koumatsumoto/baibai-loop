"""再訪時の材料。未review研究と過去のreview済み評価を別枠で表す。"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from baibai_engine.investment_judgment.company_research.contracts import (
    HypothesisCheck,
    ObservedFact,
    ResearchFindings,
    Source,
)
from baibai_engine.investment_judgment.company_research.document import ResearchRecord
from baibai_engine.investment_judgment.contracts import NonEmptyStr
from baibai_engine.investment_judgment.stock_assessment.document import AssessmentRef


class _Context(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class NotFound(_Context):
    status: Literal["not_found"] = "not_found"


class Unreadable(_Context):
    status: Literal["unreadable"] = "unreadable"
    record_id: str | None
    reason: str = Field(min_length=1)


class AvailableResearch(_Context):
    status: Literal["available"] = "available"
    role: Literal["unreviewed_research"] = "unreviewed_research"
    record: ResearchRecord


class AvailableAssessment(_Context):
    status: Literal["available"] = "available"
    role: Literal["historical_reviewed_assessment_not_current_readiness"] = (
        "historical_reviewed_assessment_not_current_readiness"
    )
    reference: AssessmentRef
    ticker: str
    as_of: date
    published_at: AwareDatetime
    stance: Literal["supported", "uncertain", "unsupported"]
    sources: tuple[Source, ...]
    facts: tuple[ObservedFact, ...]
    findings: ResearchFindings
    # 旧固定 Selection は未記録のまま保持し、現在の評価から補完しない。
    strongest_countercase: NonEmptyStr | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    invalidation_conditions: tuple[NonEmptyStr, ...] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class CompanyContext(_Context):
    research: Annotated[AvailableResearch | NotFound | Unreadable, Field(discriminator="status")]
    assessment: Annotated[
        AvailableAssessment | NotFound | Unreadable, Field(discriminator="status")
    ]


def company_context_brief(context: CompanyContext) -> dict[str, object]:
    """有限の発見表示。直近資料日を全根拠の再確認済みとせず、全文はexact refで読む。"""
    result: dict[str, object] = {}
    research = context.research
    if isinstance(research, AvailableResearch):
        record = research.record
        document = record.document
        research_brief: dict[str, object] = {
            "status": research.status,
            "role": research.role,
            "reference": record.reference.model_dump(mode="json"),
            "as_of": document.as_of.isoformat(),
            "recorded_at": record.recorded_at.isoformat(),
            "investigated_scope": document.investigated_scope[:400],
            **_questions_brief(document.unresolved_questions),
            **_hypotheses_brief(document.hypotheses, document.sources),
            # Retained old sources must not hide newly added evidence on revisit.
            "source_dates": sorted({item.as_of.isoformat() for item in document.sources})[-3:],
            "display": "brief_only; use exact reference for all evidence and counterarguments",
        }
        if document.value_capture_hypothesis is not None:
            note = document.value_capture_hypothesis
            research_brief["value_capture_hypothesis"] = {
                "text": note.text[:400],
                "text_truncated": len(note.text) > 400,
                **_source_brief(note.source_ids, document.sources),
            }
        result["research"] = research_brief
    else:
        result["research"] = _absence_brief(research)
    assessment = context.assessment
    if isinstance(assessment, AvailableAssessment):
        result["assessment"] = {
            "status": assessment.status,
            "role": assessment.role,
            "reference": assessment.reference.model_dump(mode="json"),
            "as_of": assessment.as_of.isoformat(),
            "published_at": assessment.published_at.isoformat(),
            "stance": assessment.stance,
            **(
                {
                    "strongest_countercase": assessment.strongest_countercase[:400],
                    "strongest_countercase_truncated": len(assessment.strongest_countercase) > 400,
                }
                if assessment.strongest_countercase is not None
                else {}
            ),
            **(
                {
                    "invalidation_conditions": [
                        {"text": text[:400], "text_truncated": len(text) > 400}
                        for text in assessment.invalidation_conditions[:3]
                    ],
                    "invalidation_conditions_omitted": max(
                        0, len(assessment.invalidation_conditions) - 3
                    ),
                }
                if assessment.invalidation_conditions is not None
                else {}
            ),
            "business_understanding": assessment.findings.business_understanding[:400],
            "value_gap_explanation": assessment.findings.value_gap_explanation[:400],
            "value_gap_explanation_truncated": len(assessment.findings.value_gap_explanation) > 400,
            "realization": {
                "mechanism": assessment.findings.realization.mechanism[:400],
                "timing": assessment.findings.realization.timing,
                "timing_reason": assessment.findings.realization.timing_reason[:240],
                "delay_risk": assessment.findings.realization.delay_risk[:240],
            },
            **_hypotheses_brief(assessment.findings.hypotheses, assessment.sources),
            **_questions_brief(assessment.findings.unresolved_questions),
            "source_dates": sorted({item.as_of.isoformat() for item in assessment.sources})[-3:],
            "display": "brief_only; use exact reference for all evidence and counterarguments",
        }
    else:
        result["assessment"] = _absence_brief(assessment)
    return result


def _questions_brief(questions: tuple[str, ...]) -> dict[str, object]:
    return {
        "unresolved_questions": [text[:240] for text in questions[:3]],
        **({"unresolved_questions_omitted": len(questions) - 3} if len(questions) > 3 else {}),
    }


def _hypotheses_brief(
    hypotheses: tuple[HypothesisCheck, ...], sources: tuple[Source, ...]
) -> dict[str, object]:
    result: dict[str, object] = {
        "hypotheses": [
            {
                "statement": item.statement[:240],
                "result": item.result,
                "reason": item.reason[:240],
                **_source_brief(item.source_ids, sources),
            }
            for item in hypotheses[:3]
        ]
    }
    omitted = hypotheses[3:]
    if omitted:
        # 先頭の支持だけを全体の結論にせず、反証の存在から同じexactへ戻せるようにする。
        result.update(
            hypotheses_omitted=len(omitted),
            omitted_hypothesis_results=sorted({item.result for item in omitted}),
        )
    return result


def _source_brief(source_ids: tuple[str, ...], sources: tuple[Source, ...]) -> dict[str, object]:
    # A shared citation is not an independent vote; its own clocks matter.
    by_id = {source.source_id: source for source in sources}
    return {
        "sources": [
            {
                "source_id": source_id,
                "as_of": by_id[source_id].as_of.isoformat(),
                "retrieved_at": by_id[source_id].retrieved_at.isoformat(),
            }
            for source_id in source_ids[:3]
        ],
        "sources_omitted": max(0, len(source_ids) - 3),
    }


def _absence_brief(value: NotFound | Unreadable) -> dict[str, object]:
    if isinstance(value, Unreadable):
        return {
            "status": value.status,
            "record_id": value.record_id,
            "reason": value.reason[:400],
            "display": "reason_excerpt; exact read reports the full error",
        }
    return value.model_dump(mode="json")
