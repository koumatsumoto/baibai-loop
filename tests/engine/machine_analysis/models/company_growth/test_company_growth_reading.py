"""読取はownerの順序と保存形を守り、公開日cutoffを暗黙に補わない。"""

from copy import deepcopy
from dataclasses import replace
from datetime import date

import pytest

from baibai_engine.machine_analysis.models.company_growth.generation import generate_company_growth
from baibai_engine.machine_analysis.models.company_growth.models import (
    CompanyGrowth,
    InputRelease,
    Observation,
    SourceDocument,
    Value,
)
from baibai_engine.machine_analysis.models.company_growth.reading import (
    CompanyGrowthReader,
    metric_value,
)
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.primitives.json import canonical_json


def saved_growth():
    prior = JQuantsFinancialSummary(
        ticker="1001",
        disclosed_at=date(2024, 5, 15),
        fiscal_period="FY",
        fiscal_year_end=date(2024, 3, 31),
        period_start=date(2023, 4, 1),
        period_end=date(2024, 3, 31),
        sales=1000.0,
        operating_profit=100.0,
        profit=80.0,
        document_type="FYFinancialStatements_Consolidated_JP",
        disclosure_number="prior-fy",
    )
    current = replace(
        prior,
        disclosed_at=date(2025, 5, 15),
        fiscal_year_end=date(2025, 3, 31),
        period_start=date(2024, 4, 1),
        period_end=date(2025, 3, 31),
        sales=1200.0,
        operating_profit=180.0,
        profit=150.0,
        disclosure_number="current-fy",
    )
    quarter = replace(
        current,
        fiscal_period="1Q",
        disclosed_at=date(2025, 8, 10),
        fiscal_year_end=date(2026, 3, 31),
        period_start=date(2025, 4, 1),
        period_end=date(2025, 6, 30),
        sales=400.0,
        operating_profit=80.0,
        profit=70.0,
        disclosure_number="current-quarter",
    )
    return generate_company_growth(
        [prior, current, quarter],
        ticker="1001",
        sector="情報・通信業",
        as_of=date(2025, 8, 31),
        input_release=InputRelease(release_id="reader-fixture", manifest_sha256="b" * 64),
    ).model_dump(mode="json")


def test_reader_uses_owner_periods_comparisons_and_prior_sources_without_rewriting_payload():
    payload = saved_growth()
    before = canonical_json(payload)
    reader = CompanyGrowthReader(payload)
    assert reader.latest_period.period_end == date(2025, 6, 30)
    annual = reader.observation(reader.latest_period, "annual_ttm")
    assert annual.period_status == "matching_period_not_observed"
    fy = reader.latest_full_fy
    assert (fy.period_start, fy.period_end) == (date(2024, 4, 1), date(2025, 3, 31))
    (change,) = reader.year_over_year(fy, "sales")
    assert change.difference == 200.0
    (prior,) = reader.comparison_prior(fy, change)
    assert prior.period_end == date(2024, 3, 31)
    assert metric_value(prior, "sales", disclosed_by=date(2024, 5, 15)) == 1000.0
    assert metric_value(prior, "sales", disclosed_by=date(2024, 5, 14)) is None
    assert reader.saved_observation(fy) in payload["periods"][1]["observations"]
    assert reader.saved_change(change) in payload["changes"]
    assert canonical_json(payload) == before


def test_reader_does_not_fill_absent_legacy_fields_or_normalize_evidence_numbers():
    payload = saved_growth()
    payload["calculation_method"] = "company_growth_retrospective_v3"
    for state in payload["periods"]:
        for item in state["observations"]:
            item.pop("period_status", None)
            item.pop("share_basis")
            for value in item["metrics"].values():
                if value["reason"] is None:
                    value.pop("reason")
    payload["periods"][-1]["observations"][0]["metrics"]["sales"]["value"] = 400
    change = next(
        item
        for item in payload["changes"]
        if item["measurement"] == "reported" and item["to_end"] == "2025-06-30"
    )
    change.pop("margin_points")
    before = canonical_json(payload)
    reader = CompanyGrowthReader(payload)
    annual = reader.observation(reader.latest_period, "annual_ttm")
    assert annual.period_status is None
    assert "period_status" not in reader.saved_observation(annual)
    reported = reader.observation(reader.latest_period, "reported")
    evidence = reader.saved_observation(reported)
    assert type(evidence["metrics"]["sales"]["value"]) is int
    assert "reason" not in evidence["metrics"]["sales"]
    assert "share_basis" not in evidence
    (comparison,) = reader.year_over_year(reported, change["metric"])
    assert "margin_points" not in reader.saved_change(comparison)
    assert canonical_json(payload) == before


def test_saved_evidence_uses_identity_even_for_equal_typed_observations_and_changes():
    payload = saved_growth()
    state = deepcopy(payload["periods"][-1])
    state["observations"][0].pop("share_basis")
    payload["periods"].append(state)
    duplicate = deepcopy(payload["changes"][-1])
    duplicate.pop("margin_points")
    payload["changes"].append(duplicate)
    reader = CompanyGrowthReader(payload)
    assert reader.model.periods[-2].observations[0] == reader.model.periods[-1].observations[0]
    assert (
        reader.saved_observation(reader.model.periods[-1].observations[0])
        is state["observations"][0]
    )
    assert reader.model.changes[-2] == reader.model.changes[-1]
    assert reader.saved_change(reader.model.changes[-1]) is duplicate


@pytest.mark.parametrize("measurement", ["quarter", "annual_ttm"])
def test_metric_cutoff_is_explicit_and_checks_every_source_document(measurement):
    old = SourceDocument(
        disclosure_number="original",
        disclosed_at=date(2025, 5, 15),
        disclosed_time=None,
        document_type=None,
    )
    future = old.model_copy(
        update={"disclosure_number": "correction", "disclosed_at": date(2025, 9, 1)}
    )
    observation = Observation(
        period_start=date(2025, 4, 1),
        period_end=date(2025, 6, 30),
        measurement=measurement,
        statement_basis="consolidated",
        metrics={
            "sales": Value(value=0.0, documents=(old, future)),
            "operating_profit": Value(value=-1.0, documents=(old,)),
            "parent_profit": Value(value=10.0),
        },
    )
    assert metric_value(observation, "sales") == 0.0
    assert metric_value(observation, "sales", disclosed_by=date(2025, 8, 31)) is None
    assert metric_value(observation, "sales", disclosed_by=date(2025, 9, 1)) == 0.0
    assert metric_value(observation, "operating_profit", disclosed_by=date(2025, 8, 31)) == -1.0
    assert metric_value(observation, "parent_profit") == 10.0
    assert metric_value(observation, "parent_profit", disclosed_by=date(2025, 8, 31)) is None
    assert metric_value(observation, "operating_margin") is None
    assert metric_value(None, "sales") is None


@pytest.mark.parametrize("generated", [False, True])
def test_absent_and_generated_unavailable_growth_remain_distinct(generated):
    payload = (
        CompanyGrowth(
            ticker="1001",
            status="unavailable",
            requested_start=None,
            requested_end=date(2025, 8, 31),
        ).model_dump(mode="json")
        if generated
        else None
    )
    reader = CompanyGrowthReader(payload)
    assert (reader.model is not None) is generated
    assert reader.latest_period is None
    assert reader.latest_full_fy is None
    assert reader.observation(None, "quarter") is None
    assert reader.year_over_year(None, "sales") == ()
    assert reader.saved_observation(None) == {}
