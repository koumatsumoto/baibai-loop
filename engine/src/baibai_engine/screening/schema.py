from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from math import isfinite
from typing import Annotated, Any

from pydantic import ConfigDict, Field, field_validator, model_validator
from pydantic.dataclasses import dataclass

from baibai_engine.market.ticker import normalize_ticker as normalize_ticker

from .metric_quality import OperatingProfitSource as OperatingProfitSource
from .metric_quality import TTMQuality as TTMQuality

_MODEL_CONFIG = ConfigDict(
    strict=True,
    arbitrary_types_allowed=False,
    validate_assignment=False,
)
_TICKER_PATTERN = r"^[0-9A-Z]{4}$"

# 配当の株式基準が確定できないことを表す `dividend_basis` の値。年間 DPS は中間・期末
# それぞれの基準日時点の株式基準で記載されるので、会計期間に分割・併合が入り、かつ支払
# ごとの換算もできない年度はこの状態になる。無配 (`dividend_yield=0`) とも、観測できない
# (`unavailable`) とも別で、E[r] はこの行に順位を付けない。
UNRESOLVED_DIVIDEND_BASIS = "unresolved_split_basis"

# `sector_median_basis` の 2 値。どちらの母集団が中央値を出したかを表す。
SECTOR_MEDIAN_BASIS_SECTOR = "sector"
SECTOR_MEDIAN_BASIS_MARKET = "market"

type NullableFloatMap = Mapping[str, float | None]
type StringMap = Mapping[str, str]
type MetricValueMap = Mapping[str, float | int | bool | str | None]
type Ticker = Annotated[str, Field(pattern=_TICKER_PATTERN)]
type NonEmptyString = Annotated[str, Field(min_length=1)]
type NonNegativeInt = Annotated[int, Field(ge=0)]


def _validate_finite(value: float | None) -> float | None:
    if value is not None and not isfinite(value):
        raise ValueError("numeric values must be finite")
    return value


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class SecurityMaster:
    ticker: Ticker
    name: NonEmptyString
    market_segment: NonEmptyString
    sector_33: NonEmptyString
    is_common_stock: bool

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker(cls, value: str) -> str:
        return normalize_ticker(value)


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class UniverseSnapshot:
    market_cap_oku: int | None
    avg_turnover_oku: float | None
    listing_span_days: int | None = None
    jpx_flags: tuple[str, ...] = ()

    @field_validator("jpx_flags", mode="before")
    @classmethod
    def _tuple_jpx_flags(cls, value: Sequence[str]) -> tuple[str, ...]:
        return tuple(value)

    @field_validator("avg_turnover_oku")
    @classmethod
    def _finite_turnover(cls, value: float | None) -> float | None:
        return _validate_finite(value)


