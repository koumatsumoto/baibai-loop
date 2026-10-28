"""保存済み観測の責務別読取。取得・公開は行わない。"""

from __future__ import annotations
from datetime import date
from pydantic.dataclasses import dataclass
from baibai_engine.machine_analysis.observations.prices.price_models import MODEL_CONFIG


@dataclass(frozen=True, slots=True, config=MODEL_CONFIG)
class JQuantsWeeklyMargin:
    """One ticker's margin balances as of one weekly balance date.

    ``issue_type`` says whether a short balance is possible at all: a 信用銘柄
    carries no stock lending, so its zero short balance describes the instrument
    rather than positioning. Standard and negotiable balances are separate
    because only the standard side carries a six-month settlement deadline.
    """

    ticker: str
    week_end: date
    long_vol: float | None = None
    short_vol: float | None = None
    long_std_vol: float | None = None
    long_neg_vol: float | None = None
    short_std_vol: float | None = None
    short_neg_vol: float | None = None
    issue_type: str | None = None


@dataclass(frozen=True, slots=True, config=MODEL_CONFIG)
class JQuantsAllIssuesDailyMargin:
    """One ticker's post-transition all-issues balance for one business day."""

    ticker: str
    balance_date: date
    long_vol: float | None = None
    short_vol: float | None = None
    long_std_vol: float | None = None
    long_neg_vol: float | None = None
    short_std_vol: float | None = None
    short_neg_vol: float | None = None
    issue_type: str | None = None
    publication_date: date | None = None


@dataclass(frozen=True, slots=True, config=MODEL_CONFIG)
class JQuantsMarginAlert:
    """Daily balance and regulation facts for an issue selected for daily publication.

    ``applied_date`` stores provider AppDate: the balance's application date (申込日),
    not the date when a regulation takes effect.
    """

    publication_date: date
    ticker: str
    applied_date: date
    publication_reason: str | None = None
    short_outstanding: float | None = None
    short_change: float | None = None
    short_ratio: float | None = None
    long_outstanding: float | None = None
    long_change: float | None = None
    long_ratio: float | None = None
    short_long_ratio: float | None = None
    short_negotiable_outstanding: float | None = None
    short_negotiable_change: float | None = None
    short_standard_outstanding: float | None = None
    short_standard_change: float | None = None
    long_negotiable_outstanding: float | None = None
    long_negotiable_change: float | None = None
    long_standard_outstanding: float | None = None
    long_standard_change: float | None = None
    tse_margin_regulation_classification: str | None = None


@dataclass(frozen=True, slots=True, config=MODEL_CONFIG)
class JQuantsShortSaleReport:
    """One reporter's disclosed short-position state for one ticker."""

    disclosed_at: date
    calculated_at: date
    ticker: str
    short_seller_name: str
    discretionary_investment_contractor_name: str
    investment_fund_name: str
    short_ratio: float | None
    short_shares: int | None = None
    short_trading_units: int | None = None
    previous_reported_at: date | None = None
    previous_short_ratio: float | None = None
    short_seller_address: str | None = None
    discretionary_investment_contractor_address: str | None = None
    notes: str | None = None
    source_ordinal: int = 0
