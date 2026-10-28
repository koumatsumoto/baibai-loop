"""FinancialSnapshot保存型、TTM計算入力、配当basis。取得・保存・Screening設定に依存しない。"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.dataclasses import dataclass

from baibai_engine.market.metric_quality import OperatingProfitSource, TTMQuality
from baibai_engine.market.price_models import validate_finite

_MODEL_CONFIG = ConfigDict(strict=True, arbitrary_types_allowed=False, validate_assignment=False)
UNRESOLVED_DIVIDEND_BASIS = "unresolved_split_basis"
UNRESOLVED_ACTUAL_DIVIDEND_BASIS = "unresolved_actual_dividend"


class TTMRules(BaseModel):
    """変則決算や前年期間のずれを区別し、別期間の利益をTTMへ混ぜない。

    算術の期間照合であり、購入の要求期間ではない。
    docs/reference/company-valuation.md
    """

    model_config = ConfigDict(frozen=True, strict=True)

    full_year_min_days: int = Field(ge=1)
    full_year_max_days: int = Field(ge=1)
    period_length_tolerance_ratio: float = Field(ge=0)
    period_end_tolerance_days: int = Field(ge=0)


DEFAULT_TTM_RULES = TTMRules(
    full_year_min_days=330,
    full_year_max_days=370,
    period_length_tolerance_ratio=0.1,
    period_end_tolerance_days=45,
)


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
    # 比較可能な直近実績の年間 DPS (原本値)・進行期の予想年間 DPS・carry 用配当利回り。
    # dividend_yield は将来 carry なので明示 0 を含む非負の予想 DPS を優先する
    # (forecast_annual)。ただし正の実績 DPS の 2 倍を超える予想は実績へ倒す。
    # 予想不使用時は、会計期間から asof まで単位変更がない年間実績と明示 0 を追加の
    # 支払別再計算なしで採用する (actual_reported)。期間内に分割・併合がある正の実績は
    # 原本の基準を特定できないため未解決 (unresolved_split_basis)。独立して利用できる
    # 予想があれば使い、無ければ利回りと E[r] を出さない。
    # dividend_split_factor は配当計算窓に調整があれば累積 factor を記録する。
    # 相殺して 1 でも記録する。変更なし・factor 不明は None。値だけでは解決状態を表さない。
    # 倍率/警告の元数値を同じsource traceと保持する。EPSはas-of株式basis。
    forecast_eps_yen: float | None = None
    forecast_profit_yen: float | None = None
    forecast_ordinary_profit_yen: float | None = None
    earnings_forecast_source: dict[str, str | None] | None = None
    dividend_forecast_source: dict[str, str | None] | None = None
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
    # Reinvestment用。最新のcomplete actual rowのTAとEqARの積であり、PBRのBPS fallbackは使わない。
    same_state_equity_yen: float | None = None
    # 最後の raw close を as-of の株式基準へ換算した screening 参考価格。時価総額・E[r]・
    # Stock Analysis表示は同じ基準の株数と組み合わせる。約定価格ではなく、
    # Research の価格 fact は SQLite の raw/unadjusted close を取得する。
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
    edinet_snapshot_asof: date | None = None
    edinet_snapshot_recorded_at_utc: str | None = None
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
    # accruals_to_assets = (profit_ttm - cfo_ttm) / average total assets — Sloan
    # 1996. High positive accruals are an earnings-quality flag (reported NI
    # not converting to cash). Prior assets are optional; without them use current assets.
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
        "same_state_equity_yen",
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
        return validate_finite(value)

    @field_validator("investment_securities")
    @classmethod
    def _nonnegative_investment_securities(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError("investment_securities must be nonnegative")
        return value
