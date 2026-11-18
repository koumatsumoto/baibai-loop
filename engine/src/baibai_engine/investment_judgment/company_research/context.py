"""再訪時の材料。未review研究と過去のreview済み評価を別枠で表す。"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

from baibai_engine.investment_judgment.company_research.contracts import (
    HypothesisCheck,
    ObservedFact,
    ResearchFindings,
    Source,
)
from baibai_engine.investment_judgment.company_research.document import ResearchRecord
from baibai_engine.investment_judgment.contracts import NonEmptyStr
from baibai_engine.investment_judgment.stock_assessment.document import AssessmentRef
from baibai_engine.investment_judgment.stock_assessment.price_value import (
    PriceValueCase,
    recorded_price_observation,
)


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


class AssessmentValuationContext(_Context):
    status: Literal["resolved", "unresolved"]
    unresolved_reason: str | None


class HistoricalPriceCase(_Context):
    # The containing assessment already retains the observed facts, including alternate quotes.
    market_price_fact_id: NonEmptyStr | None
    case: PriceValueCase

    def brief(self, facts: tuple[ObservedFact, ...]) -> dict[str, JsonValue]:
        quote = next((fact for fact in facts if fact.fact_id == self.market_price_fact_id), None)
        result = self.case.model_dump(mode="json")
        result.setdefault("acquisition_limit", None)
        result["observed_quote"] = recorded_price_observation(quote)
        # Keep the authored price reversal together with its qualifications; the general
        # business argument/evidence list is only a pointer to the same exact pair.
        conditions = self.case.price_compatible_conditions
        if conditions is not None and len(conditions) > 400:
            result["price_compatible_conditions"] = conditions[:399] + "…"
            result["price_compatible_conditions_truncated"] = True
        if len(self.case.value_fact_ids) > 3:
            result["value_fact_ids"] = list(self.case.value_fact_ids[:3])
            result["value_fact_ids_omitted"] = len(self.case.value_fact_ids) - 3
        return result


class AssessmentReviewContext(_Context):
    reviewed_at: AwareDatetime
    primary_source_check: Literal["verified", "partially_verified", "unverified"]
    checked_source_ids: tuple[NonEmptyStr, ...]
    review_scope: NonEmptyStr
    strongest_countercase: NonEmptyStr
    uncertainty_acceptance_reason: NonEmptyStr | None


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

    valuation_context: AssessmentValuationContext | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    historical_price_case: HistoricalPriceCase | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    independent_review: AssessmentReviewContext | None = Field(
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
            **(
                {
                    "independent_review": _review_brief(
                        assessment.independent_review, assessment.sources
                    )
                }
                if assessment.independent_review is not None
                else {}
            ),
            **(
                {"valuation_context": _valuation_brief(assessment.valuation_context)}
                if assessment.valuation_context is not None
                else {}
            ),
            **(
                {"price_value_case": assessment.historical_price_case.brief(assessment.facts)}
                if assessment.historical_price_case is not None
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


def _valuation_brief(valuation: AssessmentValuationContext) -> dict[str, object]:
    reason = valuation.unresolved_reason
    return {
        "status": valuation.status,
        "unresolved_reason": reason[:400] if reason is not None else None,
        "unresolved_reason_truncated": reason is not None and len(reason) > 400,
    }


def _review_brief(
    review: AssessmentReviewContext, sources: tuple[Source, ...]
) -> dict[str, object]:
    return {
        "reviewed_at": review.reviewed_at.isoformat(),
        "primary_source_check": review.primary_source_check,
        **{
            name: {"text": text[:400], "text_truncated": len(text) > 400}
            for name, text in (
                ("review_scope", review.review_scope),
                ("strongest_countercase", review.strongest_countercase),
                ("uncertainty_acceptance_reason", review.uncertainty_acceptance_reason),
            )
            if text is not None
        },
        **_source_brief(review.checked_source_ids, sources),
    }


def _questions_brief(questions: tuple[str, ...]) -> dict[str, object]:
    return {
        # 金額や末尾の限定で切れた抜粋を、完結した問いと読ませない。
        "unresolved_questions": [
            text if len(text) <= 240 else text[:239] + "…" for text in questions[:3]
        ],
        **({"unresolved_questions_omitted": len(questions) - 3} if len(questions) > 3 else {}),
    }


def _hypotheses_brief(
    hypotheses: tuple[HypothesisCheck, ...], sources: tuple[Source, ...]
) -> dict[str, object]:
    # 各結果の最初の例を含め、残枠は原順序で埋める。重要度や独立票の順位ではない。
    first_by_result: dict[str, int] = {}
    for index, item in enumerate(hypotheses):
        first_by_result.setdefault(item.result, index)
    selected = set(first_by_result.values())
    for index in range(len(hypotheses)):
        if len(selected) >= 3:
            break
        selected.add(index)
    result: dict[str, object] = {
        "hypotheses": [
            {
                "statement": item.statement[:240],
                "statement_truncated": len(item.statement) > 240,
                "result": item.result,
                "reason": item.reason[:240],
                "reason_truncated": len(item.reason) > 240,
                **_source_brief(item.source_ids, sources),
            }
            for index, item in enumerate(hypotheses)
            if index in selected
        ]
    }
    omitted = tuple(item for index, item in enumerate(hypotheses) if index not in selected)
    if omitted:
        # 表示した例だけを全体の結論にせず、残る論点は同じexactへ戻る。
        result.update(
            hypotheses_omitted=len(omitted),
            omitted_hypothesis_results=sorted({item.result for item in omitted}),
        )
    return result


def _source_brief(source_ids: tuple[str, ...], sources: tuple[Source, ...]) -> dict[str, object]:
    # URLそのものは複写せず、同じ文字列の参照も独立票にしない。
    by_id = {source.source_id: source for source in sources}
    return {
        "sources": [
            {
                "source_id": source_id,
                "source_tier": by_id[source_id].source_tier,
                **_same_external_ref_brief(by_id[source_id], sources),
                "as_of": by_id[source_id].as_of.isoformat(),
                "retrieved_at": by_id[source_id].retrieved_at.isoformat(),
            }
            for source_id in source_ids[:3]
        ],
        "sources_omitted": max(0, len(source_ids) - 3),
    }


def _same_external_ref_brief(source: Source, sources: tuple[Source, ...]) -> dict[str, object]:
    if source.ref is None:
        return {}
    matching = [
        item.source_id
        for item in sources
        if item.source_id != source.source_id and item.ref == source.ref
    ]
    return (
        {
            "same_external_ref_source_ids": matching[:3],
            "same_external_ref_sources_omitted": max(0, len(matching) - 3),
        }
        if matching
        else {}
    )


def _absence_brief(value: NotFound | Unreadable) -> dict[str, object]:
    if isinstance(value, Unreadable):
        return {
            "status": value.status,
            "record_id": value.record_id,
            "reason": value.reason[:400],
            "display": "reason_excerpt; exact read reports the full error",
        }
    return value.model_dump(mode="json")
