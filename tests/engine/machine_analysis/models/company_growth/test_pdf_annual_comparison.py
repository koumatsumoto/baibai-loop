"""年度末の同じ実期間を、旧native値と組替比較欄で別方向へ評価しない。"""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

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
from baibai_engine.machine_analysis.models.company_growth.models import (
    FINANCIAL_SECTOR_INAPPLICABLE,
    CompanyGrowth,
)
from baibai_engine.machine_analysis.models.company_growth.reading import CompanyGrowthReader
from baibai_engine.machine_analysis.observations.financials.pdf_statements import extract_pages
from baibai_engine.machine_analysis.screening.policies.discovery import project_discovery_row
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


def discovery_paths(generated):
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
    return project_discovery_row("9268", models=(*generated, market), metadata=metadata)[
        "coordinates"
    ]


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
    assert growth.calculation_revision == "company_growth_retrospective_v26-pdf-jp-interim"


@pytest.mark.parametrize(
    ("sector", "margin_reason"),
    [
        ("卸売業", None),
        ("銀行業", FINANCIAL_SECTOR_INAPPLICABLE),
        (None, "sector_unverified"),
    ],
)
def test_annual_reader_and_l3_keep_restated_prior_source_and_dates(sector, margin_reason):
    docs, pdf = annual_inputs()
    growth = models(documents=docs, pdfs=[pdf], sector=sector)[1]
    before = canonical_json(growth.payload)
    reader = CompanyGrowthReader(growth.payload)
    annual = reader.observation(reader.latest_period, "annual_ttm")
    change = reader.year_over_year(annual, "sales")[0]
    (prior,) = reader.comparison_prior(annual, change)
    assert prior.measurement == "annual_ttm"
    assert (prior.period_start, prior.period_end) == (date(2024, 4, 1), date(2025, 3, 31))
    assert prior.metrics["sales"].value == 400_000_000_000
    assert prior.metrics["sales"].precision == "published_display_amounts"
    margin_change = reader.year_over_year(annual, "operating_margin")[0]
    (margin_prior,) = reader.comparison_prior(annual, margin_change)
    margin = margin_prior.metrics["operating_margin"]
    assert annual.metrics["operating_margin"].reason == margin_reason
    assert margin_prior.metrics["sales"] == prior.metrics["sales"]
    assert margin_prior.metrics["operating_profit"].value == 20_000_000_000
    if margin_reason is None:
        assert margin.value == margin_change.from_value == pytest.approx(0.05)
        assert margin.precision == "derived_from_truncated_display_amounts"
        assert margin.documents == prior.metrics["sales"].documents
    else:
        assert margin.value is margin_change.from_value is None
        assert margin.reason == margin_change.reason
        assert margin.precision is None
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
    assert canonical_json(growth.payload) == before


def test_annual_growth_path_cannot_use_old_increase_instead_of_restated_decline():
    docs, pdf = annual_inputs()
    generated = models(documents=docs, pdfs=[pdf])
    paths = discovery_paths(generated)
    assert not any(p["path_id"] == "business_expansion" for p in paths)
    # The correction is not a new company veto or an investment decision.
    assert any(p["path_id"] == "earning_power" for p in paths)
    change = comparisons(generated[1], "annual_ttm")["sales"]
    assert change["rate"] == pytest.approx((315_507 - 400_000) / 400_000)
    assert (change["from_value"], change["to_value"]) == (400_000_000_000, 315_507_000_000)
    assert (change["from_end"], change["to_end"]) == ("2025-03-31", "2026-03-31")
    assert change["source_sha256"] == pdf.source_sha256


def test_retrospective_growth_does_not_replace_new_native_revision_with_an_old_pdf():
    docs, pdf = annual_inputs()
    # Only the sales comparative conflicts. Profit has an independent compatible prior.
    docs[0] = replace(docs[0], operating_profit=20_000_000_000, profit=4_000_000_000)
    revised = replace(
        docs[-1],
        disclosed_at=ASOF + timedelta(days=1),
        disclosure_number="retained-next-day-correction",
        sales=380_000_000_000,
    )
    baseline_financial, _, _ = models(documents=docs, pdfs=[pdf])
    financial, growth, _ = models(documents=[*docs, revised], pdfs=[pdf])

    assert financial == baseline_financial  # the valuation side still fixes as-of
    reader = CompanyGrowthReader(growth.payload)
    for measurement in ("annual_ttm", "reported"):
        observed = reader.observation(reader.latest_period, measurement)
        sales = observed.metrics["sales"]
        assert sales.value == 380_000_000_000
        assert sales.documents[0].disclosure_number == revised.disclosure_number
        comparison = comparisons(growth, measurement)
        assert comparison["sales"]["to_value"] == sales.value
        assert comparison["sales"]["from_value"] is comparison["sales"]["difference"] is None
        assert comparison["sales"]["reason"] == "restated_comparative_unverified_after_revision"
        assert "source_sha256" not in comparison["sales"]
        assert comparison["operating_margin"]["difference"] is None
        assert comparison["operating_profit"]["from_value"] == 20_000_000_000
        assert comparison["operating_profit"]["difference"] is not None


def test_pdf_prior_refuses_a_copied_change_with_the_same_source_but_different_amount():
    docs, pdf = annual_inputs()
    growth = models(documents=docs, pdfs=[pdf], sector="卸売業")[1]
    reader = CompanyGrowthReader(growth.payload)
    annual = reader.latest_full_fy
    assert annual is not None
    (change,) = reader.year_over_year(annual, "operating_margin")
    (prior,) = reader.comparison_prior(annual, change)
    assert prior.metrics["operating_margin"].value == change.from_value == pytest.approx(0.05)

    copied = change.model_copy(update={"from_value": 0.99})
    assert copied.source_sha256 == change.source_sha256 == pdf.source_sha256
    assert reader.comparison_prior(annual, copied) == ()


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
