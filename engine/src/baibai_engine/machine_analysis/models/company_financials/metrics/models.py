"""FinancialSnapshot保存型、TTM計算入力、配当basis。取得・保存・Screening設定に依存しない。"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field
from pydantic.dataclasses import dataclass

from baibai_engine.machine_analysis.observations.financials.metric_quality import (
    OperatingProfitSource,
    TTMQuality,
)

_MODEL_CONFIG = ConfigDict(
    strict=True, arbitrary_types_allowed=False, validate_assignment=False, allow_inf_nan=False
)
UNRESOLVED_DIVIDEND_BASIS = "unresolved_split_basis"
UNRESOLVED_ACTUAL_DIVIDEND_BASIS = "unresolved_actual_dividend"


class TTMRules(BaseModel):
    """変則決算や前年期間のずれを区別し、別期間の利益をTTMへ混ぜない。

    算術の期間照合であり、購入の要求期間ではない。
    docs/reference/model-observations.md
    """

    model_config = ConfigDict(frozen=True, strict=True)

    full_year_min_days: int = Field(ge=1)
    full_year_max_days: int = Field(ge=1)
    period_length_tolerance_ratio: float = Field(ge=0)
    period_end_tolerance_days: int = Field(ge=0)


DEFAULT_TTM_RULES = TTMRules(
    full_year_min_days=330,
    full_year_max_days=371,
    period_length_tolerance_ratio=0.1,
    period_end_tolerance_days=45,
)


# 多数のfieldを位置引数で渡すと追加時に値がずれる。
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
    sales_ttm: float | None
    ocf_ttm: float | None
    ocf_ttm_period: dict[str, object] | None = None
    # 倍率/警告の元数値を同じsource traceと保持する。EPSはas-of株式basis。
    forecast_eps_yen: float | None = None
    forecast_profit_yen: float | None = None
    forecast_ordinary_profit_yen: float | None = None
    earnings_forecast_source: dict[str, str | None] | None = None
    dividend_forecast_source: dict[str, str | None] | None = None
    # 実績の報告期・文書。最新予想年度や配当権利/支払日ではない。
    dividend_actual_source: dict[str, object] | None = None
    dps_actual_annual: float | None = None
    dps_forecast_annual: float | None = None
    # 予想や株価で補わず、実績額の株式単位・未解決を記録する。
    dividend_actual_basis: str = "unavailable"
    dividend_split_factor: float | None = None
    # 過去開示から補ったBS項目と最大経過日数。
    bs_carry_forward_fields: str | None = None
    bs_carry_forward_lag_days: int | None = None
    sales: float | None = None
    cfo: float | None = None
    # 主cfoが採った報告行。最新開示日やTTM/EDINETの期間で置き換えない。
    cfo_source: dict[str, str | None] | None = None
    cash_eq: float | None = None
    # 採用した短信の報告期・文書。最新開示日や他BS項目の最大carry日数とは別。
    cash_eq_source: dict[str, str | None] | None = None
    total_assets: float | None = None
    # 同じ実績開示の総資産と自己資本比率から得た額。PBRが採用する簿価とは限らない。
    same_state_equity_yen: float | None = None
    # PBRが実際に採った分母と元開示。EDINET残高のscopeや現在の純資産とは別に読む。
    book_equity: dict[str, object] | None = None
    # 基準日の株式単位にそろえた直近終値。倍率に使う価格で、発注・約定価格ではない。
    market_price_yen: float | None = None
    # 時価総額に使う自己株控除後の株数。自己株式が不明なら発行済総数で代用しない。
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
    investment_securities: float | None = Field(default=None, ge=0)
    fcf_yield: float | None = None
    debt: float | None = None
    cash: float | None = None
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
    # ttm_quality_* は「TTM 値の合成の質」であって値の有無ではない。分母が負・ゼロで
    # 比率 (per_trailing / pcfr 等) が None でも、合成に成功していれば exact のまま。
    ttm_quality_ev_ebitda: TTMQuality | None = None
    ttm_quality_per_trailing: TTMQuality = TTMQuality.UNAVAILABLE
    ttm_quality_p_s: TTMQuality = TTMQuality.UNAVAILABLE
    ttm_quality_pcfr: TTMQuality = TTMQuality.UNAVAILABLE
    ttm_quality_ocf_yield: TTMQuality = TTMQuality.UNAVAILABLE
    ttm_quality_sales: TTMQuality = TTMQuality.UNAVAILABLE
    ttm_quality_fcf_yield: TTMQuality | None = None
    shares_outstanding: float | None = None
    # accruals_to_assets = (profit_ttm - cfo_ttm) / average total assets — Sloan
    # 1996. High positive accruals are an earnings-quality flag (reported NI
    # not converting to cash). Prior assets are optional; without them use current assets.
    accruals_to_assets: float | None = None
    # 発行済株式の変化は自己株買いそのものを示さない。
    net_share_change_yoy: float | None = None
    # 自己株買いは発行済株数を減らさないため、控除後株数の変化を別に持つ。
    tradable_share_change_yoy: float | None = None
    # 会社予想の経常利益・純利益に基づく。EPSの符号から推定しない。
    forecast_full_year_loss_flag: bool | None = None