# field が 80 を超えるので kw_only にする。位置引数で組めると、先頭へ 1 つ足しただけで
# 呼び出し側の全引数が静かに 1 つずれる。
@dataclass(frozen=True, slots=True, kw_only=True, config=_MODEL_CONFIG)
class FinancialSnapshot:
    # 本 snapshot が読んだ最新開示の開示日。決算シーズンは「発表済みだが取込前」の窓が
    # 開くので、行の数字がどの開示までを含むかを判断面から読めるようにする。
    latest_financial_disclosure_date: date | None
    per_forward: float | None
    per_trailing: float | None
    pbr: float | None
    ev_ebitda: float | None
    p_s: float | None
    pcfr: float | None
    # TTM 純利益 ÷ 市場が値付けする株式数 (発行済 - 自己株)。提出者が開示する 1 株当たり
    # 当期純利益ではなく、時価総額と同じ資本分母で組み直した値である。こうすると
    # `market_price_yen / eps == per_trailing` が厳密に成立する。
    eps: float | None
    sales_ttm: float | None
    ocf_ttm: float | None
    edinet_ocf_ttm: float | None = None
    # 直近実績の年間 DPS (asof の株式基準)・進行期の予想年間 DPS・carry 用配当利回り。
    # dividend_yield は将来 carry なので予想 DPS を優先する (forecast_annual)。ただし正の
    # 実績 DPS の 2 倍を超える予想は実績へ倒す。予想を使えなければ実績を使い、会計期間に
    # 分割・併合が無ければ報告値をそのまま (actual_reported)、あれば
    # 支払ごとに基準日より後の調整を掛け直した値を使う (actual_record_date_resolved)。
    # 掛け直せない年度は利回りを出さず (unresolved_split_basis)、E[r] も付けない。
    # dividend_split_factor は会計期間に起きた累積 factor で、期間内に何も無ければ None。
    dps_actual_annual: float | None = None
    dps_forecast_annual: float | None = None
    dividend_yield: float | None = None
    dividend_basis: str | None = None
    dividend_split_factor: float | None = None
    # BS 系 fact (bps / cash_eq / equity / total_assets) の carry-forward 記録。
    # fields = latest 行に無く過去行から引いた field 名 (comma 区切り)、
    # lag_days = その最大遅延日数 (staleness fact)。
    bs_carry_forward_fields: str | None = None
    bs_carry_forward_lag_days: int | None = None
    sales: float | None = None
    cfo: float | None = None
    cash_eq: float | None = None
    total_assets: float | None = None
    # 最後の raw close を as-of の株式基準へ換算した screening 参考価格。時価総額・E[r]・
    # Review Set表示は同じ基準の株数と組み合わせる。約定価格ではなく、plan-limit は SQLite
    # の raw/unadjusted close を再取得する。
    market_price_yen: float | None = None
    # 市場が値付けする株式数 (発行済 - 自己株式)。valuation history も現在倍率と同じ
    # 資本分母で組み、自己株比率の変化ではなく価格変化だけを自己レンジへ反映する。
    shares_ex_treasury: float | None = None
    # 発行済と自己株式を安全に合成できなかった理由。None は通常の欠損も含み、ここには
    # 「値はあるが異なる資本状態に属する」と検出できた場合だけ理由を残す。
    capital_basis_failure_reason: str | None = None
    market_cap: float | None = None
    cash_to_market_cap: float | None = None
    equity_ratio: float | None = None
    ocf_yield: float | None = None
    net_cash: float | None = None
    net_cash_to_market_cap: float | None = None
    investment_securities: float | None = None
    asset_backed_ratio: float | None = None
    fcf_ttm: float | None = None
    fcf_yield: float | None = None
    capex_ttm: float | None = None
    depreciation_and_amortization_ttm: float | None = None
    debt: float | None = None
    cash: float | None = None
    ebitda_ttm: float | None = None
    consolidation_basis: str | None = None
    edinet_source_doc_id: str | None = None
    edinet_document_type: str | None = None
    edinet_source_submit_datetime: str | None = None
    edinet_source_period_start: date | None = None
    edinet_source_period_end: date | None = None
    edinet_capex_source: str | None = None
    edinet_failure_reasons: str | None = None
    operating_profit: float | None = None
    operating_profit_ttm: float | None = None
    operating_profit_source: OperatingProfitSource = OperatingProfitSource.NULL
    eps_yoy: float | None = None
    sales_yoy: float | None = None
    operating_profit_yoy: float | None = None
    cfo_yoy: float | None = None
    operating_profit_loss_narrowing: bool | None = None
    # ttm_quality_* は「TTM 値の合成の質」であって値の有無ではない。分母が負・ゼロで
    # 比率 (per_trailing / pcfr 等) が None でも、合成に成功していれば exact のまま。
    ttm_quality_ev_ebitda: TTMQuality | None = None
    ttm_quality_per_trailing: TTMQuality = TTMQuality.UNAVAILABLE
    ttm_quality_p_s: TTMQuality = TTMQuality.UNAVAILABLE
    ttm_quality_pcfr: TTMQuality = TTMQuality.UNAVAILABLE
    ttm_quality_ocf_yield: TTMQuality = TTMQuality.UNAVAILABLE
    ttm_quality_sales: TTMQuality = TTMQuality.UNAVAILABLE
    ttm_quality_fcf_yield: TTMQuality | None = None
    ttm_quality_net_cash: TTMQuality = TTMQuality.UNAVAILABLE
    shares_outstanding: float | None = None
    # accruals_to_assets = (eps_ttm * shares - cfo_ttm) / average total assets — Sloan
    # 1996. High positive accruals are an earnings-quality flag (reported NI
    # not converting to cash). None if any input is missing.
    accruals_to_assets: float | None = None
    # グロス発行済株式数の前年比。正は増加、負は減少。
    # 自己株買いの実行額や将来の買戻しを直接表すものではない。
    # 前年の株式数が欠損または0ならNone。
    net_share_change_yoy: float | None = None
    # 同じ前年比を自己株式控除後の株数で測ったもの。日本の自社株買いは取得株を自己株式へ
    # 入れるだけで発行済株式総数を減らさないので、上の量と別の年に動く。carry へは入れず、
    # 事前登録した比較の入力として持つだけである。自己株式数が観測できない行は答えない。
    tradable_share_change_yoy: float | None = None
    # 同じ予想期の純利益が経常利益を上回ることの注記。
    # 原因・持続性を確定せず、rankやE[r]を変更しない。
    forecast_special_gain_flag: bool = False
    # 予想経常利益または予想純利益の赤字を注記する。
    # 予想EPSとFV anchorへの影響は実際の入力に依存する。
    forecast_full_year_loss_flag: bool = False

    @field_validator(
        "per_forward",
        "per_trailing",
        "pbr",
        "ev_ebitda",
        "p_s",
        "pcfr",
        "eps",
        "dps_actual_annual",
        "dps_forecast_annual",
        "dividend_yield",
        "dividend_split_factor",
        "sales_ttm",
        "ocf_ttm",
        "edinet_ocf_ttm",
        "sales",
        "cfo",
        "cash_eq",
        "total_assets",
        "market_price_yen",
        "shares_ex_treasury",
        "market_cap",
        "cash_to_market_cap",
        "equity_ratio",
        "ocf_yield",
        "net_cash",
        "net_cash_to_market_cap",
        "investment_securities",
        "asset_backed_ratio",
        "fcf_ttm",
        "fcf_yield",
        "capex_ttm",
        "depreciation_and_amortization_ttm",
        "debt",
        "cash",
        "ebitda_ttm",
        "operating_profit",
        "operating_profit_ttm",
        "eps_yoy",
        "sales_yoy",
        "operating_profit_yoy",
        "cfo_yoy",
        "shares_outstanding",
        "accruals_to_assets",
        "net_share_change_yoy",
        "tradable_share_change_yoy",
    )
    @classmethod
    def _finite_numeric_fields(cls, value: float | None) -> float | None:
        return _validate_finite(value)

    @field_validator("investment_securities")
    @classmethod
    def _nonnegative_investment_securities(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError("investment_securities must be nonnegative")
        return value


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class DerivedMetrics:
    price_change_1d: float | None = None
    price_change_5d: float | None = None
    price_change_20d: float | None = None
    price_change_60d: float | None = None
    gap_from_52w_low: float | None = None
    turnover_spike_5d: float | None = None
    sector_relative_strength_4w: float | None = None
    sector_median_gap: NullableFloatMap = Field(default_factory=dict)
    # sector 中央値倍率の絶対値と自己レンジ (750 営業日) の中央値倍率。
    # 機械 E[r] / FV アンカーの入力 (gap / percentile と違い水準そのもの)。
    sector_median_value: NullableFloatMap = Field(default_factory=dict)
    # 上の 2 つがどの母集団から作られたかを軸ごとに記録する。母数が薄い業種では
    # 市場全体へ落ちるので、同じ field が「業種との差」と「市場との差」の 2 つの量を
    # 指す。落ちた業種は市場より低倍率に寄るため、素性が無いと gate を越えた根拠が
    # 業種比較なのか市場比較なのか読めない。値は `SECTOR_MEDIAN_BASIS_SECTOR` /
    # `SECTOR_MEDIAN_BASIS_MARKET` のいずれか。
    sector_median_basis: StringMap = Field(default_factory=dict)
    self_range_percentile: NullableFloatMap = Field(default_factory=dict)
    self_range_median: NullableFloatMap = Field(default_factory=dict)
    sigma_gap: NullableFloatMap = Field(default_factory=dict)
    sector_relative_strength_percentile: float | None = None
    ticker_return_4w: float | None = None
    sector_return_4w: float | None = None
    short_history_flag: bool = False
    split_adjustment_flag: bool = False
    price_history_sessions_750d: int | None = None
    price_history_coverage_750d: float | None = None
    # Supply/demand read from the exchange's published margin balances. Definitions
    # and the reason each one is shaped this way live in `margin_metrics`. The field
    # names the balance date behind the numbers, which is what makes their age
    # readable: through 2026-09-18 the balances are weekly and published days later,
    # so an asof in the middle of a week is looking at data up to nine days old by
    # construction; the daily series that replaces them narrows that to a day.
    margin_week_end: date | None = None
    margin_issue_type: str | None = None
    margin_long_to_adv: float | None = None
    margin_short_to_adv: float | None = None
    margin_long_share: float | None = None
    margin_long_delta_26w: float | None = None
    margin_std_long_share: float | None = None
    # Annualized 60-session realized volatility is a calibration control. It is
    # kept out of Security Analysis output until a separately tested decision use exists.
    realized_volatility_60d: float | None = None

    @field_validator(
        "price_change_1d",
        "price_change_5d",
        "price_change_20d",
        "price_change_60d",
        "gap_from_52w_low",
        "turnover_spike_5d",
        "sector_relative_strength_4w",
        "sector_relative_strength_percentile",
        "ticker_return_4w",
        "sector_return_4w",
        "price_history_coverage_750d",
        "margin_long_to_adv",
        "margin_short_to_adv",
        "margin_long_share",
        "margin_long_delta_26w",
        "margin_std_long_share",
        "realized_volatility_60d",
    )
    @classmethod
    def _finite_optional_float(cls, value: float | None) -> float | None:
        return _validate_finite(value)


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class FreshnessWarning:
    source_family: NonEmptyString
    stale_metric: NonEmptyString
    reason: NonEmptyString
    event_date: date
    event_kind: NonEmptyString
    event_title: NonEmptyString
    event_source: NonEmptyString
    edinet_source_submit_datetime: str | None = None
    event_url: str | None = None


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class SecurityAnalysis:
    ticker: Ticker
    name: NonEmptyString
    per_forward: float | None
    per_trailing: float | None
    pbr: float | None
    ev_ebitda: float | None
    p_s: float | None
    pcfr: float | None
    sector_33: NonEmptyString
    ttm_quality: Mapping[str, TTMQuality]
    market_cap_oku: int | None = None
    avg_turnover_oku: float | None = None
    listing_span_days: int | None = None
    jpx_flags: tuple[str, ...] = ()
    price_change_1d: float | None = None
    price_change_5d: float | None = None
    price_change_20d: float | None = None
    price_change_60d: float | None = None
    gap_from_52w_low: float | None = None
    turnover_spike_5d: float | None = None
    sector_relative_strength_percentile: float | None = None
    price_history_sessions_750d: int | None = None
    price_history_coverage_750d: float | None = None
    metrics: MetricValueMap = Field(default_factory=dict)
    next_earnings_date: date | None = None
    split_adjustment_flag: bool = False
    freshness_warnings: tuple[FreshnessWarning, ...] = ()

    @field_validator("freshness_warnings", "jpx_flags", mode="before")
    @classmethod
    def _tuple_sequence(cls, value: Sequence[Any]) -> tuple[Any, ...]:
        return tuple(value)

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker_field(cls, value: str) -> str:
        return normalize_ticker(value)

    @field_validator(
        "per_forward",
        "per_trailing",
        "pbr",
        "ev_ebitda",
        "p_s",
        "pcfr",
        "avg_turnover_oku",
        "price_change_1d",
        "price_change_5d",
        "price_change_20d",
        "price_change_60d",
        "gap_from_52w_low",
        "turnover_spike_5d",
        "sector_relative_strength_percentile",
        "price_history_coverage_750d",
    )
    @classmethod
    def _finite_optional_float(cls, value: float | None) -> float | None:
        return _validate_finite(value)


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class ScreeningRunDocument:
    run_date: date
    asof_date: date
    universe_size: NonNegativeInt
    filters: Mapping[str, Any]
    security_analyses: tuple[SecurityAnalysis, ...]
    run_at: datetime
    run_id: NonEmptyString
    screening_rules_hash: NonEmptyString
    er_model_version: NonEmptyString
    generated_by: str = "screening-cli-v1"
    data_sources: tuple[str, ...] = (
        "j-quants-light",
        "jpx-public-earnings-calendar",
        "jpx-public-regulation",
    )
    provider_status_lines: tuple[str, ...] = ()
    universe_exclusion_lines: tuple[str, ...] = ()
    ttm_quality_counts: Mapping[str, int] = Field(default_factory=dict)
    fallback_lines: tuple[str, ...] = ()

    @field_validator(
        "security_analyses",
        "data_sources",
        "provider_status_lines",
        "universe_exclusion_lines",
        "fallback_lines",
        mode="before",
    )
    @classmethod
    def _tuple_sequence(cls, value: Sequence[Any]) -> tuple[Any, ...]:
        return tuple(value)

    @model_validator(mode="after")
    def _consistent_dates(self) -> ScreeningRunDocument:
        if self.run_date != self.asof_date:
            raise ValueError("run_date must equal asof_date")
        if self.run_at.tzinfo is None:
            raise ValueError("run_at must be timezone-aware")
        return self
