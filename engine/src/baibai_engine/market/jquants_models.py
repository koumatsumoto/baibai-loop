"""J-Quants source facts; no Screening outputs or provider I/O."""

from __future__ import annotations

from datetime import date

from pydantic import field_validator
from pydantic.dataclasses import dataclass

from baibai_engine.market.bars import MODEL_CONFIG, validate_finite
from baibai_engine.market.ticker import normalize_ticker


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


@dataclass(frozen=True, slots=True, config=MODEL_CONFIG)
class JQuantsMarginAlert:
    """Daily balance and regulation facts for an issue selected for daily publication."""

    publication_date: date
    ticker: str
    applied_date: date | None = None
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
    is_cancellation: bool = False
    notes: str | None = None
    source_ordinal: int = 0


@dataclass(frozen=True, slots=True, config=MODEL_CONFIG)
class JQuantsFinancialSummary:
    """短信1行。保存fieldと計算後の指標を区別する。

    `eps_ttm`は開示期間の累計EPSであり、TTM合成済みの値ではない。
    このEPS自体を足し引きしてTTM合成しない。
    `shares_outstanding`は自己株式を含む発行済株式総数である。
    計算基準はdocs/reference/valuation-metrics.mdの
    「Trailing PER の算出」「資本の分母」に従う。
    """

    ticker: str
    disclosed_at: date
    forecast_eps: float | None = None
    # 期中累計 EPS。TTM ではない (上の docstring)。
    eps_ttm: float | None = None
    bps: float | None = None
    # 自己株式を含む発行済株式総数。市場が値付けする株数ではない (上の docstring)。
    shares_outstanding: float | None = None
    sales: float | None = None
    cfo: float | None = None
    cash_eq: float | None = None
    total_assets: float | None = None
    equity: float | None = None
    operating_profit: float | None = None
    ordinary_profit: float | None = None
    profit: float | None = None
    # 会社予想の当期純利益・経常利益。forecast_eps と同一予想期 (当期予想 or 翌期予想)
    # から採ったペアで、両方揃うときだけ純利益>経常の一時益 flag を機械判定できる。
    forecast_profit: float | None = None
    forecast_ordinary_profit: float | None = None
    fiscal_period: str | None = None
    fiscal_year_end: date | None = None
    period_start: date | None = None
    period_end: date | None = None
    dps_actual_annual: float | None = None
    dps_forecast_annual: float | None = None
    # 期末自己株式数。`shares_outstanding` は自己株式を含む発行済株式総数なので、時価総額の
    # 分母にはこれを引いた株数を使う。自己株式は議決権も配当請求権も持たないため、含めると
    # 時価総額が過大になり、現金比率・利回りが薄く見える。
    treasury_shares: float | None = None
    # 開示された自己資本比率。`equity` は非支配株主持分を含む純資産なので、`equity / total_assets`
    # は自己資本比率にならない。導出でなく開示値を持つのは、自己資本を別途持たずに済むため。
    equity_to_asset_ratio: float | None = None
    # 支払ごとの 1 株当たり配当と、通期に支払った配当の総額 (円)。`dps_actual_annual` は
    # 中間・期末それぞれの基準日時点の株式基準で記載されるため、分割・併合を跨いだ年度は
    # 株価と基準が揃わない。支払ごとに持てば各支払の基準日より後の調整だけを掛けられ、
    # 総額は株式基準を持たないのでその換算の独立した照合になる。
    dividend_q1: float | None = None
    dividend_interim: float | None = None
    dividend_q3: float | None = None
    dividend_year_end: float | None = None
    dividend_total_annual: float | None = None
    # 期中平均株式数。提出者が EPS を出すのに使った株数で、期末発行済から自己株を引いた
    # 株数が壊れていないかを同じ行の中で照合するのに使う。
    average_shares: float | None = None

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker_field(cls, value: str) -> str:
        return normalize_ticker(value)

    @field_validator(
        "forecast_eps",
        "eps_ttm",
        "bps",
        "shares_outstanding",
        "sales",
        "cfo",
        "cash_eq",
        "total_assets",
        "equity",
        "operating_profit",
        "ordinary_profit",
        "profit",
        "forecast_profit",
        "forecast_ordinary_profit",
        "dps_actual_annual",
        "dps_forecast_annual",
        "treasury_shares",
        "equity_to_asset_ratio",
        "dividend_q1",
        "dividend_interim",
        "dividend_q3",
        "dividend_year_end",
        "dividend_total_annual",
        "average_shares",
    )
    @classmethod
    def _finite_numeric_fields(cls, value: float | None) -> float | None:
        return validate_finite(value)
