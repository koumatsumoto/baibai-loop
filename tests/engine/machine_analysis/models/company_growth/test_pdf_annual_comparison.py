"""年度末の同じ実期間を、旧native値と組替比較欄で別方向へ評価しない。"""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from tests.engine.machine_analysis.observations.financials.test_pdf_selection import (
    document,
    models,
)
from tests.helpers.pdf_statements import ASOF, base, pages, statement

from baibai_engine.investment_judgment.workflow.company_growth_projection import (
    project_company_growth,
)
from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationMetadata,
    InputSnapshot,
)
from baibai_engine.machine_analysis.models.company_growth.models import CompanyGrowth
from baibai_engine.machine_analysis.models.company_growth.reading import CompanyGrowthReader
from baibai_engine.machine_analysis.observations.financials.pdf_statements import extract_pages
from baibai_engine.machine_analysis.screening.policies.economic_path_inputs import project_company
from baibai_engine.primitives.json import canonical_json


def annual_inputs(prior_basis="IFRS"):
    texts = pages(True)
    for old, new in (("268,825", "400,000"), ("8,900", "20,000"), ("1,305", "4,000")):
        texts = [text.replace(old, new) for text in texts]
    pdf = extract_pages(base(True), texts)
    assert pdf.reason is None
    previous = replace(
        document(True),
        disclosure_number="synthetic-prior-annual",
        disclosed_at=date(2025, 5, 15),
        period_start=date(2024, 4, 1),
        period_end=date(2025, 3, 31),
        fiscal_year_end=date(2025, 3, 31),
        document_type=f"FYFinancialStatements_Consolidated_{prior_basis}",
        sales=250_000_000_000,
        operating_profit=7_000_000_000,
        profit=1_000_000_000,
    )
    return [previous, document(True)], pdf


def comparisons(growth, measurement):
    return {
        item["metric"]: item
        for item in growth.payload["changes"]
        if item["measurement"] == measurement and item["to_end"] == "2026-03-31"
    }


@pytest.mark.parametrize("prior_basis", ["IFRS", "JP", None])
def test_annual_ttm_uses_same_verified_statement_comparison_without_rewriting_prior(prior_basis):
    docs, pdf = annual_inputs(prior_basis or "IFRS")
    if prior_basis is None:
        docs = docs[1:]
    before_docs, before_pdf = deepcopy(docs), pdf.model_dump_json()
    old = models(documents=docs, pdfs=[])[1]
    financial, growth, technical = models(documents=docs, pdfs=[pdf])
    reported = comparisons(growth, "reported")
    annual = comparisons(growth, "annual_ttm")
    for metric in ("sales", "operating_profit", "parent_profit", "operating_margin"):
        assert annual[metric] == {**reported[metric], "measurement": "annual_ttm"}
        assert annual[metric]["difference"] < 0
        assert annual[metric]["source_sha256"] == pdf.source_sha256
        assert annual[metric]["precision"] == "derived_from_truncated_display_amounts"
    if prior_basis is not None:
        assert growth.payload["periods"][0] == old.payload["periods"][0]
    assert docs == before_docs
    assert pdf.model_dump_json() == before_pdf
    assert financial.payload["earnings_price"]["profit_ttm_yen"] == 2_469_000_000
    assert technical == models(documents=docs, pdfs=[])[2]
    assert growth.calculation_revision == "company_growth_retrospective_v18-revised-comparative"


def test_annual_reader_and_l3_keep_restated_prior_source_and_dates():
    docs, pdf = annual_inputs()
    growth = models(documents=docs, pdfs=[pdf])[1]
    reader = CompanyGrowthReader(growth.payload)
    annual = reader.observation(reader.latest_period, "annual_ttm")
    change = reader.year_over_year(annual, "sales")[0]
    (prior,) = reader.comparison_prior(annual, change)
    assert prior.measurement == "annual_ttm"
    assert (prior.period_start, prior.period_end) == (date(2024, 4, 1), date(2025, 3, 31))
    assert prior.metrics["sales"].value == 400_000_000_000
    (source,) = prior.metrics["sales"].documents
    assert (source.disclosed_at, source.source_sha256, source.source_url) == (
        pdf.published_on,
        pdf.source_sha256,
        pdf.source_url,
    )
    assert not reader.comparison_prior(
        annual, change.model_copy(update={"source_sha256": "c" * 64})
    )
    view = project_company_growth(growth.payload, ticker="9268")
    assert (
        next(
            item
            for item in view["latest_observations"]["annual_ttm"]["yoy"]
            if item["metric"] == "sales"
        )
        == comparisons(growth, "annual_ttm")["sales"]
    )


def test_annual_growth_path_cannot_use_old_increase_instead_of_restated_decline():
    docs, pdf = annual_inputs()
    generated = models(documents=docs, pdfs=[pdf])
    market = GeneratedModel(
        model_id="market_context",
        subject="9268",
        calculation_revision="synthetic",
        payload={"master": {"sector_33": "卸売業"}},
    )
    metadata = GenerationMetadata(
        generation_id="synthetic-restated-fy",
        as_of=ASOF,
        evaluation_time=datetime(2026, 11, 1, tzinfo=UTC),
        inputs={
            "market": InputSnapshot(kind="lake_release", reference="synthetic", sha256="a" * 64)
        },
    )
    row = project_company("9268", models=(*generated, market), metadata=metadata)
    path = next(
        p for p in row.paths if p.path_id == "business_expansion" and p.variant == "annual_ttm"
    )
    assert path.business_value == pytest.approx((315_507 - 400_000) / 400_000)
    assert not path.eligible
    # The correction is not a new company veto or an investment decision.
    assert any(p.path_id == "earning_power" and p.eligible for p in row.paths)
    assert path.evidence["sales_change"]["source_sha256"] == pdf.source_sha256


@pytest.mark.parametrize("mode", ["no_pdf", "future_pdf"])
def test_unverified_or_absent_annual_comparative_does_not_acquire_new_authority(mode):
    docs, pdf = annual_inputs()
    if mode == "future_pdf":
        pdf = pdf.model_copy(update={"published_on": date(2026, 11, 2)})
    growth = models(documents=docs, pdfs=[] if mode == "no_pdf" else [pdf])[1]
    annual = comparisons(growth, "annual_ttm")["sales"]
    assert annual["from_value"] == 250_000_000_000
    assert annual["difference"] > 0
    assert "source_sha256" not in annual


def test_interim_statement_comparison_is_not_an_annual_ttm_comparison():
    _, growth, _ = models()
    for item in growth.payload["changes"]:
        if item["measurement"] == "annual_ttm":
            assert "source_sha256" not in item
    quarterly = [
        item
        for item in growth.payload["changes"]
        if item["measurement"] == "quarter" and item["to_end"] == "2026-06-30"
    ]
    assert any(item.get("source_sha256") == statement().source_sha256 for item in quarterly)


def test_old_growth_payload_keeps_its_recorded_comparison_and_revision():
    docs, _ = annual_inputs()
    old = models(documents=docs, pdfs=[])[1].payload
    old["calculation_method"] = "company_growth_retrospective_v16-ifrs-interim-display"
    before = canonical_json(old)
    reader = CompanyGrowthReader(old)
    change = reader.year_over_year(reader.latest_full_fy, "sales")[0]
    assert change.from_value == 250_000_000_000
    assert change.difference > 0
    assert canonical_json(CompanyGrowth.model_validate(old).model_dump(mode="json")) == before
