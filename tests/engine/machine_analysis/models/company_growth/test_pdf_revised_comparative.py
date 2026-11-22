"""後発の当期額不変だけで、組替と矛盾する旧前年の成長を復活させない。"""

from dataclasses import replace
from datetime import date

import pytest
from tests.engine.machine_analysis.models.company_growth.test_pdf_annual_comparison import (
    annual_inputs,
    comparisons,
    discovery_paths,
)
from tests.engine.machine_analysis.observations.financials.test_pdf_selection import models
from tests.helpers.pdf_statements import ASOF, base, pages

from baibai_engine.investment_judgment.workflow.company_growth_projection import (
    project_company_growth,
)
from baibai_engine.machine_analysis.models.company_growth.calculations import build_changes
from baibai_engine.machine_analysis.models.company_growth.models import CompanyGrowth
from baibai_engine.machine_analysis.models.company_growth.pdf_selection import apply_pdf_changes
from baibai_engine.machine_analysis.observations.financials.pdf_selection import (
    prepare_pdf_financials,
)
from baibai_engine.machine_analysis.observations.financials.pdf_statements import extract_pages

REASON = "restated_comparative_unverified_after_revision"


def revised_inputs(prior_basis="IFRS"):
    docs, pdf = annual_inputs(prior_basis)
    docs.append(
        replace(
            docs[-1],
            disclosure_number="later-eps-correction",
            disclosed_at=date(2026, 6, 19),
            cumulative_eps=123,
        )
    )
    return docs, pdf


def test_later_unchanged_amounts_do_not_revive_conflicting_native_growth():
    docs, pdf = revised_inputs()
    old_financial, confirmed, old_technical = models(documents=docs[:-1], pdfs=[pdf])
    financial, growth, technical = models(documents=docs, pdfs=[pdf])
    selection = growth.payload["pdf_selection"]
    assert selection["comparative_verified"] is False
    assert selection["annual_revision_basis"] == "latest_reconfirmed_unchanged_amounts"
    assert selection["annual_latest_disclosure_number"] == docs[-1].disclosure_number
    assert selection["current"]["disclosure_number"] == pdf.disclosure_number
    for measurement in ("reported", "annual_ttm"):
        for metric in ("sales", "operating_profit", "parent_profit", "operating_margin"):
            assert comparisons(confirmed, measurement)[metric]["difference"] < 0
            change = comparisons(growth, measurement)[metric]
            assert change["reason"] == REASON
            assert all(
                change[k] is None
                for k in ("from_value", "difference", "rate", "margin_points", "sign_change")
            )
            assert "source_sha256" not in change
            assert change["to_value"] == comparisons(confirmed, measurement)[metric]["to_value"]
    assert (
        financial.payload["earnings_price"]["profit_ttm_yen"]
        == old_financial.payload["earnings_price"]["profit_ttm_yen"]
    )
    assert technical == old_technical
    assert growth.payload["periods"][0] == confirmed.payload["periods"][0]


@pytest.mark.parametrize("delta", [0, 999_999, 1_000_000, -1_000_000])
def test_matching_or_one_display_unit_prior_is_not_a_confirmed_conflict(delta):
    docs, pdf = revised_inputs()
    docs[0] = replace(
        docs[0],
        sales=400_000_000_000 + delta,
        operating_profit=20_000_000_000,
        profit=4_000_000_000,
    )
    growth = models(documents=docs, pdfs=[pdf])[1]
    for measurement in ("reported", "annual_ttm"):
        for metric in ("sales", "operating_profit", "parent_profit", "operating_margin"):
            change = comparisons(growth, measurement)[metric]
            assert change["reason"] is None
            assert change["difference"] < 0
            assert "source_sha256" not in change


def test_only_conflicting_parent_profit_is_held_not_unrelated_sales_or_margin():
    docs, pdf = revised_inputs()
    docs[0] = replace(
        docs[0], sales=400_000_000_000, operating_profit=20_000_000_000, profit=-4_000_000_000
    )
    growth = models(documents=docs, pdfs=[pdf])[1]
    for measurement in ("reported", "annual_ttm"):
        items = comparisons(growth, measurement)
        assert items["parent_profit"]["reason"] == REASON
        assert items["parent_profit"]["sign_change"] is None
        for metric in ("sales", "operating_profit", "operating_margin"):
            assert items[metric]["reason"] is None
            assert items[metric]["difference"] < 0


@pytest.mark.parametrize("delta", [1_000_001, -1_000_001])
def test_over_one_display_unit_holds_sales_and_its_margin_only(delta):
    docs, pdf = revised_inputs()
    docs[0] = replace(
        docs[0],
        sales=400_000_000_000 + delta,
        operating_profit=20_000_000_000,
        profit=4_000_000_000,
    )
    growth = models(documents=docs, pdfs=[pdf])[1]
    for measurement in ("reported", "annual_ttm"):
        items = comparisons(growth, measurement)
        assert items["sales"]["reason"] == items["operating_margin"]["reason"] == REASON
        assert items["operating_profit"]["reason"] is None
        assert items["parent_profit"]["reason"] is None


