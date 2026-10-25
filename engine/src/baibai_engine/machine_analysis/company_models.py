"""共有された財務・価格入力をモデルへ組み立てる。候補選択・取得・保存はしない。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date

from baibai_engine.data_collection.identifiers import normalize_ticker
from baibai_engine.foundation.json import canonical_json
from baibai_engine.market.jquants_models import JQuantsFinancialSummary
from baibai_engine.market.price_models import JQuantsAdjustmentFactorEvent, PriceBar

from .company_growth.generation import generate_company_growth
from .company_growth.models import InputRelease
from .company_valuation.earnings import earnings_price
from .company_valuation.metrics.financial_snapshot import (
    VALUATION_CALCULATION_REVISION,
    build_financial_snapshot,
)
from .company_valuation.metrics.models import TTMRules
from .model_generation import GeneratedModel
from .technical_analysis.calculations import TECHNICAL_CALCULATION_REVISION
from .technical_analysis.generation import generate_technical_analysis
from .technical_analysis.momentum import price_momentum_12_1

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
    sector: str | None,
    as_of: date,
    input_release: InputRelease,
    ttm_rules: TTMRules,
    growth_start: date | None = None,
) -> tuple[GeneratedModel, ...]:
    """同じ個社入力から企業価値・企業成長・テクニカル分析を生成する。

    barsはas_ofと同じ調整基準。財務原本の全履歴を受け取り、企業成長の振り返りと
    企業価値の開示日cutoffは各ownerの意味を保つ。EDINETによる詳細調査はL3へ委ねる。
    市場環境・外部評価はそれぞれの共有読取から別途同じgenerationへ組み込む。
    """
    ticker = normalize_ticker(ticker)
    if any(row.ticker != ticker for row in documents) or any(
        event.ticker != ticker for event in adjustment_events
    ):
        raise ValueError("company model inputs contain a different ticker")
    bars = sorted(
        (bar for bar in adjusted_bars if bar.traded_at <= as_of), key=lambda bar: bar.traded_at
    )
    latest = bars[-1] if bars else None
    earnings = earnings_price(
        documents,
        adjusted_price=latest,
        adjustment_events=adjustment_events,
        as_of=as_of,
        ttm_rules=ttm_rules,
    )
    financial = None
    if latest is not None and latest.price > 0:
        financial = asdict(
            build_financial_snapshot(
                latest.price,
                documents,
                None,
                ttm_rules,
                adjustment_events=adjustment_events,
                asof_date=as_of,
            )
        )
        # 変化の正準材料は企業成長の期間・basis・実数差・sign_change。
        # 旧snapshotの負基準前年比を、成長モデルと競合する評価軸にしない。
        financial = {key: value for key, value in financial.items() if key not in _GROWTH_FIELDS}
    growth = generate_company_growth(
        documents,
        ticker=ticker,
        sector=sector,
        as_of=as_of,
        input_release=input_release,
        start=growth_start,
    )
    technical = generate_technical_analysis(bars, ticker=ticker, as_of=as_of)
    technical["price_momentum"] = asdict(
        price_momentum_12_1(bars, trading_dates=trading_dates, as_of=as_of)
    )
    technical["price_momentum_revision"] = "common-calendar-252-21-v1"
    return (
        GeneratedModel(
            model_id="company_valuation",
            subject=ticker,
            calculation_revision=f"{VALUATION_CALCULATION_REVISION}-observations-v1",
            payload=json.loads(
                canonical_json(
                    {
                        "as_of": as_of,
                        "read_semantics": "retained_disclosures_through_as_of_not_receipt_vintage",
                        "earnings_price": asdict(earnings),
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
