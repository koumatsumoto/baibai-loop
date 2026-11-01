"""共有された財務・価格入力をモデルへ組み立てる。候補選択・取得・保存はしない。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, replace
from datetime import date

from baibai_engine.machine_analysis.generation.contracts import GeneratedModel
from baibai_engine.machine_analysis.models.company_financials.earnings import earnings_price
from baibai_engine.machine_analysis.models.company_financials.metrics.financial_snapshot import (
    VALUATION_CALCULATION_REVISION,
    build_financial_snapshot,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import TTMRules
from baibai_engine.machine_analysis.models.company_financials.reported_flows import (
    financial_observation_payload,
)
from baibai_engine.machine_analysis.models.company_growth.calculations import (
    build_reported_comparison,
)
from baibai_engine.machine_analysis.models.company_growth.generation import generate_company_growth
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
from baibai_engine.machine_analysis.models.technical_analysis.calculations import (
    TECHNICAL_CALCULATION_REVISION,
)
from baibai_engine.machine_analysis.models.technical_analysis.generation import (
    generate_technical_analysis,
)
from baibai_engine.machine_analysis.observations.financials.edinet_comparisons import (
    FilingComparisons,
)
from baibai_engine.machine_analysis.observations.financials.edinet_models import EdinetMetricRecord
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.financials.pdf_selection import select_pdf_financials
from baibai_engine.machine_analysis.observations.financials.pdf_statements import PdfStatement
from baibai_engine.machine_analysis.observations.prices.price_models import (
    JQuantsAdjustmentFactorEvent,
    PriceBar,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.primitives.stocks import normalize_ticker

_GROWTH_FIELDS = frozenset(
    {"eps_yoy", "sales_yoy", "operating_profit_yoy", "cfo_yoy", "operating_profit_loss_narrowing"}
)


def generate_financial_and_price_models(
    *,
    ticker: str,
    documents: Sequence[JQuantsFinancialSummary],
    adjusted_bars: Sequence[PriceBar],
    adjustment_events: Sequence[JQuantsAdjustmentFactorEvent],
    trading_dates: Sequence[date],
    expected_price_date: date | None,
    sector: str | None,
    as_of: date,
    input_release: InputRelease,
    ttm_rules: TTMRules,
    growth_start: date | None = None,
    edinet: EdinetMetricRecord | None = None,
    price_path: dict[str, object] | None = None,
    comparisons: Sequence[FilingComparisons] = (),
    pdf_statements: Sequence[PdfStatement] = (),
) -> tuple[GeneratedModel, ...]:
    """同じ個社入力から企業財務・企業成長・テクニカル分析を生成する。

    barsはas_ofと同じ調整基準。財務原本の全履歴を受け取り、企業成長の振り返りと
    企業財務の開示日cutoffは各ownerの意味を保つ。EDINETによる詳細調査はL3へ委ねる。
    市場環境・外部評価はそれぞれの共有読取から別途同じgenerationへ組み込む。
    """
    ticker = normalize_ticker(ticker)
    if any(row.ticker != ticker for row in documents) or any(
        event.ticker != ticker for event in adjustment_events
    ):
        raise ValueError("company model inputs contain a different ticker")
    if edinet is not None and edinet.ticker != ticker:
        raise ValueError("company model inputs contain a different ticker")
    bars = sorted(
        (bar for bar in adjusted_bars if bar.traded_at <= as_of), key=lambda bar: bar.traded_at
    )
    latest = bars[-1] if bars else None
    pdf_selection = select_pdf_financials(documents, pdf_statements, as_of=as_of)
    earnings = earnings_price(
        documents,
        adjusted_price=latest,
        adjustment_events=adjustment_events,
        as_of=as_of,
        ttm_rules=ttm_rules,
        pdf_selection=pdf_selection,
    )
    if earnings.earnings_yield is not None and earnings.price_date != expected_price_date:
        earnings = replace(
            earnings,
            earnings_yield=None,
            unavailable_reason="insufficient_calendar"
            if expected_price_date is None
            else "stale_price",
        )
    price = latest.price if latest is not None and latest.price > 0 else None
    financial = financial_observation_payload(
        build_financial_snapshot(
            price,
            documents,
            edinet,
            ttm_rules,
            adjustment_events=adjustment_events,
            asof_date=as_of,
            pdf_selection=pdf_selection,
        ),
        edinet,
    )
    # 変化の正準材料は企業成長。価格欠測で独立した財務観測を消さない。
    financial = {key: value for key, value in financial.items() if key not in _GROWTH_FIELDS}
    growth = generate_company_growth(
        documents,
        ticker=ticker,
        sector=sector,
        as_of=as_of,
        input_release=input_release,
        start=growth_start,
        comparisons=comparisons,
        pdf_statements=pdf_statements,
    )
    # The growth query's display window must not narrow the financial model.
    reported = tuple(
        build_reported_comparison(item)
        for item in comparisons
        if item.source_submit_datetime.date() <= as_of
    )
    if reported:
        financial["reported_comparisons"] = [item.model_dump(mode="json") for item in reported]
    if growth.pdf_supplement is not None:
        financial["pdf_supplement"] = growth.pdf_supplement.model_dump(mode="json")
    if pdf_selection is not None:
        financial["pdf_selection"] = pdf_selection.model_dump(mode="json")
    technical = generate_technical_analysis(
        bars, ticker=ticker, as_of=as_of, trading_dates=trading_dates, price_path=price_path
    )
    return (
        GeneratedModel(
            model_id="company_financials",
            subject=ticker,
            calculation_revision=(
                f"{VALUATION_CALCULATION_REVISION}-observations-v11-pdf-selection"
                if growth.pdf_supplement is not None
                else f"{VALUATION_CALCULATION_REVISION}-observations-v9-edinet-comparisons"
                if reported
                else f"{VALUATION_CALCULATION_REVISION}-observations-v8-reported-flow-periods"
            ),
            payload=json.loads(
                canonical_json(
                    {
                        "as_of": as_of,
                        "read_semantics": "retained_disclosures_through_as_of_not_receipt_vintage",
                        "earnings_price": asdict(earnings),
                        "earnings_basis": "reported_profit_before_common_share_allocations",
                        "earnings_composition_basis": (
                            "selected_retained_pdf_restated_display_amounts"
                            if pdf_selection is not None and pdf_selection.current is not None
                            else "latest_reported_period_amounts_not_restatement_reconciled"
                        ),
                        "expected_price_date": expected_price_date,
                        "financial": financial,
                        "ttm_rules": ttm_rules.model_dump(mode="json"),
                    }
                )
            ),
        ),
        GeneratedModel(
            model_id="company_growth",
            subject=ticker,
            calculation_revision=growth.calculation_method,
            payload=growth.model_dump(mode="json"),
        ),
        GeneratedModel(
            model_id="technical_analysis",
            subject=ticker,
            calculation_revision=TECHNICAL_CALCULATION_REVISION,
            payload=json.loads(canonical_json(technical)),
        ),
    )
