"""固定・共有読取の結果から個社と共有モデルを組み立てる。取得・選定・保存はしない。"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationMetadata,
    GenerationSubject,
    InputSnapshot,
    ModelGenerationStream,
)
from baibai_engine.machine_analysis.generation.financial_models import (
    generate_financial_and_price_models,
)
from baibai_engine.machine_analysis.generation.runners.external_assessment import (
    generate_external_assessment,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import TTMRules
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
from baibai_engine.machine_analysis.models.industry_trends.calculations import (
    REVISION as INDUSTRY_REVISION,
)
from baibai_engine.machine_analysis.models.industry_trends.calculations import (
    calculate_industry_trends,
)
from baibai_engine.machine_analysis.models.social_trends.calculations import (
    REVISION as SOCIAL_REVISION,
)
from baibai_engine.machine_analysis.models.social_trends.calculations import calculate_social_trends
from baibai_engine.machine_analysis.observations.external_assessments.history import (
    ObservationHistory,
)
from baibai_engine.machine_analysis.observations.financials.edinet_models import EdinetMetricRecord
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.prices.price_models import (
    JQuantsAdjustmentFactorEvent,
    PriceBar,
)
from baibai_engine.machine_analysis.generation.runners.trend_inputs import TrendGenerationInputs
from baibai_engine.primitives.json import canonical_json
from baibai_engine.primitives.stocks import normalize_ticker


@dataclass(frozen=True, slots=True)
class CompanyModelInputs:
    ticker: str
    financial_documents: Sequence[JQuantsFinancialSummary]
    adjusted_bars: Sequence[PriceBar]
    adjustment_events: Sequence[JQuantsAdjustmentFactorEvent]
    sector: str | None
    edinet: EdinetMetricRecord | None = None
    price_path: dict[str, object] | None = None


def generate_models(
    *,
    generation_id: str,
    as_of: date,
    evaluation_time: datetime,
    inputs: dict[str, InputSnapshot],
    financial_release: InputRelease,
    companies: Sequence[CompanyModelInputs],
    trading_dates: Sequence[date],
    expected_price_date: date | None,
    ttm_rules: TTMRules,
    external_observations: Mapping[str, ObservationHistory],
    market_context: dict[str, object] | None,
    securities_context: Mapping[str, dict[str, object]],
    market_context_revision: str,
    growth_start: date | None = None,
    trends: TrendGenerationInputs | None = None,
) -> ModelGenerationStream:
    """各ownerへ共有入力を渡す。参照母集団を候補集合へ縮めない。

    市場/業種の参照母集団と価格basisは読取ownerが確定する。ここで個社queryを
    反復しない。外部/市場の入力が渡されない場合は未生成であり、0や正常値で埋めない。
    """
    tickers = [normalize_ticker(company.ticker) for company in companies]
    if len(tickers) != len(set(tickers)):
        raise ValueError("model generation requires unique companies")
    if not any(
        item.kind == "lake_release"
        and item.reference == financial_release.release_id
        and item.sha256 == financial_release.manifest_sha256
        for item in inputs.values()
    ):
        raise ValueError("financial release must belong to the fixed generation inputs")
    # Model participation is owned by generation, not copied into L3 or Screening.
    company_model_ids = (
        "company_valuation",
        "company_growth",
        "technical_analysis",
        "external_assessment",
        "market_context",
    )
    shared_plan: dict[str, GenerationSubject] = {}
    related: dict[str, tuple[str, ...]] = {}
    if trends is not None:
        for fixed in (trends.social, *(item.inputs for item in trends.industries)):
            if (
                fixed.manifest.release_id != financial_release.release_id
                or fixed.manifest.release_sha256 != financial_release.manifest_sha256
                or fixed.manifest.cutoff != evaluation_time
            ):
                raise ValueError("trend input manifest differs from the fixed generation inputs")
        shared_plan["market:global"] = GenerationSubject(
            scope="market", model_ids=("social_trends",)
        )
        for industry in trends.industries:
            subject = f"industry:{industry.definition.scope_id}"
            shared_plan[subject] = GenerationSubject(scope="sector", model_ids=("industry_trends",))
        industry_links = dict(trends.industry_links)
        related = {
            ticker: tuple(
                sorted(
                    f"industry:{item.definition.scope_id}"
                    for item in trends.industries
                    if ticker in industry_links.get(item.definition.scope_id, ())
                )
            )
            for ticker in tickers
        }
    metadata = GenerationMetadata(
        schema_version=2,
        subject_plan={
            **{
                ticker: GenerationSubject(
                    scope="stock",
                    model_ids=company_model_ids,
                    related_subjects=related.get(ticker, ()),
                )
                for ticker in tickers
            },
            "market": GenerationSubject(scope="market", model_ids=("market_context",)),
            **shared_plan,
        },
        generation_id=generation_id,
        as_of=as_of,
        evaluation_time=evaluation_time,
        inputs=inputs,
    )

    def models() -> Iterator[GeneratedModel]:
        for ticker, company in zip(tickers, companies, strict=True):
            yield from generate_financial_and_price_models(
                ticker=ticker,
                documents=company.financial_documents,
                adjusted_bars=company.adjusted_bars,
                adjustment_events=company.adjustment_events,
                trading_dates=trading_dates,
                expected_price_date=expected_price_date,
                sector=company.sector,
                as_of=as_of,
                input_release=financial_release,
                ttm_rules=ttm_rules,
                growth_start=growth_start,
                edinet=company.edinet,
                price_path=company.price_path,
            )
            history = external_observations.get(ticker)
            if history is not None:
                if history.ticker != ticker or history.cutoff > evaluation_time:
                    raise ValueError("external observation identity or cutoff differs")
                external = generate_external_assessment(history, evaluated_at=evaluation_time)
                yield GeneratedModel(
                    model_id="external_assessment",
                    subject=ticker,
                    calculation_revision=external.method_version,
                    payload=external.model_dump(mode="json"),
                )
            if ticker in securities_context:
                yield _market_model(ticker, securities_context[ticker], market_context_revision)
        if market_context is not None:
            yield _market_model("market", market_context, market_context_revision)
        if trends is not None:
            for industry in trends.industries:
                yield GeneratedModel(
                    model_id="industry_trends",
                    subject=f"industry:{industry.definition.scope_id}",
                    calculation_revision=INDUSTRY_REVISION,
                    payload=json.loads(canonical_json(calculate_industry_trends(industry))),
                )
            yield GeneratedModel(
                model_id="social_trends",
                subject="market:global",
                calculation_revision=SOCIAL_REVISION,
                payload=json.loads(
                    canonical_json(calculate_social_trends(trends.social, as_of=as_of))
                ),
            )

    return ModelGenerationStream(metadata, tuple(tickers), models())


def _market_model(subject: str, payload: dict[str, object], revision: str) -> GeneratedModel:
    return GeneratedModel(
        model_id="market_context",
        subject=subject,
        calculation_revision=revision,
        payload=json.loads(canonical_json(payload)),
    )
