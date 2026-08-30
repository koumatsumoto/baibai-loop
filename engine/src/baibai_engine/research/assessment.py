"""割安機会評価 (bargain assessment) — 1 opportunity cycle の統合判断。

深掘りした候補を横に並べ、「今どれが最もお買い得か / なぜ買わないか」を 1 つの
immutable revision へ固定する。shortlist が「どれを調べるか」の判断であるのに対し、
これは「調べ終えて何を結論したか」の判断である。

判断の散文はここが正本だが、**数値は正本ではない**: 5 年 base CAGR・FV・乖離・
break-even は promoted thesis から read 時に機械で導出する。注文数量と指値は判断時の
`plan-limit` だけが出し、ここへ永続化しない。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from baibai_engine.appdb.json import canonical_json

from .thesis import (
    ThesisDocument,
    UnpublishedThesis,
    evaluate_thesis,
)

BARGAIN_ASSESSMENT_SCHEMA_VERSION = 5

type AssessmentResult = Literal["buy", "no_actionable_bargain", "defer"]
type CaseDisposition = Literal["selected", "reject", "defer"]


class AssessmentError(ValueError):
    pass


class AssessmentConflictError(AssessmentError):
    pass


class ResearchQuestion(BaseModel):
    """shortlist が「これを確かめるなら深掘りする価値がある」と書いた論点の決着。

    候補が research slot を得た理由そのものなので、答えられなかった場合も
    `unresolved` として残し、黙って落とさない。
    """

    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1)
    answer: str = Field(min_length=1)
    status: Literal["answered", "unresolved"]


class SourceCaveat(BaseModel):
    """一次情報が ok でない claim。判断への影響を省略せずレポートへ出す。"""

    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1)
    status: Literal["missing", "stale", "failed", "blocked"]
    decision_impact: str = Field(min_length=1)


class AssessmentCase(BaseModel):
    """深掘りした 1 銘柄の結論。判断の要点を immutable thesis へ束縛する。"""

    model_config = ConfigDict(extra="forbid")
    ticker: str = Field(pattern=r"^[0-9A-Z]{4}$")
    name: str | None = None
    disposition: CaseDisposition
    disposition_reason: str = Field(min_length=1)
    thesis_id: str = Field(min_length=1)
    thesis_core_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_id: str | None = None
    business_model: str = Field(min_length=1)
    value_capture: str = Field(min_length=1)
    growth_quality: str = Field(min_length=1)
    financial_resilience: str = Field(min_length=1)
    strongest_countercase: str = Field(min_length=1)
    catalyst: str = Field(min_length=1)
    research_questions: tuple[ResearchQuestion, ...] = Field(min_length=1)
    unknowns: tuple[str, ...] = ()
    source_caveats: tuple[SourceCaveat, ...] = ()


class ContentReviewBinding(BaseModel):
    """独立 content review の結論を、review した draft の内容そのものへ束縛する。

    `draft_sha256` は review 以外の全内容の hash。review 後に散文を書き換えると
    publish が落ちるので、review 済みの結論と publish される内容が乖離しない。
    """

    model_config = ConfigDict(extra="forbid")
    attempt: int = Field(ge=1)
    reviewer_identity: str = Field(min_length=1)
    reviewed_at: datetime
    conclusion: Literal["pass"]
    draft_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    open_findings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_reviewed_at_carries_a_timezone(self) -> Self:
        if self.reviewed_at.tzinfo is None:
            raise ValueError("reviewed_at must include a timezone")
        return self


class BargainAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[5]
    kind: Literal["bargain_assessment"]
    assessment_id: str
    as_of: date
    published_at: datetime
    result: AssessmentResult
    headline: str = Field(min_length=1)
    shortlist_id: str = Field(min_length=1)
    macro_context_id: str | None = None
    comparison: str = Field(min_length=1)
    forgone: str = Field(min_length=1)
    cases: tuple[AssessmentCase, ...] = Field(min_length=1)
    review: ContentReviewBinding

    @model_validator(mode="after")
    def validate_publication(self) -> Self:
        if not re.fullmatch(r"bargain-assessment-\d{8}-[a-z0-9-]+", self.assessment_id):
            raise ValueError("assessment_id has an invalid format")
        if self.published_at.tzinfo is None:
            raise ValueError("published_at must include a timezone")
        tickers = [case.ticker for case in self.cases]
        if len(tickers) != len(set(tickers)):
            raise ValueError("assessment case ticker must be unique")
        selected = [case for case in self.cases if case.disposition == "selected"]
        if len(selected) > 1:
            raise ValueError("at most one case can be selected in one assessment")
        if self.result == "buy":
            if not selected:
                raise ValueError("a buy result requires exactly one selected case")
            if selected[0].review_id is None:
                raise ValueError("a buy result requires the selected independent review")
        else:
            if selected:
                raise ValueError("only a buy result can carry a selected case")
        return self

    def payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def assessment_draft_sha256(assessment: BargainAssessment) -> str:
    """content review が束縛する対象 — 判断内容そのもの — の hash。

    `review` 自身と、判断内容ではない `published_at` を除く。review 後に publish
    時刻が動いても hash は変わらず、判断の散文が動けば変わる。
    """
    payload = assessment.payload()
    payload.pop("review", None)
    payload.pop("published_at", None)
    return _sha256_json(payload)


def derive_case_machine_values(document: ThesisDocument) -> dict[str, object]:
    """read surface に必要な case の機械値を immutable thesis から導出する。"""
    # Only the scenarios are read here; the identity never leaves this call.
    result = evaluate_thesis(document, identity=UnpublishedThesis.DRAFT)
    base = next(
        (
            scenario
            for scenario in result.scenarios
            if scenario.horizon_years == 5 and scenario.name == "base"
        ),
        None,
    )
    break_even = result.five_year_base_break_even
    estimates = document.estimates
    fair_value = estimates.current_fair_value_yen
    entry_price = estimates.entry_price_basis_yen
    return {
        "five_year_base_cagr_pct": (None if base is None else round(base.total_return_cagr_pct, 4)),
        "required_return_pct": _float(estimates.required_5y_base_cagr_pct),
        "fair_value_yen": _float(fair_value),
        "fv_gap_pct": _fv_gap_pct(fair_value, entry_price),
        "base_terminal_multiple": (
            None if break_even is None else _float(break_even.base_terminal_valuation_multiple)
        ),
        "break_even_terminal_multiple": (
            None
            if break_even is None
            else _float(break_even.break_even_terminal_valuation_multiple)
        ),
        "terminal_multiple_buffer": (
            None if break_even is None else _float(break_even.terminal_multiple_downside_buffer)
        ),
        "break_even_earnings_growth_pct": (
            None if break_even is None else _float(break_even.break_even_annual_earnings_growth_pct)
        ),
        "earnings_growth_buffer_pp": (
            None
            if break_even is None
            else _float(break_even.earnings_growth_downside_buffer_pct_points)
        ),
        "observed_trailing_multiple": (
            None if break_even is None else _float(break_even.observed_trailing_multiple)
        ),
        "permanent_loss_conclusion": document.judgment.permanent_loss_conclusion,
        "adverse_risk_axes": sorted(
            risk.axis for risk in document.permanent_loss_risks if risk.assessment == "adverse"
        ),
    }


def _fv_gap_pct(fair_value: object, entry_price: object) -> float | None:
    fv = _float(fair_value)
    price = _float(entry_price)
    if fv is None or price is None or price == 0:
        return None
    return round((fv / price - 1) * 100, 4)


def _float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def _sha256_json(payload: object) -> str:
    return sha256(canonical_json(payload).encode("utf-8")).hexdigest()


__all__ = [
    "BARGAIN_ASSESSMENT_SCHEMA_VERSION",
    "AssessmentCase",
    "AssessmentConflictError",
    "AssessmentError",
    "AssessmentResult",
    "BargainAssessment",
    "CaseDisposition",
    "ContentReviewBinding",
    "ResearchQuestion",
    "SourceCaveat",
    "assessment_draft_sha256",
    "derive_case_machine_values",
]
