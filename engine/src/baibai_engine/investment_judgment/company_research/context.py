"""再訪時の材料。未review研究と過去のreview済み評価を別枠で表す。"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from baibai_engine.investment_judgment.company_research.contracts import (
    ObservedFact,
    ResearchFindings,
    Source,
)
from baibai_engine.investment_judgment.company_research.document import ResearchRecord
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


class CompanyContext(_Context):
    research: Annotated[AvailableResearch | NotFound | Unreadable, Field(discriminator="status")]
    assessment: Annotated[
        AvailableAssessment | NotFound | Unreadable, Field(discriminator="status")
    ]


def company_context_brief(context: CompanyContext) -> dict[str, object]:
    """有限の発見表示。全文はexact refで読む。省略を根拠・反証なしと扱わない。"""
    result: dict[str, object] = {}
    research = context.research
    if isinstance(research, AvailableResearch):
        record = research.record
        document = record.document
        result["research"] = {
            "status": research.status,
            "role": research.role,
            "reference": record.reference.model_dump(mode="json"),
            "as_of": document.as_of.isoformat(),
            "recorded_at": record.recorded_at.isoformat(),
            "investigated_scope": document.investigated_scope[:400],
            "unresolved_questions": [text[:240] for text in document.unresolved_questions[:3]],
            "hypotheses": [
                {
                    "statement": item.statement[:240],
                    "result": item.result,
                    "reason": item.reason[:240],
                }
                for item in document.hypotheses[:3]
            ],
            "source_dates": sorted({item.as_of.isoformat() for item in document.sources})[:3],
            "display": "brief_only; use exact reference for all evidence and counterarguments",
        }
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
            "business_understanding": assessment.findings.business_understanding[:400],
            "hypotheses": [
                {
                    "statement": item.statement[:240],
                    "result": item.result,
                    "reason": item.reason[:240],
                }
                for item in assessment.findings.hypotheses[:3]
            ],
            "unresolved_questions": [
                text[:240] for text in assessment.findings.unresolved_questions[:3]
            ],
            "source_dates": sorted({item.as_of.isoformat() for item in assessment.sources})[:3],
            "display": "brief_only; use exact reference for all evidence and counterarguments",
        }
    else:
        result["assessment"] = _absence_brief(assessment)
    return result


def _absence_brief(value: NotFound | Unreadable) -> dict[str, object]:
    if isinstance(value, Unreadable):
        return {
            "status": value.status,
            "record_id": value.record_id,
            "reason": value.reason[:400],
            "display": "reason_excerpt; exact read reports the full error",
        }
    return value.model_dump(mode="json")
