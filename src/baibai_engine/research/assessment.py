"""割安機会評価 (bargain assessment) — 1 opportunity cycle の統合判断。

深掘りした候補を横に並べ、「今どれが最もお買い得か」と「どう買うか / なぜ買わないか」
までを 1 つの immutable revision へ固定する。shortlist が「どれを調べるか」の判断で
あるのに対し、これは「調べ終えて何を結論したか」の判断であり、proposal を作らない
サイクル — `no_actionable_bargain` と `defer` — にも成立する。

判断の散文はここが正本だが、**数値は正本ではない**: 5 年 base CAGR・FV・乖離・
break-even・指値・数量・想定約定額は promoted thesis と proposal から機械で導出する。
publish は同じ導出をやり直して draft の値と照合するので、scaffold 後に手で書き換えた
数値は保存されない。詳細な調査全文は thesis と operation session artifacts に残り、
ここには判断に必要な要点だけを置く。
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from baibai_engine.appdb.json import canonical_json
from baibai_engine.appdb.paths import database_path
from baibai_engine.appdb.read import connect_read_only
from baibai_engine.appdb.write import connect_rw, initialize_database
from baibai_engine.foundation.reject_classification import RejectClass

from .thesis import (
    ThesisDocument,
    ThesisError,
    evaluate_thesis,
    require_recorded_identity,
)

BARGAIN_ASSESSMENT_SCHEMA_VERSION = 2

# 機械値の照合許容差。thesis 評価は Decimal、YAML 往復は float を通るので、
# 表示桁の丸めだけを許し、書き換えは許さない幅にする。
_NUMERIC_TOLERANCE = Decimal("0.005")

type AssessmentResult = Literal["proposal", "no_actionable_bargain", "defer"]
type LaneDisposition = Literal["selected", "reject", "defer"]


class AssessmentError(ValueError):
    pass


class AssessmentConflictError(AssessmentError):
    pass


class LaneMachineValues(BaseModel):
    """promoted thesis から再導出する値。scaffold が書き、publish が照合する。

    リターン側の数値だけでなく永久損失の結論も含める。リスクリワードは片側だけでは
    読めないので、レポートは両側を同じ機械経路から供給する。
    """

    model_config = ConfigDict(extra="forbid")
    five_year_base_cagr_pct: float | None = None
    required_return_pct: float | None = None
    fair_value_yen: float | None = None
    fv_gap_pct: float | None = None
    base_terminal_multiple: float | None = None
    break_even_terminal_multiple: float | None = None
    terminal_multiple_buffer: float | None = None
    break_even_earnings_growth_pct: float | None = None
    earnings_growth_buffer_pp: float | None = None
    observed_trailing_multiple: float | None = None
    permanent_loss_conclusion: Literal["acceptable", "elevated", "unknown"] | None = None
    adverse_risk_axes: tuple[str, ...] = ()


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


class AssessmentLane(BaseModel):
    """深掘りした 1 銘柄の結論。判断の要点と、thesis へ束縛した機械値を持つ。"""

    model_config = ConfigDict(extra="forbid")
    ticker: str = Field(pattern=r"^[0-9A-Z]{4}$")
    name: str | None = None
    disposition: LaneDisposition
    disposition_reason: str = Field(min_length=1)
    # disposition_reason が判断の正本。class は棄却理由の頻度集計にだけ使う。
    reject_class: RejectClass | None = None
    thesis_id: str = Field(min_length=1)
    thesis_core_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_id: str | None = None
    machine: LaneMachineValues = LaneMachineValues()
    business_model: str = Field(min_length=1)
    value_capture: str = Field(min_length=1)
    growth_quality: str = Field(min_length=1)
    financial_resilience: str = Field(min_length=1)
    strongest_countercase: str = Field(min_length=1)
    catalyst: str = Field(min_length=1)
    research_questions: tuple[ResearchQuestion, ...] = Field(min_length=1)
    unknowns: tuple[str, ...] = ()
    source_caveats: tuple[SourceCaveat, ...] = ()

    @model_validator(mode="after")
    def validate_reject_class_matches_disposition(self) -> Self:
        if self.disposition in {"reject", "defer"} and self.reject_class is None:
            raise ValueError("reject/defer assessment lane must include a reject_class")
        if self.disposition == "selected" and self.reject_class is not None:
            raise ValueError("selected assessment lane must not include a reject_class")
        return self


class PurchasePlan(BaseModel):
    """proposal から再導出する購入方法。何を・いくらで・何株・いつまで。"""

    model_config = ConfigDict(extra="forbid")
    proposal_id: str = Field(min_length=1)
    proposal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ticker: str = Field(pattern=r"^[0-9A-Z]{4}$")
    limit_price_yen: float
    quantity: int = Field(gt=0)
    notional_yen: float
    max_acceptable_price_yen: float
    close_yen: float
    price_as_of: date
    expires_at: datetime
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_expiry_carries_a_timezone(self) -> Self:
        if self.expires_at.tzinfo is None:
            raise ValueError("expires_at must include a timezone")
        return self


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
    schema_version: Literal[2]
    kind: Literal["bargain_assessment"]
    assessment_id: str
    as_of: date
    published_at: datetime
    result: AssessmentResult
    headline: str = Field(min_length=1)
    shortlist_id: str = Field(min_length=1)
    macro_context_id: str | None = None
    comparison: str = Field(min_length=1)
    entry_timing: str | None = None
    forgone: str = Field(min_length=1)
    lanes: tuple[AssessmentLane, ...] = Field(min_length=1)
    purchase: PurchasePlan | None = None
    review: ContentReviewBinding

    @model_validator(mode="after")
    def validate_publication(self) -> Self:
        if not re.fullmatch(r"bargain-assessment-\d{8}-[a-z0-9-]+", self.assessment_id):
            raise ValueError("assessment_id has an invalid format")
        if self.published_at.tzinfo is None:
            raise ValueError("published_at must include a timezone")
        tickers = [lane.ticker for lane in self.lanes]
        if len(tickers) != len(set(tickers)):
            raise ValueError("assessment lane ticker must be unique")
        selected = [lane for lane in self.lanes if lane.disposition == "selected"]
        if len(selected) > 1:
            raise ValueError("at most one lane can be selected in one proposal round")
        if self.result == "proposal":
            if not selected:
                raise ValueError("a proposal result requires exactly one selected lane")
            if self.purchase is None:
                raise ValueError("a proposal result requires a purchase plan")
            if self.purchase.ticker != selected[0].ticker:
                raise ValueError("purchase plan ticker must match the selected lane")
            if self.entry_timing is None or not self.entry_timing.strip():
                raise ValueError("a proposal result requires entry_timing")
        else:
            if selected:
                raise ValueError("only a proposal result can carry a selected lane")
            if self.purchase is not None:
                raise ValueError("a purchase plan requires a proposal result")
        return self

    def payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")


@dataclass(frozen=True, slots=True)
class _StoredThesis:
    ticker: str
    core_sha256: str
    document: ThesisDocument


class BargainAssessmentService:
    """canonical store への publish と、機械値の再導出照合。"""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path

    def check(self, assessment: BargainAssessment) -> None:
        """store へ書かずに、参照束縛と機械値の再導出を検証する。

        content review の hash 束縛だけは求めない。review 前の draft を検証して
        期待 hash を知るための経路であり、束縛は publish が求める。
        """
        self._verify_bindings(assessment, require_review_binding=False)

    def publish(self, assessment: BargainAssessment) -> BargainAssessment:
        self._verify_bindings(assessment, require_review_binding=True)
        initialize_database(self._db_path)
        payload = canonical_json(assessment.payload())
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT payload FROM bargain_assessment WHERE assessment_id = ?",
                    (assessment.assessment_id,),
                ).fetchone()
                if row is not None:
                    if str(row[0]) == payload:
                        connection.rollback()
                        return assessment
                    raise AssessmentConflictError(
                        f"assessment differs from existing publication: {assessment.assessment_id}"
                    )
                connection.execute(
                    """
                    INSERT INTO bargain_assessment (
                        assessment_id, as_of, published_at, result, shortlist_id, payload
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        assessment.assessment_id,
                        assessment.as_of.isoformat(),
                        assessment.published_at.isoformat(),
                        assessment.result,
                        assessment.shortlist_id,
                        payload,
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return assessment

    def _verify_bindings(
        self, assessment: BargainAssessment, *, require_review_binding: bool
    ) -> None:
        expected_draft = assessment_draft_sha256(assessment)
        if require_review_binding and assessment.review.draft_sha256 != expected_draft:
            raise AssessmentConflictError(
                f"content review binds a different draft, expected {expected_draft}"
            )
        shortlist = self._shortlist(assessment.shortlist_id)
        shortlist_as_of = str(shortlist.get("as_of", ""))
        if shortlist_as_of and assessment.as_of < date.fromisoformat(shortlist_as_of):
            raise AssessmentConflictError(
                f"assessment as_of {assessment.as_of} precedes shortlist {shortlist_as_of}"
            )
        shortlist_tickers = _selected_tickers(shortlist, assessment.shortlist_id)
        for lane in assessment.lanes:
            if lane.ticker not in shortlist_tickers:
                raise AssessmentConflictError(
                    f"lane {lane.ticker} is not a selected candidate of {assessment.shortlist_id}"
                )
            stored = self._stored_thesis(lane.thesis_id)
            if stored.ticker != lane.ticker:
                raise AssessmentConflictError(
                    f"thesis {lane.thesis_id} belongs to {stored.ticker}, not {lane.ticker}"
                )
            if stored.core_sha256 != lane.thesis_core_sha256:
                raise AssessmentConflictError(
                    f"thesis {lane.thesis_id} has moved since the draft was written"
                )
            _require_matching_machine_values(lane, derive_lane_machine_values(stored.document))
        if assessment.purchase is not None:
            self._verify_purchase(assessment.purchase, assessment.lanes)

    def _verify_purchase(self, purchase: PurchasePlan, lanes: tuple[AssessmentLane, ...]) -> None:
        row = self._row(
            "SELECT ticker, thesis_id, payload FROM proposal WHERE proposal_id = ?",
            (purchase.proposal_id,),
        )
        if row is None:
            raise AssessmentConflictError(f"proposal is unavailable: {purchase.proposal_id}")
        ticker = str(row[0])
        thesis_id = str(row[1])
        payload = json.loads(str(row[2]))
        if ticker != purchase.ticker:
            raise AssessmentConflictError(
                f"proposal {purchase.proposal_id} belongs to {ticker}, not {purchase.ticker}"
            )
        selected = next(lane for lane in lanes if lane.disposition == "selected")
        if selected.thesis_id != thesis_id:
            raise AssessmentConflictError(
                f"proposal {purchase.proposal_id} binds thesis {thesis_id}, "
                f"not the selected lane's {selected.thesis_id}"
            )
        digest = _sha256_json(payload)
        if digest != purchase.proposal_sha256:
            raise AssessmentConflictError(
                f"proposal {purchase.proposal_id} has moved since the draft was written"
            )
        _require_matching_purchase(purchase, payload)

    def _shortlist(self, shortlist_id: str) -> dict[str, object]:
        row = self._row(
            "SELECT payload FROM shortlist WHERE shortlist_id = ?",
            (shortlist_id,),
        )
        if row is None:
            raise AssessmentConflictError(f"shortlist is unavailable: {shortlist_id}")
        payload = json.loads(str(row[0]))
        if not isinstance(payload, dict):
            raise AssessmentConflictError(f"shortlist payload is not an object: {shortlist_id}")
        return payload

    def _stored_thesis(self, thesis_id: str) -> _StoredThesis:
        row = self._row(
            "SELECT ticker, core_sha256, payload FROM thesis WHERE thesis_id = ?",
            (thesis_id,),
        )
        if row is None:
            raise AssessmentConflictError(f"thesis is unavailable: {thesis_id}")
        payload = json.loads(str(row[2]))
        try:
            document = ThesisDocument.model_validate(payload)
        except (ThesisError, ValueError) as error:
            raise AssessmentConflictError(f"thesis {thesis_id} cannot be read: {error}") from error
        return _StoredThesis(
            ticker=str(row[0]),
            core_sha256=require_recorded_identity(row[1], thesis_id),
            document=document,
        )

    def _row(self, sql: str, parameters: tuple[str, ...]) -> sqlite3.Row | None:
        path = database_path(self._db_path)
        if not path.is_file():
            raise AssessmentConflictError(f"application database is unavailable: {path}")
        with closing(connect_read_only(path)) as connection:
            row: sqlite3.Row | None = connection.execute(sql, parameters).fetchone()
            return row


def assessment_draft_sha256(assessment: BargainAssessment) -> str:
    """content review が束縛する対象 — 判断内容そのもの — の hash。

    `review` 自身と、判断内容ではない `published_at` を除く。review 後に publish
    時刻が動いても hash は変わらず、散文や機械値が動けば変わる。
    """
    payload = assessment.payload()
    payload.pop("review", None)
    payload.pop("published_at", None)
    return _sha256_json(payload)


def _selected_tickers(payload: Mapping[str, object], shortlist_id: str) -> frozenset[str]:
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise AssessmentConflictError(f"shortlist has no entries: {shortlist_id}")
    return frozenset(
        str(entry["ticker"])
        for entry in entries
        if isinstance(entry, dict) and entry.get("decision") == "selected"
    )


def derive_lane_machine_values(document: ThesisDocument) -> LaneMachineValues:
    """thesis から lane の機械値を導出する。scaffold と publish が同じ経路を使う。"""
    result = evaluate_thesis(document)
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
    return LaneMachineValues(
        five_year_base_cagr_pct=(None if base is None else round(base.total_return_cagr_pct, 4)),
        required_return_pct=_float(estimates.required_5y_base_cagr_pct),
        fair_value_yen=_float(fair_value),
        fv_gap_pct=_fv_gap_pct(fair_value, entry_price),
        base_terminal_multiple=(
            None if break_even is None else _float(break_even.base_terminal_valuation_multiple)
        ),
        break_even_terminal_multiple=(
            None
            if break_even is None
            else _float(break_even.break_even_terminal_valuation_multiple)
        ),
        terminal_multiple_buffer=(
            None if break_even is None else _float(break_even.terminal_multiple_downside_buffer)
        ),
        break_even_earnings_growth_pct=(
            None if break_even is None else _float(break_even.break_even_annual_earnings_growth_pct)
        ),
        earnings_growth_buffer_pp=(
            None
            if break_even is None
            else _float(break_even.earnings_growth_downside_buffer_pct_points)
        ),
        observed_trailing_multiple=(
            None if break_even is None else _float(break_even.observed_trailing_multiple)
        ),
        permanent_loss_conclusion=document.judgment.permanent_loss_conclusion,
        adverse_risk_axes=tuple(
            sorted(
                risk.axis for risk in document.permanent_loss_risks if risk.assessment == "adverse"
            )
        ),
    )


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


def _require_matching_machine_values(lane: AssessmentLane, derived: LaneMachineValues) -> None:
    """draft の機械値が thesis からの再導出と一致することを求める。

    scaffold が書いた値を手で書き換えても、publish は保存しない。散文は判断だが、
    数値は thesis の従属変数である。
    """
    drifted = [
        name
        for name in LaneMachineValues.model_fields
        if not _values_agree(getattr(lane.machine, name), getattr(derived, name))
    ]
    if drifted:
        raise AssessmentConflictError(
            f"{lane.ticker} machine values do not match the thesis: {', '.join(sorted(drifted))}"
        )


def _require_matching_purchase(purchase: PurchasePlan, payload: dict[str, object]) -> None:
    planned = payload.get("planned_limit")
    if not isinstance(planned, dict):
        raise AssessmentConflictError(
            f"proposal {purchase.proposal_id} carries no planned limit to bind"
        )
    expected: dict[str, object] = {
        "limit_price_yen": purchase.limit_price_yen,
        "quantity": purchase.quantity,
        "notional_yen": purchase.notional_yen,
        "max_acceptable_price_yen": purchase.max_acceptable_price_yen,
        "close_yen": purchase.close_yen,
    }
    drifted = [
        name for name, value in expected.items() if not _numbers_agree(value, planned.get(name))
    ]
    if str(planned.get("expires_at", "")) != purchase.expires_at.isoformat():
        drifted.append("expires_at")
    if drifted:
        raise AssessmentConflictError(
            f"purchase plan does not match proposal {purchase.proposal_id}: "
            f"{', '.join(sorted(drifted))}"
        )


def _values_agree(left: object, right: object) -> bool:
    """機械値の一致判定。数値は表示桁の丸めだけ許し、それ以外は完全一致を求める。"""
    if isinstance(left, str) or isinstance(right, str):
        return left == right
    if isinstance(left, list | tuple) and isinstance(right, list | tuple):
        return list(left) == list(right)
    if isinstance(left, list | tuple) or isinstance(right, list | tuple):
        return False
    return _numbers_agree(left, right)


def _numbers_agree(left: object, right: object) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        return abs(Decimal(str(left)) - Decimal(str(right))) <= _NUMERIC_TOLERANCE
    except (ArithmeticError, ValueError):
        return False


def _sha256_json(payload: object) -> str:
    return sha256(canonical_json(payload).encode("utf-8")).hexdigest()


__all__ = [
    "BARGAIN_ASSESSMENT_SCHEMA_VERSION",
    "AssessmentConflictError",
    "AssessmentError",
    "AssessmentLane",
    "AssessmentResult",
    "BargainAssessment",
    "BargainAssessmentService",
    "ContentReviewBinding",
    "LaneDisposition",
    "LaneMachineValues",
    "PurchasePlan",
    "ResearchQuestion",
    "SourceCaveat",
    "assessment_draft_sha256",
    "derive_lane_machine_values",
]
