"""財務snapshotの生成本体。対象銘柄・母集団・設定読込はcallerが所有する。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from baibai_engine.machine_analysis.models.company_financials.metrics.capital import (
    ENTITY_SCALE_MISMATCH,
    _common_equity,
    _edinet_passes_entity_scale_check,
    _normalize_summaries_with_status,
    _resolve_capital_basis,
    _shares_excluding_treasury,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.dividends import (
    _resolve_dividend_carry,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    FinancialSnapshot,
    TTMRules,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.periods import (
    _accounting_observation_key,
    _carry_forward,
    _latest_actual_row,
    _latest_actual_row_by_field_priority,
    _latest_complete_row,
    _latest_non_null_row,
    _latest_summary,
    _prior_year_summary,
    _ttm_components,
    _ttm_period,
    _ttm_value,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.profit import (
    _accruals_to_assets,
    _select_operating_profit,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.ratios import (
    _safe_positive_ratio,
    _safe_ratio,
    _yoy_ratio,
)
from baibai_engine.machine_analysis.observations.financials.capital_action_pdf import CapitalAction
from baibai_engine.machine_analysis.observations.financials.edinet_models import EdinetMetricRecord
from baibai_engine.machine_analysis.observations.financials.financial_documents import (
    accounting_standard,
    comparable_reporting_basis,
    forecast_source_trace,
    latest_forecast_source,
    project_financial_documents,
    statement_basis,
)
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.financials.metric_quality import (
    TTMQuality,
)
from baibai_engine.machine_analysis.observations.financials.pdf_selection import (
    PdfFinancialSelection,
)
from baibai_engine.machine_analysis.observations.prices.price_models import (
    JQuantsAdjustmentFactorEvent,
    JQuantsDailyBar,
)

VALUATION_CALCULATION_REVISION = "continuous-annual-input-v51-partial-split-basis"


def build_financial_snapshot(
    latest_price: float | None,
    summaries: Sequence[JQuantsFinancialSummary],
    edinet: EdinetMetricRecord | None,
    ttm_rules: TTMRules,
    *,
    adjustment_events: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    asof_date: date,
    pdf_selection: PdfFinancialSelection | None = None,
    capital_actions: Sequence[CapitalAction] = (),
) -> FinancialSnapshot:
    if any(action.effective_date > asof_date for action in capital_actions):
        raise ValueError("capital action is after the model as-of date")
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
    forecast_profit = forecast.forecast_profit if forecast is not None else None
    forecast_ordinary_profit = forecast.forecast_ordinary_profit if forecast is not None else None
    forecasts = (forecast_profit, forecast_ordinary_profit)
    forecast_full_year_loss_flag = (
        True
        if any(value is not None and value < 0 for value in forecasts)
        else False
        if all(value is not None for value in forecasts)
        else None
    )
    # 期中累計は円額でTTM合成する。株数の異なるEPSは足せない。
    sales_row = _latest_actual_row(summaries, field_names=("sales",))
    cfo_row = _latest_actual_row(summaries, field_names=("cfo",))
    profit_ttm, profit_quality = _ttm_value(summaries, "profit", ttm_rules)
    selected_pdf = pdf_selection is not None and (
        pdf_selection.current is not None or pdf_selection.blocks_legacy_ttm
    )
    if selected_pdf:
        assert pdf_selection is not None
        profit_ttm = pdf_selection.amounts_yen.get("parent_profit")
        profit_quality = (
            TTMQuality.APPROXIMATED if profit_ttm is not None else TTMQuality.UNAVAILABLE
        )
    # 四半期に未開示のBS値は直近値を使い、経過日数を残す。

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
    per_forward = (
        latest_price / forecast_eps
        if latest_price is not None and forecast_eps is not None and forecast_eps > 0
        else None
    )
    operating_row = _latest_actual_row_by_field_priority(
        summaries,
        ("operating_profit", "ordinary_profit", "profit"),
    )
    operating_profit, operating_profit_source = _select_operating_profit(operating_row)
    capital_basis = _resolve_capital_basis(
        summaries,
        capital_basis_barrier=capital_basis_barrier,
        capital_actions=capital_actions,
    )
    if (
        capital_basis.failure_reason == "indeterminate_treasury_after_capital_action"
        and forecast is not None
        and any(action.effective_date > forecast.disclosed_at for action in capital_actions)
    ):
        # A pre-action per-share forecast is not automatically valid after a
        # new share issuance; retain the original document, not a synthetic EPS.
        forecast_eps = None
        per_forward = None
    shares_outstanding = capital_basis.issued
    # 自己株式が不明なとき、発行済株数で代用すると時価総額を過大評価する。
    shares_ex_treasury = capital_basis.shares_ex_treasury
    operating_profit_ttm, _ = _ttm_value(summaries, "operating_profit", ttm_rules)
    sales_ttm, sales_quality = _ttm_value(summaries, "sales", ttm_rules)
    if selected_pdf:
        assert pdf_selection is not None
        operating_profit_ttm = pdf_selection.amounts_yen.get("operating_profit")
        sales_ttm = pdf_selection.amounts_yen.get("sales")
        sales_quality = TTMQuality.APPROXIMATED if sales_ttm is not None else TTMQuality.UNAVAILABLE
    ocf_ttm, ocf_quality = _ttm_value(summaries, "cfo", ttm_rules)
    # EDINETと短信の大きな規模差を検出する。通過しても同一時点・範囲の証明にはならない。
    edinet_usable = _edinet_passes_entity_scale_check(edinet, total_assets)
    edinet_metrics = edinet if edinet_usable else None
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
    latest_market_cap = (
        latest_price * shares_ex_treasury
        if latest_price is not None and shares_ex_treasury
        else None
    )
    latest_enterprise_value = (
        (latest_market_cap + debt - cash)
        if latest_market_cap is not None and debt is not None and cash is not None
        else None
    )
    ev_ebitda = _safe_positive_ratio(latest_enterprise_value, ebitda_ttm)
    # EPSとPERは同じ自己株控除後の株数を使う。
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
    equity = _common_equity(
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
    book_equity: dict[str, object] | None = None
    if equity is not None:
        equity_yen, method = equity
        source = common_equity_row if method == "assets_times_equity_ratio" else bps_row
        assert source is not None
        book_equity = {
            "amount_yen": equity_yen,
            "method": method,
            "measurement_date": source.period_end,
            "statement_basis": statement_basis(source),
            "accounting_standard": accounting_standard(source),
            "disclosure_number": source.disclosure_number,
            "disclosed_at": source.disclosed_at,
            "disclosed_time": source.disclosed_time,
            "document_type": source.document_type,
            "operands": (
                {
                    "total_assets_yen": source.total_assets,
                    "equity_to_asset_ratio": source.equity_to_asset_ratio,
                }
                if method == "assets_times_equity_ratio"
                else {
                    "bps_yen": bps,
                    "shares_ex_treasury": shares_ex_treasury,
                    "share_basis_as_of": asof_date,
                }
            ),
        }
    pbr = _safe_positive_ratio(latest_market_cap, None if equity is None else equity[0])
    prior_total_assets_row = _prior_year_summary(
        summaries,
        ttm_rules,
        field_name="total_assets",
        require_basis=False,
    )
    current_assets_row = _latest_actual_row(summaries, field_names=("total_assets",))
    profit_rows = _ttm_components(summaries, "profit", ttm_rules)
    ocf_rows = _ttm_components(summaries, "cfo", ttm_rules)
    accrual_basis_rows = (
        *profit_rows,
        *ocf_rows,
        *((current_assets_row,) if current_assets_row is not None else ()),
    )
    prior_assets_comparable = prior_total_assets_row is not None and comparable_reporting_basis(
        (*accrual_basis_rows, prior_total_assets_row)
    )
    accruals_to_assets = _accruals_to_assets(
        net_income=(
            profit_ttm
            if profit_rows
            and ocf_rows
            and current_assets_row is not None
            and comparable_reporting_basis(accrual_basis_rows)
            else None
        ),
        ocf_ttm=ocf_ttm,
        total_assets=total_assets,
        prior_total_assets=(
            prior_total_assets_row.total_assets
            if prior_assets_comparable and prior_total_assets_row is not None
            else None
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
    # (docs/reference/estimate-calibration.md#carry-policy)。ここでは両方の fact を出し、
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
        forecast_eps_yen=forecast_eps,
        forecast_profit_yen=forecast_profit,
        forecast_ordinary_profit_yen=forecast_ordinary_profit,
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
        ocf_ttm_period=_ttm_period(summaries, "cfo", ttm_rules),
        sales=sales_row.sales if sales_row else None,
        cfo=cfo_row.cfo if cfo_row else None,
        cash_eq=cash_eq,
        total_assets=total_assets,
        same_state_equity_yen=same_state_equity_yen,
        book_equity=book_equity,
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
        fcf_yield=_safe_ratio(fcf_ttm, latest_market_cap),
        debt=debt,
        cash=cash,
        consolidation_basis=edinet.consolidation_basis if edinet else None,
        edinet_snapshot_asof=edinet.snapshot_asof if edinet else None,
        edinet_snapshot_recorded_at_utc=edinet.snapshot_recorded_at_utc if edinet else None,
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
        forecast_full_year_loss_flag=forecast_full_year_loss_flag,
    )
