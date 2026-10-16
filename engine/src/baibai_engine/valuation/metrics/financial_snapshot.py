"""財務snapshotの生成本体。対象銘柄・母集団・設定読込はcallerが所有する。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from baibai_engine.market.bars import JQuantsAdjustmentFactorEvent, JQuantsDailyBar
from baibai_engine.market.edinet_models import EdinetMetricRecord
from baibai_engine.market.financial_documents import (
    forecast_source_trace,
    latest_forecast_source,
    project_financial_documents,
)
from baibai_engine.market.jquants_models import JQuantsFinancialSummary
from baibai_engine.market.metric_quality import OperatingProfitSource, TTMQuality

from .capital import (
    ENTITY_SCALE_MISMATCH,
    _common_equity_yen,
    _edinet_passes_entity_scale_check,
    _normalize_summaries_with_status,
    _resolve_capital_basis,
    _shares_excluding_treasury,
)
from .dividends import _resolve_dividend_carry
from .models import FinancialSnapshot, TTMRules
from .periods import (
    _accounting_observation_key,
    _carry_forward,
    _latest_actual_row,
    _latest_actual_row_by_field_priority,
    _latest_complete_row,
    _latest_non_null_row,
    _latest_summary,
    _prior_year_summary,
    _ttm_value,
)
from .profit import (
    _accruals_to_assets,
    _loss_narrowing,
    _select_operating_profit,
)
from .ratios import _safe_positive_ratio, _safe_ratio, _yoy_ratio

VALUATION_CALCULATION_REVISION = "statement-period-basis-v38"


def build_financial_snapshot(
    latest_price: float,
    summaries: Sequence[JQuantsFinancialSummary],
    edinet: EdinetMetricRecord | None,
    ttm_rules: TTMRules,
    *,
    adjustment_events: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    asof_date: date,
) -> FinancialSnapshot:
    normalization = _normalize_summaries_with_status(
        project_financial_documents(summaries, asof_date),
        adjustment_events,
        asof_date,
    )
    summaries = normalization.summaries
    capital_basis_barrier = normalization.capital_basis_barrier
    latest = _latest_summary(summaries)
    forecast = latest_forecast_source(summaries)
    forecast_eps = forecast.forecast_eps if forecast is not None else None
    # 同じ予想期の純利益>経常利益はdata-quality注記。原因・持続性は一次開示で確認する。
    # 純利益/経常は同じ予想期・basis・開示候補から投影する分割不変の絶対額なので、
    # 両方揃うときだけ比較する。flag は warning で per_forward / E[r] / rank を変えない。
    forecast_profit = forecast.forecast_profit if forecast is not None else None
    forecast_ordinary_profit = forecast.forecast_ordinary_profit if forecast is not None else None
    forecast_special_gain_flag = (
        forecast_profit is not None
        and forecast_ordinary_profit is not None
        and forecast_profit > forecast_ordinary_profit
    )
    # 予想経常利益または予想純利益の赤字を注記する。
    # flagだけでは予想EPSの符号や採用FV anchorは確定しない。
    # 未開示と赤字の確認を区別する。
    forecast_full_year_loss_flag = (forecast_profit is not None and forecast_profit < 0) or (
        forecast_ordinary_profit is not None and forecast_ordinary_profit < 0
    )
    # 開示の利益は期中累計で、年度途中の四半期開示では 12 か月分にならない (Q1 開示だと
    # 3 か月分)。sales / cfo と同じ rolling 合成 (直近累計 + 前期通期 - 前年同期間累計) で
    # TTM に直し、通期開示のときだけそのまま使う。合成できない場合は per_trailing を
    # 出さない (単一四半期の利益で割った偽の割高 PER を作らない)。
    # 合成は 1 株当たりでなく円で行う。1 株当たりの各項は自分の期の株数で割られており、
    # 株数が動いた会社では和・差が成立しない (新株発行で株数が倍になった期を跨ぐと、
    # 黒字の会社が赤字に見える)。1 株当たりへの換算は最後に 1 回だけ行う。
    eps_row = _latest_actual_row(summaries, field_names=("eps_ttm",))
    eps_prior = _prior_year_summary(summaries, ttm_rules, field_name="eps_ttm")
    sales_row = _latest_actual_row(summaries, field_names=("sales",))
    sales_prior = _prior_year_summary(summaries, ttm_rules, field_name="sales")
    cfo_row = _latest_actual_row(summaries, field_names=("cfo",))
    cfo_prior = _prior_year_summary(summaries, ttm_rules, field_name="cfo")
    eps_cumulative = eps_row.eps_ttm if eps_row else None
    profit_ttm, profit_quality = _ttm_value(summaries, "profit", ttm_rules)
    # BS 系 fact (bps / cash_eq / equity / total_assets / 株数) は四半期開示に
    # 載らないことが多く (bps 非 null は FY 開示 ~69% に対し四半期 ~17-20%)、
    # latest 行だけを見ると四半期行が最新になる断面で PBR 等が季節的に大量欠損
    # する。直近の非 null 行から carry-forward し (値は asof-basis 正規化済み)、
    # どの field をいつの開示から引いたかを staleness fact として残す。

    cash_eq, cash_eq_lag = _carry_forward(summaries, "cash_eq", latest)
    total_assets, total_assets_lag = _carry_forward(summaries, "total_assets", latest)
    equity_to_asset_ratio, eq_ratio_lag = _carry_forward(summaries, "equity_to_asset_ratio", latest)
    bps, bps_lag = _carry_forward(summaries, "bps", latest)
    carried_lags = {
        "bps": bps_lag,
        "cash_eq": cash_eq_lag,
        "total_assets": total_assets_lag,
        "equity_to_asset_ratio": eq_ratio_lag,
    }
    bs_carry_forward_fields = ",".join(
        sorted(name for name, lag in carried_lags.items() if lag is not None and lag > 0)
    )
    bs_carry_forward_lag_days = max(
        (lag for lag in carried_lags.values() if lag is not None), default=None
    )
    # 配当側で原本実績の比較可能性を判断する。資本の換算値を DPS へ流用しない。
    # 独立して使える現行予想を優先し、受理した実績または未解決を snapshot へ渡す。
    dividend = _resolve_dividend_carry(summaries, adjustment_events, latest_price, asof_date)
    dps_actual_annual = dividend.dps_actual_annual
    dps_forecast_annual = dividend.dps_forecast_annual
    dividend_yield = dividend.dividend_yield
    per_forward = (latest_price / forecast_eps) if forecast_eps and forecast_eps > 0 else None
    operating_row = _latest_actual_row_by_field_priority(
        summaries,
        ("operating_profit", "ordinary_profit", "profit"),
    )
    operating_profit, operating_profit_source = _select_operating_profit(operating_row)
    operating_field = {
        OperatingProfitSource.OPERATING_PROFIT: "operating_profit",
        OperatingProfitSource.ORDINARY_PROFIT: "ordinary_profit",
        OperatingProfitSource.PROFIT: "profit",
    }.get(operating_profit_source)
    operating_prior_row = (
        _prior_year_summary(summaries, ttm_rules, field_name=operating_field)
        if operating_field is not None
        else None
    )
    operating_profit_prior_year = (
        getattr(operating_prior_row, operating_field)
        if operating_prior_row is not None and operating_field is not None
        else None
    )
    capital_basis = _resolve_capital_basis(
        summaries,
        capital_basis_barrier=capital_basis_barrier,
    )
    shares_outstanding = capital_basis.issued
    # 時価総額の分母は自己株式を除いた株数である。自己株式は議決権も配当請求権も持たない
    # ので、含めると時価総額が過大になり現金比率・利回りが薄く、倍率が割高に見える。歪みが
    # 最大になるのは自己株式を積み上げた企業である。自己株式数が観測できない行は
    # 時価総額を出さない — 発行済で
    # 代用すると、どれだけ過大かが分からない値が現金比率・利回り・時価総額 gate へ入る。
    shares_ex_treasury = capital_basis.shares_ex_treasury
    operating_profit_ttm, _ = _ttm_value(summaries, "operating_profit", ttm_rules)
    sales_ttm, sales_quality = _ttm_value(summaries, "sales", ttm_rules)
    ocf_ttm, ocf_quality = _ttm_value(summaries, "cfo", ttm_rules)
    # EDINET の値は 1 つの書類を連結・単体のどちらかの基準で読んだもので、時価総額と
    # TTM 系列は短信由来である。連結財務諸表を持つ会社の書類を単体基準で読むと、比率の
    # 分子と分母の範囲がずれ得る。総資産の倍率で大きな規模差を検出するが、
    # 通過しても同一時点・連結範囲の一致を証明するものではない。
    edinet_usable = _edinet_passes_entity_scale_check(edinet, total_assets)
    edinet_metrics = edinet if edinet_usable else None
    edinet_ocf_ttm = edinet_metrics.ocf_ttm if edinet_metrics else None
    debt = edinet_metrics.debt if edinet_metrics else None
    cash = edinet_metrics.cash if edinet_metrics else None
    ebitda_ttm = edinet_metrics.ebitda_ttm if edinet_metrics else None
    fcf_ttm = edinet_metrics.fcf_ttm if edinet_metrics else None
    net_cash = edinet_metrics.net_cash if edinet_metrics else None
    investment_securities = edinet_metrics.investment_securities if edinet_metrics else None
    edinet_failure_reasons = (
        ",".join((*edinet.failure_reasons, *(() if edinet_usable else (ENTITY_SCALE_MISMATCH,))))
        if edinet
        else None
    )
    if net_cash is None and cash is not None and debt is not None:
        net_cash = cash - debt
    latest_market_cap = (latest_price * shares_ex_treasury) if shares_ex_treasury else None
    latest_enterprise_value = (
        (latest_market_cap + debt - cash)
        if latest_market_cap is not None and debt is not None and cash is not None
        else None
    )
    ev_ebitda = _safe_positive_ratio(latest_enterprise_value, ebitda_ttm)
    # 収益倍率も p_s / pcfr / ev_ebitda と同じ「時価総額 ÷ 円の TTM 系列」で組む。
    # 1 株当たりへの換算はここで 1 回だけ行い、市場が値付けできる株数で割る。こうすると
    # `株価 / eps == per_trailing` が厳密に成立し、同じ語が 2 つの値を指さない。
    per_trailing = _safe_positive_ratio(latest_market_cap, profit_ttm)
    eps_ttm = _safe_ratio(profit_ttm, shares_ex_treasury)
    # 同じ開示行のTAとEqARから円経路を作り、普通株BPS経路との照合と鮮度で選ぶ。
    # 別開示日の値を掛け合わせたり、carry後の差だけで古いBPSへ戻したりしない。
    # 行内の近さは経路選択の条件であり、差の経済的な原因を確定するものではない。
    common_equity_row = _latest_complete_row(summaries, ("total_assets", "equity_to_asset_ratio"))
    same_state_equity_yen = None
    if common_equity_row is not None:
        assert common_equity_row.total_assets is not None
        assert common_equity_row.equity_to_asset_ratio is not None
        same_state_equity_yen = (
            common_equity_row.total_assets * common_equity_row.equity_to_asset_ratio
        )
    bps_row = _latest_non_null_row(summaries, "bps")
    # 同一状態の円経路へ直しても、その組がより新しい BPS より古ければstaleな資本を
    # 復活させる。2経路のうち新しい観測を先に選び、同日または円経路が新しい場合だけ
    # 下の普通株basis一致判定へ進める。
    use_yen_route = common_equity_row is not None and (
        bps_row is None
        or _accounting_observation_key(common_equity_row) >= _accounting_observation_key(bps_row)
    )
    equity_yen = _common_equity_yen(
        summaries,
        total_assets=(
            common_equity_row.total_assets
            if common_equity_row is not None and use_yen_route
            else None
        ),
        equity_to_asset_ratio=(
            common_equity_row.equity_to_asset_ratio
            if common_equity_row is not None and use_yen_route
            else None
        ),
        bps=bps,
        shares_ex_treasury=shares_ex_treasury,
    )
    pbr = _safe_positive_ratio(latest_market_cap, equity_yen)
    prior_total_assets_row = _prior_year_summary(
        summaries,
        ttm_rules,
        field_name="total_assets",
    )
    accruals_to_assets = _accruals_to_assets(
        net_income=profit_ttm,
        ocf_ttm=ocf_ttm,
        total_assets=total_assets,
        prior_total_assets=(
            prior_total_assets_row.total_assets if prior_total_assets_row is not None else None
        ),
    )
    shares_prior = _prior_year_summary(summaries, ttm_rules, field_name="shares_outstanding")
    net_share_change_yoy = _yoy_ratio(
        shares_outstanding,
        shares_prior.shares_outstanding if shares_prior else None,
    )
    # 同じ前年行を、自己株式を除いた株数で測り直したもの。日本の自社株買いは取得した株式を
    # 自己株式へ入れるだけで発行済株式総数を減らさないので、上の量が動くのは主に消却年で
    # あって取得年ではない。当時の比較は既存 carry を維持する結論だった
    # (reports/studies/2026-08-17-tradable-share-change/)。ここでは両方の fact を出し、
    # carry の計算は変えない。
    tradable_share_change_yoy = _yoy_ratio(
        shares_ex_treasury,
        _shares_excluding_treasury(
            shares_prior.shares_outstanding if shares_prior else None,
            shares_prior.treasury_shares if shares_prior else None,
        ),
    )
    return FinancialSnapshot(
        latest_financial_disclosure_date=latest.disclosed_at if latest else None,
        per_forward=per_forward,
        earnings_forecast_source=forecast_source_trace(forecast),
        dividend_forecast_source=forecast_source_trace(
            latest_forecast_source(summaries, dividend=True), dividend=True
        ),
        per_trailing=per_trailing,
        pbr=pbr,
        ev_ebitda=ev_ebitda,
        p_s=_safe_ratio(latest_market_cap, sales_ttm),
        pcfr=_safe_ratio(latest_market_cap, ocf_ttm if ocf_ttm and ocf_ttm > 0 else None),
        eps=eps_ttm,
        dps_actual_annual=dps_actual_annual,
        dps_forecast_annual=dps_forecast_annual,
        dividend_yield=dividend_yield,
        dividend_basis=dividend.basis,
        dividend_split_factor=dividend.split_factor,
        sales_ttm=sales_ttm,
        ocf_ttm=ocf_ttm,
        edinet_ocf_ttm=edinet_ocf_ttm,
        sales=sales_row.sales if sales_row else None,
        cfo=cfo_row.cfo if cfo_row else None,
        cash_eq=cash_eq,
        total_assets=total_assets,
        same_state_equity_yen=same_state_equity_yen,
        market_price_yen=latest_price,
        shares_ex_treasury=shares_ex_treasury,
        capital_basis_failure_reason=capital_basis.failure_reason,
        market_cap=latest_market_cap,
        cash_to_market_cap=_safe_ratio(cash_eq, latest_market_cap),
        # 開示された自己資本比率をそのまま使う。`equity` は非支配株主持分を含む純資産なので
        # `equity / total_assets` は自己資本比率にならない。比率が観測できないときは None へ
        # 落とし、純資産比率で代用しない。非支配株主持分が大きい銘柄ほど過大評価になる。
        equity_ratio=equity_to_asset_ratio,
        ocf_yield=_safe_ratio(ocf_ttm, latest_market_cap),
        net_cash=net_cash,
        net_cash_to_market_cap=_safe_ratio(net_cash, latest_market_cap),
        investment_securities=investment_securities,
        asset_backed_ratio=_safe_ratio(
            (
                net_cash + investment_securities
                if net_cash is not None and investment_securities is not None
                else None
            ),
            latest_market_cap,
        ),
        fcf_ttm=fcf_ttm,
        fcf_yield=_safe_ratio(fcf_ttm, latest_market_cap),
        capex_ttm=edinet_metrics.capex_ttm if edinet_metrics else None,
        depreciation_and_amortization_ttm=(
            edinet_metrics.depreciation_and_amortization_ttm if edinet_metrics else None
        ),
        debt=debt,
        cash=cash,
        ebitda_ttm=ebitda_ttm,
        consolidation_basis=edinet.consolidation_basis if edinet else None,
        edinet_source_doc_id=edinet.source_doc_id if edinet else None,
        edinet_document_type=edinet.document_type if edinet else None,
        edinet_source_submit_datetime=edinet.source_submit_datetime if edinet else None,
        edinet_source_period_start=edinet.source_period_start if edinet else None,
        edinet_source_period_end=edinet.source_period_end if edinet else None,
        edinet_capex_source=edinet.capex_source if edinet else None,
        edinet_failure_reasons=edinet_failure_reasons or None,
        operating_profit=operating_profit,
        operating_profit_ttm=operating_profit_ttm,
        operating_profit_source=operating_profit_source,
        eps_yoy=_yoy_ratio(eps_cumulative, eps_prior.eps_ttm if eps_prior else None),
        sales_yoy=_yoy_ratio(
            sales_row.sales if sales_row else None,
            sales_prior.sales if sales_prior else None,
        ),
        operating_profit_yoy=_yoy_ratio(operating_profit, operating_profit_prior_year),
        cfo_yoy=_yoy_ratio(
            cfo_row.cfo if cfo_row else None,
            cfo_prior.cfo if cfo_prior else None,
        ),
        operating_profit_loss_narrowing=_loss_narrowing(
            operating_profit,
            operating_profit_prior_year,
        ),
        ttm_quality_ev_ebitda=(edinet_metrics.ttm_quality_ev_ebitda if edinet_metrics else None),
        ttm_quality_per_trailing=profit_quality,
        ttm_quality_p_s=sales_quality,
        ttm_quality_pcfr=ocf_quality,
        ttm_quality_ocf_yield=ocf_quality,
        ttm_quality_sales=sales_quality,
        ttm_quality_fcf_yield=(edinet_metrics.ttm_quality_fcf if edinet_metrics else None),
        ttm_quality_net_cash=(
            edinet_metrics.ttm_quality_net_cash if edinet_metrics else TTMQuality.UNAVAILABLE
        ),
        shares_outstanding=shares_outstanding,
        accruals_to_assets=accruals_to_assets,
        net_share_change_yoy=net_share_change_yoy,
        tradable_share_change_yoy=tradable_share_change_yoy,
        bs_carry_forward_fields=bs_carry_forward_fields or None,
        bs_carry_forward_lag_days=bs_carry_forward_lag_days,
        forecast_special_gain_flag=forecast_special_gain_flag,
        forecast_full_year_loss_flag=forecast_full_year_loss_flag,
    )
