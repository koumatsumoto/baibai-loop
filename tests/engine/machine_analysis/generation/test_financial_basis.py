"""企業財務の保存は、異なる指標・異なる観測版を一つの実績にしない。"""

from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Literal

import pytest
from tests.engine.machine_analysis.observations.financials.test_pdf_jp_restatement import (
    ASOF,
    docs,
    pdf,
)
from tests.helpers.model_generation import publish_generation

from baibai_engine.investment_judgment.workflow.generated_inputs import project_generated_inputs
from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    InputSnapshot,
    ModelGeneration,
)
from baibai_engine.machine_analysis.generation.financial_models import (
    generate_financial_and_price_models,
)
from baibai_engine.machine_analysis.generation.query import (
    read_generation,
    read_research_model_inputs,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.financial_snapshot import (
    VALUATION_CALCULATION_REVISION,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.financials.pdf_statements import PdfStatement
from baibai_engine.machine_analysis.observations.prices.price_models import (
    JQuantsAdjustmentFactorEvent,
    PriceBar,
)
from baibai_engine.primitives.json import canonical_json

_RELEASE = InputRelease(release_id="financial-basis-fixture", manifest_sha256="a" * 64)


def _native() -> tuple[JQuantsFinancialSummary, ...]:
    quarter, annual = docs()
    quarter = replace(quarter, cfo=100_000_000, total_assets=2_000_000_000)
    annual = replace(annual, cfo=200_000_000)
    prior = replace(
        quarter,
        disclosed_at=date(2025, 8, 5),
        disclosure_number="prior-quarter",
        period_start=date(2025, 4, 1),
        period_end=date(2025, 6, 30),
        fiscal_year_end=date(2026, 3, 31),
        sales=1004_000_000,
        operating_profit=-40_000_000,
        profit=-70_000_000,
        cfo=50_000_000,
        total_assets=1_800_000_000,
    )
    return (prior, annual, quarter)


def _financial(
    rows: tuple[JQuantsFinancialSummary, ...],
    *,
    statements: tuple[PdfStatement, ...] = (),
    events: tuple[JQuantsAdjustmentFactorEvent, ...] = (),
) -> GeneratedModel:
    financial, _, _ = generate_financial_and_price_models(
        ticker="9127",
        documents=rows,
        adjusted_bars=(PriceBar(ASOF, 1_000.0, None),),
        adjustment_events=events,
        trading_dates=(ASOF,),
        expected_price_date=ASOF,
        sector="情報・通信業",
        as_of=ASOF,
        input_release=_RELEASE,
        ttm_rules=DEFAULT_TTM_RULES,
        pdf_statements=statements,
    )
    return financial


@pytest.mark.parametrize(
    ("route", "profit_ttm"),
    [("native", 1_091_000_000), ("restated", 1_109_000_000), ("not_eligible", None)],
)
def test_profit_vintage_never_changes_the_independent_native_cfo(
    route: Literal["native", "restated", "not_eligible"], profit_ttm: int | None
):
    statements = {
        "native": (),
        "restated": (pdf(), pdf(True)),
        "not_eligible": (pdf(),),
    }[route]
    result = _financial(_native(), statements=statements)
    financial, earnings = result.payload["financial"], result.payload["earnings_price"]
    assert isinstance(financial, dict)
    assert isinstance(earnings, dict)
    assert "accruals_to_assets" not in financial
    assert earnings["profit_ttm_yen"] == profit_ttm
    assert financial["ocf_ttm"] == 250_000_000
    assert financial["cfo"] == 100_000_000
    assert (
        financial["ocf_ttm_period"] == _financial(_native()).payload["financial"]["ocf_ttm_period"]
    )
    assert result.calculation_revision.startswith(VALUATION_CALCULATION_REVISION + "-")


@pytest.mark.parametrize(
    ("corrected_issued", "corrected_treasury", "expected_change"),
    [(None, 200.0, None), (1000.0, 200.0, 0.125), (None, None, 0.0)],
)
def test_tradable_share_yoy_resolves_capital_in_the_comparable_period(
    corrected_issued, corrected_treasury, expected_change
):
    prior, annual, quarter = _native()
    prior = replace(prior, shares_outstanding=1000.0, treasury_shares=100.0)
    # Share counts belong to the issuer even when the current financial basis changes.
    quarter = replace(
        quarter,
        shares_outstanding=1000.0,
        treasury_shares=100.0,
        document_type="1QFinancialStatements_NonConsolidated_IFRS",
    )
    correction = replace(
        prior,
        disclosed_at=date(2025, 8, 6),
        disclosure_number="prior-correction",
        shares_outstanding=corrected_issued,
        treasury_shares=corrected_treasury,
    )
    rows = (prior, correction, annual, quarter)
    for ordered in (rows, rows[::-1]):
        payload = _financial(ordered).payload["financial"]
        assert isinstance(payload, dict)
        assert payload["net_share_change_yoy"] == 0.0
        assert payload["tradable_share_change_yoy"] == expected_change


@pytest.mark.parametrize(
    ("case", "expected"),
    [("no_correction", 0.0), ("uncertain", None), ("same_day", None), ("later", 0.2)],
)
def test_prior_share_yoy_does_not_revive_pre_correction_treasury_after_a_split(case, expected):
    prior, annual, quarter = _native()
    prior = replace(prior, shares_outstanding=1000.0, treasury_shares=100.0, bps=50.0)
    quarter = replace(quarter, shares_outstanding=2000.0, treasury_shares=200.0)
    split = JQuantsAdjustmentFactorEvent("9127", date(2025, 9, 1), 0.5, "1")
    # The issuer corrects a pre-split period without specifying the new count's share unit.
    uncertain = replace(
        prior,
        disclosed_at=date(2026, 8, 6),
        disclosed_time=None,
        disclosure_number="treasury-only",
        shares_outstanding=None,
        treasury_shares=250.0,
        bps=None,
    )
    rows = (prior, annual, quarter)
    if case != "no_correction":
        rows += (uncertain,)
    if case in {"same_day", "later"}:
        rows += (
            replace(
                uncertain,
                disclosed_at=date(2026, 8, 6 if case == "same_day" else 7),
                disclosed_time="15:00:00",
                disclosure_number="complete-correction",
                shares_outstanding=2000.0,
                treasury_shares=500.0,
            ),
        )
    for ordered in (rows, rows[::-1]):
        payload = _financial(ordered, events=(split,)).payload["financial"]
        assert isinstance(payload, dict)
        assert payload["shares_ex_treasury"] == 1800.0
        assert payload["net_share_change_yoy"] == 0.0
        assert payload["cfo"] == 100_000_000
        # Only an uncontested share count can value the BPS reported in the prior filing.
        assert payload["pbr"] == (40.0 if case == "no_correction" else None)
        change = payload["tradable_share_change_yoy"]
        assert change == (pytest.approx(expected) if expected is not None else None)


@pytest.mark.parametrize("operating_profit", [None, 0.0, -40.0, 80.0])
def test_reported_operating_profit_never_uses_another_profit_line_or_year(operating_profit):
    prior, annual, quarter = _native()
    latest = replace(
        quarter, operating_profit=operating_profit, ordinary_profit=975.0, profit=123.0
    )
    payload = _financial((prior, annual, latest)).payload["financial"]
    assert isinstance(payload, dict)
    assert payload["operating_profit"] == operating_profit
    assert "operating_profit_source" not in payload
    if operating_profit is None:
        assert payload["operating_profit_ttm"] is None


def test_saved_historical_financial_payload_is_not_recalculated_or_relabelled(tmp_path):
    legacy = GeneratedModel(
        model_id="company_financials",
        subject="9127",
        calculation_revision="continuous-annual-input-v60-ttm-fiscal-year-anchor",
        payload={
            "financial": {
                "operating_profit": 975,
                "operating_profit_source": "OrdinaryProfit",
                "accruals_to_assets": 0.452105,
            }
        },
    )
    expected = ModelGeneration(
        generation_id="historical-financial-basis",
        as_of=ASOF,
        evaluation_time=datetime(2026, 11, 3, tzinfo=UTC),
        inputs={
            "market": InputSnapshot(
                kind="lake_release",
                reference=_RELEASE.release_id,
                sha256=_RELEASE.manifest_sha256,
            )
        },
        models=(legacy,),
    )
    path = tmp_path / "runs.sqlite"
    publish_generation(expected, path=path)
    actual = read_generation(expected.generation_id, path=path)
    assert actual is not None
    assert canonical_json(actual.model_dump(mode="json")) == canonical_json(
        expected.model_dump(mode="json")
    )
    saved = read_research_model_inputs(expected.generation_id, targets=("9127",), path=path)
    assert saved is not None
    research = project_generated_inputs(saved)["securities"]["9127"]["company_financials"]
    assert canonical_json(research) == canonical_json(legacy.payload)