@pytest.mark.parametrize(
    "mode", ["absent_prior", "different_basis", "different_prior_period", "no_pdf"]
)
def test_unknown_or_different_comparisons_keep_their_existing_reason(mode):
    docs, pdf = revised_inputs("JP" if mode == "different_basis" else "IFRS")
    if mode == "absent_prior":
        docs = docs[1:]
    elif mode == "different_prior_period":
        docs[0] = replace(docs[0], period_start=date(2024, 4, 2))
    native = models(documents=docs, pdfs=[])[1]
    growth = models(documents=docs, pdfs=[] if mode == "no_pdf" else [pdf])[1]
    for measurement in ("reported", "annual_ttm"):
        for metric in ("sales", "operating_profit", "parent_profit", "operating_margin"):
            change = comparisons(growth, measurement)[metric]
            assert change["reason"] == comparisons(native, measurement)[metric]["reason"]
            assert change["reason"] != REASON


def test_latest_matching_pdf_reconfirms_new_comparison_not_the_old_one():
    docs, pdf = revised_inputs()
    texts = pages(True)
    for old, new in (
        ("2026年5月15日", "2026年6月19日"),
        ("268,825", "300,000"),
        ("8,900", "20,000"),
        ("1,305", "4,000"),
    ):
        texts = [text.replace(old, new) for text in texts]
    latest = extract_pages(
        base(True).model_copy(
            update={"disclosure_number": docs[-1].disclosure_number, "source_sha256": "d" * 64}
        ),
        texts,
    )
    assert latest.reason is None
    growth = models(documents=docs, pdfs=[pdf, latest])[1]
    assert growth.payload["pdf_selection"]["comparative_verified"] is True
    for measurement in ("reported", "annual_ttm"):
        change = comparisons(growth, measurement)["sales"]
        assert change["reason"] is None
        assert change["from_value"] == 300_000_000_000
        assert change["difference"] == 15_507_000_000
        assert change["source_sha256"] == "d" * 64


def test_l3_and_financial_path_keep_unverified_comparison_without_a_company_veto():
    docs, pdf = revised_inputs()
    generated = models(documents=docs, pdfs=[pdf])
    growth = generated[1]
    projected = project_company_growth(growth.payload, ticker="9268")
    yoy = projected["latest_observations"]["annual_ttm"]["yoy"]
    by_metric = {item["metric"]: item for item in yoy}
    for metric in ("sales", "operating_profit", "parent_profit", "operating_margin"):
        assert by_metric[metric]["reason"] == REASON
        assert by_metric[metric]["from_value"] is None
        assert by_metric[metric]["rate"] is None
    paths = discovery_paths(generated)
    assert not any(p["path_id"] == "business_expansion" for p in paths)
    assert any(p["path_id"] == "earning_power" for p in paths)


@pytest.mark.parametrize("published", [date(2026, 5, 15), date(2026, 6, 1)])
def test_same_day_or_later_prior_revision_is_not_overruled_by_an_older_pdf(published):
    docs, pdf = revised_inputs()
    docs[0] = replace(docs[0], disclosed_at=published, disclosure_number="revised-prior-year")
    growth = models(documents=docs, pdfs=[pdf])[1]
    for measurement in ("reported", "annual_ttm"):
        for metric in ("sales", "operating_profit", "parent_profit", "operating_margin"):
            change = comparisons(growth, measurement)[metric]
            assert change["reason"] is None
            assert change["difference"] > 0
            assert "source_sha256" not in change


def test_unrecorded_prior_source_clock_does_not_become_a_confirmed_older_value():
    docs, pdf = revised_inputs()
    g = CompanyGrowth.model_validate(models(documents=docs, pdfs=[pdf])[1].payload)
    prior = g.periods[0]
    without_sources = prior.model_copy(
        update={
            "observations": tuple(
                observation.model_copy(
                    update={
                        "metrics": {
                            metric: value.model_copy(update={"documents": ()})
                            for metric, value in observation.metrics.items()
                        }
                    }
                )
                for observation in prior.observations
            )
        }
    )
    states = (without_sources, *g.periods[1:])
    native = build_changes(states)
    prepared = prepare_pdf_financials(docs, (pdf,), as_of=ASOF)
    assert (
        apply_pdf_changes(
            native,
            g.pdf_selection,
            states=states,
            native_anchor=prepared.native_anchor,
            sector="卸売業",
        )
        == native
    )
