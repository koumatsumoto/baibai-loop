"""評価と固定した入力から作る、取引事実とは独立した投資提案の契約。"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from hashlib import sha256
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from baibai_engine.foundation.time import JST
from baibai_engine.market.ticker import normalize_ticker
from baibai_engine.position.price_grid import common_stock_limit_is_valid, floor_common_stock_limit
from baibai_engine.research.assessment import AssessmentRef
from baibai_engine.research.selection import ResearchTarget
from baibai_engine.storage.application.json import canonical_json
from baibai_engine.valuation.scenario import finite_decimal

INVESTMENT_PROPOSAL_SCHEMA_VERSION = 1
_CONFIG = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)
_UUID_PATTERN = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def _strip(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


def _ticker(value: object) -> object:
    return normalize_ticker(value) if isinstance(value, str) else value


def _date(value: object) -> object:
    return date.fromisoformat(value) if isinstance(value, str) else value


def _instant(value: object) -> object:
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def _tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


type NonEmptyStr = Annotated[str, BeforeValidator(_strip), Field(min_length=1)]
type Ticker = Annotated[str, BeforeValidator(_ticker), Field(pattern=r"^[0-9A-Z]{4}$")]
type ISODate = Annotated[date, BeforeValidator(_date)]
type Instant = Annotated[AwareDatetime, BeforeValidator(_instant)]
type Money = Annotated[Decimal, BeforeValidator(finite_decimal)]
type PositiveMoney = Annotated[Money, Field(gt=0)]
type NonnegativeMoney = Annotated[Money, Field(ge=0)]
type PositiveInt = Annotated[int, Field(gt=0)]
type JsonTuple[T] = Annotated[tuple[T, ...], BeforeValidator(_tuple)]
type SHA256 = Annotated[NonEmptyStr, Field(pattern=r"^[0-9a-f]{64}$")]
type RequestScope = Literal["discover_new", "specified_stocks", "review_positions"]
type ProposalAction = Literal["buy", "hold", "sell", "no_action", "defer"]
type HoldingBasis = Literal["investment_case_intact", "sufficient_remaining_reward"]
type SellReason = Literal["economic_invalidation", "insufficient_remaining_reward"]


class ProposalError(ValueError):
    """提案の契約・発行条件を満たさない。"""


class ProposalConflictError(ProposalError):
    """固定したidentityまたは事実入力が一致しない。"""


class _ProposalValue(BaseModel):
    model_config = _CONFIG


class GateReason(_ProposalValue):
    """Policyが返す機械codeと、不足の具体的な説明。"""

    code: NonEmptyStr
    message: NonEmptyStr


class PositionInput(_ProposalValue):
    ticker: Ticker
    sector: NonEmptyStr
    common_factors: JsonTuple[NonEmptyStr]
    quantity: PositiveInt
    cost_yen: NonnegativeMoney
    quantity_basis_confirmed: bool
    original_assessment_ref: AssessmentRef | None = None
    original_investment_reason: NonEmptyStr | None = None
    original_reason_unavailable_reason: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _original_reason(self) -> Self:
        if self.original_assessment_ref is not None:
            if self.original_investment_reason or self.original_reason_unavailable_reason:
                raise ValueError("an original assessment reference replaces copied reason prose")
        elif bool(self.original_investment_reason) == bool(self.original_reason_unavailable_reason):
            raise ValueError("position requires its original reason or its unavailable reason")
        if len(set(self.common_factors)) != len(self.common_factors):
            raise ValueError("position common factors must be unique")
        return self


class QuoteInput(_ProposalValue):
    ticker: Ticker
    price_yen: PositiveMoney
    observed_at: Instant
    price_as_of: ISODate
    price_basis: Literal["last_close_unadjusted", "realtime"]
    source_ref: NonEmptyStr
    basis_confirmed: bool


class ReservationInput(_ProposalValue):
    reservation_id: NonEmptyStr
    ticker: Ticker
    reserved_yen: Money
    decision_reference: NonEmptyStr
    sector: NonEmptyStr
    common_factors: JsonTuple[NonEmptyStr]

    @model_validator(mode="after")
    def _unique_factors(self) -> Self:
        if len(set(self.common_factors)) != len(self.common_factors):
            raise ValueError("reservation common factors must be unique")
        return self


class SecurityClassification(_ProposalValue):
    ticker: Ticker
    market_segment: NonEmptyStr
    sector_33: NonEmptyStr
    snapshot_date: ISODate


class ProposalInputs(_ProposalValue):
    """一回の収集で固定するcash・保有・注文・価格・分類の事実。"""

    checked_at: Instant
    ledger_head: Annotated[int, Field(ge=0)]
    ledger_as_of: Instant
    available_cash_yen: Money
    positions: JsonTuple[PositionInput]
    active_reservations: JsonTuple[ReservationInput]
    prior_buy_execution_references: JsonTuple[NonEmptyStr]
    quotes: JsonTuple[QuoteInput]
    policy_id: NonEmptyStr
    budget_min_yen: PositiveInt
    budget_max_yen: PositiveInt
    board_lot: PositiveInt
    security_classification: JsonTuple[SecurityClassification]
    total_capital_yen: Money | None

    @model_validator(mode="after")
    def _facts(self) -> Self:
        if self.budget_min_yen > self.budget_max_yen:
            raise ValueError("budget_min_yen cannot exceed budget_max_yen")
        if self.ledger_as_of > self.checked_at:
            raise ValueError("ledger_as_of cannot be after checked_at")
        for rows, keys, label in (
            (self.positions, [row.ticker for row in self.positions], "positions"),
            (self.quotes, [row.ticker for row in self.quotes], "quotes"),
            (
                self.active_reservations,
                [row.reservation_id for row in self.active_reservations],
                "active reservations",
            ),
            (
                self.security_classification,
                [row.ticker for row in self.security_classification],
                "security classifications",
            ),
        ):
            if len(rows) != len(set(keys)):
                raise ValueError(f"{label} must have unique identities")
        if len(self.prior_buy_execution_references) != len(
            set(self.prior_buy_execution_references)
        ):
            raise ValueError("prior buy execution references must be unique")
        checked_date = self.checked_at.astimezone(JST).date()
        if any(row.snapshot_date > checked_date for row in self.security_classification):
            raise ValueError("security classification snapshot cannot be after checked_at")
        return self


class ReviewedOutcome(_ProposalValue):
    ticker: Ticker
    purpose: Literal["new", "holding"]
    status: Literal["reviewed"]
    assessment_ref: AssessmentRef


class UnfinishedOutcome(_ProposalValue):
    ticker: Ticker
    purpose: Literal["new", "holding"]
    status: Literal["unfinished"]
    unfinished_reason: NonEmptyStr
    references: Annotated[JsonTuple[NonEmptyStr], Field(min_length=1)]


type ResearchOutcome = Annotated[ReviewedOutcome | UnfinishedOutcome, Field(discriminator="status")]


class ProposalResearchScope(_ProposalValue):
    """発行時にOperationへ束縛する、提案当時の対象と完成/未完成。"""

    operation_id: Annotated[NonEmptyStr, Field(pattern=r"^research-op-[0-9a-f]{32}$")]
    selection_sha256: SHA256
    targets: Annotated[JsonTuple[ResearchTarget], Field(min_length=1)]
    outcomes: Annotated[JsonTuple[ResearchOutcome], Field(min_length=1)]

    @model_validator(mode="after")
    def _coverage(self) -> Self:
        target_keys = [(target.ticker, target.purpose) for target in self.targets]
        outcome_keys = [(outcome.ticker, outcome.purpose) for outcome in self.outcomes]
        if len({key[0] for key in target_keys}) != len(target_keys):
            raise ValueError("research scope targets must have unique tickers")
        if len({key[0] for key in outcome_keys}) != len(outcome_keys):
            raise ValueError("research scope outcomes must have unique tickers")
        if set(target_keys) != set(outcome_keys):
            raise ValueError("research scope outcomes must exactly cover targets and purposes")
        if len({target.purpose for target in self.targets}) != 1:
            raise ValueError("research scope cannot mix new and holding targets")
        return self


class RemainingReward(_ProposalValue):
    status: Literal["sufficient", "insufficient", "uncertain"]
    reason: NonEmptyStr


class LimitTerms(_ProposalValue):
    """具体的提案条件。注文受理・約定や人間の採用を表さない。"""

    side: Literal["buy", "sell"]
    limit_price_yen: PositiveMoney
    quantity: PositiveInt
    notional_yen: PositiveMoney
    target_session: ISODate
    expires_at: Instant
    valuation_as_of: ISODate
    maximum_entry_price_yen: PositiveMoney | None
    price_grid_rule_id: Literal["tse-common-conservative-20261014"]
    conditions: Annotated[JsonTuple[NonEmptyStr], Field(min_length=1)]
    warnings: JsonTuple[NonEmptyStr] = ()

    @model_validator(mode="after")
    def _arithmetic(self) -> Self:
        if self.notional_yen != self.limit_price_yen * self.quantity:
            raise ValueError("notional_yen must equal limit_price_yen * quantity")
        if self.side == "buy":
            if self.maximum_entry_price_yen is None:
                raise ValueError("buy terms require maximum_entry_price_yen")
            if self.limit_price_yen > self.maximum_entry_price_yen:
                raise ValueError("buy limit cannot exceed maximum entry price")
        elif self.maximum_entry_price_yen is not None:
            raise ValueError("sell terms cannot carry a buy maximum entry price")
        expiry = self.expires_at.astimezone(JST)
        if expiry.date() != self.target_session or expiry.timetz().replace(tzinfo=None) != time(
            15, 30
        ):
            raise ValueError("expires_at must be target session 15:30 JST")
        return self


class _ProposalItem(_ProposalValue):
    ticker: Ticker
    rationale: NonEmptyStr
    review_conditions: Annotated[JsonTuple[NonEmptyStr], Field(min_length=1)]


class BuyItem(_ProposalItem):
    action: Literal["buy"]
    assessment_ref: AssessmentRef
    timing: Literal["at_current_limit", "wait_limit"]
    terms: LimitTerms | None
    terms_unavailable_reasons: JsonTuple[GateReason] = ()

    @model_validator(mode="after")
    def _terms(self) -> Self:
        _validate_terms_availability(self.terms, self.terms_unavailable_reasons)
        if self.terms is not None and self.terms.side != "buy":
            raise ValueError("buy item requires buy terms")
        allowed = {"price_grid_rule_unavailable", "security_classification_unavailable"}
        if any(reason.code not in allowed for reason in self.terms_unavailable_reasons):
            raise ValueError("buy terms may be unavailable only for price grid or classification")
        return self


class HoldItem(_ProposalItem):
    action: Literal["hold"]
    assessment_ref: AssessmentRef
    position: PositionInput
    holding_basis: HoldingBasis
    case_reassessment_reason: NonEmptyStr
    remaining_reward: RemainingReward | None = None
    return_calculation_unavailable_reason: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _basis(self) -> Self:
        if self.position.ticker != self.ticker:
            raise ValueError("hold item position ticker differs")
        if self.remaining_reward is not None and self.remaining_reward.status == "insufficient":
            raise ValueError("hold contradicts known insufficient remaining reward")
        if self.holding_basis == "sufficient_remaining_reward":
            if self.remaining_reward is None or self.remaining_reward.status != "sufficient":
                raise ValueError("sufficient reward hold requires sufficient remaining_reward")
            if self.return_calculation_unavailable_reason is not None:
                raise ValueError("sufficient reward hold cannot have unavailable calculation")
        return self


class SellItem(_ProposalItem):
    action: Literal["sell"]
    assessment_ref: AssessmentRef
    position: PositionInput
    reason_code: SellReason
    case_reassessment_reason: NonEmptyStr
    remaining_reward: RemainingReward | None
    terms: LimitTerms | None
    terms_unavailable_reasons: JsonTuple[GateReason] = ()

    @model_validator(mode="after")
    def _terms(self) -> Self:
        if self.position.ticker != self.ticker:
            raise ValueError("sell item position ticker differs")
        _validate_terms_availability(self.terms, self.terms_unavailable_reasons)
        if self.reason_code == "insufficient_remaining_reward" and (
            self.remaining_reward is None or self.remaining_reward.status != "insufficient"
        ):
            raise ValueError("insufficient reward sell requires insufficient remaining_reward")
        if self.terms is not None:
            if self.terms.side != "sell":
                raise ValueError("sell item requires sell terms")
            if not self.position.quantity_basis_confirmed:
                raise ValueError("sell terms require confirmed quantity basis")
            if self.terms.quantity != self.position.quantity:
                raise ValueError("initial sell terms must sell the entire holding")
        return self


class NoActionItem(_ProposalItem):
    action: Literal["no_action"]
    assessment_ref: AssessmentRef


class DeferItem(_ProposalItem):
    action: Literal["defer"]
    assessment_ref: AssessmentRef | None = None
    blockers: Annotated[JsonTuple[GateReason], Field(min_length=1)]
    resume_conditions: Annotated[JsonTuple[NonEmptyStr], Field(min_length=1)]


type ProposalItem = Annotated[
    BuyItem | HoldItem | SellItem | NoActionItem | DeferItem, Field(discriminator="action")
]


def _validate_terms_availability(terms: LimitTerms | None, reasons: tuple[GateReason, ...]) -> None:
    if (terms is None) != bool(reasons):
        raise ValueError("terms and terms_unavailable_reasons must be exclusive and exhaustive")


class LiquidityContext(_ProposalValue):
    """選択snapshotから固定したADV。現在runへの付替えを行わない。"""

    ticker: Ticker
    run_revision_id: NonEmptyStr | None
    analysis_as_of: ISODate | None
    rules_hash: NonEmptyStr | None
    adv_yen: Money | None
    unavailable_reason: NonEmptyStr | None

    @model_validator(mode="after")
    def _availability(self) -> Self:
        if (self.adv_yen is None) != (self.unavailable_reason is not None):
            raise ValueError("ADV value and unavailable_reason must be exclusive and exhaustive")
        if self.adv_yen is not None and (
            self.run_revision_id is None or self.analysis_as_of is None
        ):
            raise ValueError("available ADV requires its saved run and analysis date")
        return self


class ExposureRow(_ProposalValue):
    key: NonEmptyStr
    prospective_pct: Money
    warning_pct: Money


class PortfolioExposure(_ProposalValue):
    total_capital_yen: Money | None
    ticker: ExposureRow | None
    sector: ExposureRow | None
    common_factors: JsonTuple[ExposureRow]
    common_factor_unclassified_tickers: JsonTuple[Ticker]


class ProposalContentReview(_ProposalValue):
    review_id: Annotated[NonEmptyStr, Field(pattern=rf"^proposal-review-{_UUID_PATTERN}$")]
    attempt: PositiveInt
    reviewer_identity: NonEmptyStr
    reviewed_at: Instant
    conclusion: Literal["pass"]
    draft_sha256: SHA256
    open_findings: JsonTuple[NonEmptyStr] = ()

    @model_validator(mode="after")
    def _passing_review(self) -> Self:
        if self.open_findings:
            raise ValueError("a passing review cannot carry open findings")
        return self


class InvestmentProposal(_ProposalValue):
    """不変の提案本文と、当時の入力・比較範囲・具体条件。"""

    kind: Literal["investment_proposal"] = "investment_proposal"
    schema_version: Literal[1] = 1
    proposal_id: Annotated[NonEmptyStr, Field(pattern=rf"^investment-proposal-{_UUID_PATTERN}$")]
    as_of: ISODate
    prepared_at: Instant
    published_at: Instant
    request_scope: RequestScope
    research_scope: ProposalResearchScope
    headline: NonEmptyStr
    comparison: NonEmptyStr
    forgone: NonEmptyStr
    market_context_assessment_id: NonEmptyStr | None = None
    inputs: ProposalInputs
    items: Annotated[JsonTuple[ProposalItem], Field(min_length=1)]
    review: ProposalContentReview
    liquidity_context: JsonTuple[LiquidityContext] = ()
    warnings: JsonTuple[NonEmptyStr] = ()
    portfolio_exposure: PortfolioExposure

    @field_validator("schema_version", mode="before")
    @classmethod
    def _strict_version(cls, value: object) -> object:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("schema_version must be an integer")
        return value

    @model_validator(mode="after")
    def _scope_and_terms(self) -> Self:
        if not self.prepared_at <= self.review.reviewed_at <= self.published_at:
            raise ValueError("proposal requires prepared_at <= reviewed_at <= published_at")
        purpose = "holding" if self.request_scope == "review_positions" else "new"
        if any(target.purpose != purpose for target in self.research_scope.targets):
            raise ValueError("request scope and target purpose differ")
        outcomes = {outcome.ticker: outcome for outcome in self.research_scope.outcomes}
        if len({item.ticker for item in self.items}) != len(self.items):
            raise ValueError("proposal items must have unique tickers")
        if set(outcomes) != {item.ticker for item in self.items}:
            raise ValueError("proposal items must exactly cover research scope")
        positions = {position.ticker: position for position in self.inputs.positions}
        buy_notional = Decimal(0)
        for item in self.items:
            outcome = outcomes[item.ticker]
            if outcome.status == "unfinished":
                if item.action != "defer" or item.assessment_ref is not None:
                    raise ValueError(
                        "unfinished outcome requires defer without assessment reference"
                    )
                if not any(reason.code == "research_unfinished" for reason in item.blockers):
                    raise ValueError("unfinished outcome requires research_unfinished blocker")
            elif item.assessment_ref != outcome.assessment_ref:
                raise ValueError("item assessment reference differs from its completed outcome")
            if purpose == "holding" and item.action in {"buy", "no_action"}:
                raise ValueError("holding scope cannot carry buy or no_action")
            if purpose == "new" and item.action in {"hold", "sell"}:
                raise ValueError("new scope cannot carry hold or sell")
            if item.action in {"hold", "sell"} and positions.get(item.ticker) != item.position:
                raise ValueError("item position differs from the saved current position")
            if item.action in {"buy", "sell"} and item.terms is not None:
                self._validate_item_terms(item)
                if item.action == "buy":
                    buy_notional += item.terms.notional_yen
        if buy_notional > self.inputs.available_cash_yen:
            raise ValueError("combined buy notional cannot exceed available cash")
        liquidity_tickers = [context.ticker for context in self.liquidity_context]
        if len(set(liquidity_tickers)) != len(liquidity_tickers):
            raise ValueError("liquidity context tickers must be unique")
        if not set(liquidity_tickers) <= set(outcomes):
            raise ValueError("liquidity context must belong to the fixed research scope")
        return self

    def _validate_item_terms(self, item: BuyItem | SellItem) -> None:
        terms = item.terms
        assert terms is not None
        if terms.quantity % self.inputs.board_lot != 0:
            raise ValueError("terms quantity must be a board-lot multiple")
        if terms.expires_at <= self.prepared_at:
            raise ValueError("terms expiry must be after prepared_at")
        classification = next(
            (row for row in self.inputs.security_classification if row.ticker == item.ticker), None
        )
        if classification is None:
            raise ValueError("terms require saved security classification")
        if not common_stock_limit_is_valid(
            terms.limit_price_yen,
            target_session=terms.target_session,
            market_segment=classification.market_segment,
            sector_33=classification.sector_33,
        ):
            raise ValueError("limit price is outside the common stock grid rule")
        quote = next((row for row in self.inputs.quotes if row.ticker == item.ticker), None)
        if quote is None:
            raise ValueError("terms require a saved actual quote")
        if item.action == "buy":
            if terms.valuation_as_of != self.as_of:
                raise ValueError("buy terms require valuation based on proposal as_of")
            if item.timing == "wait_limit" and terms.limit_price_yen >= quote.price_yen:
                raise ValueError("wait_limit must be below the actual quote")
        if item.action == "sell" or item.timing == "at_current_limit":
            current_limit = floor_common_stock_limit(
                quote.price_yen,
                target_session=terms.target_session,
                market_segment=classification.market_segment,
                sector_33=classification.sector_33,
            )
            if terms.limit_price_yen != current_limit:
                raise ValueError("current-limit terms must use the actual close upper bound grid")
        if (
            item.action == "sell"
            and item.reason_code == "insufficient_remaining_reward"
            and terms.valuation_as_of != self.as_of
        ):
            raise ValueError("insufficient reward sell requires current valuation basis")

    def payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def proposal_draft_sha256(proposal: InvestmentProposal) -> str:
    """Reviewと発行時刻以外の、入力/条件/warning/固定scope全体をhashする。"""

    payload = proposal.model_dump(mode="json", exclude={"review", "published_at"})
    return sha256(canonical_json(payload).encode()).hexdigest()


def order_readiness(item: ProposalItem) -> Literal["ready", "needs_terms", "not_applicable"]:
    """保存条件の完成度だけを投影し、発注・受理・約定を推定しない。"""

    if item.action in {"buy", "sell"}:
        return "ready" if item.terms is not None else "needs_terms"
    return "not_applicable"
