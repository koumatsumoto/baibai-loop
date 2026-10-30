"""完全FYの発見経路は、利益の良し悪しでTTMから切り替えない。"""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import TypeAdapter

from baibai_engine.investment_judgment.workflow.screening_brief import economic_screening_brief
from baibai_engine.machine_analysis.generation.assembly import CompanyModelInputs, generate_models
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.store import ModelGenerationStore
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease, Observation
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar
from baibai_engine.machine_analysis.screening.policies.economic_path_inputs import project_company
from baibai_engine.machine_analysis.screening.policies.economic_path_selection import (
    select_saved_economic_generation,
)
from baibai_engine.machine_analysis.screening.policies.economic_paths import (
    EconomicPathPolicy,
    EconomicPathSelection,
    select_economic_paths,
)
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.primitives.json import canonical_json

AS_OF = date(2025, 8, 31)


def document(year, fiscal_period, sales, op, profit):
    annual = fiscal_period == "FY"
    end = date(year + 1, 3, 31) if annual else date(year, 6, 30)
    return JQuantsFinancialSummary(
        ticker="1001",
        disclosed_at=date(year + 1, 5, 15) if annual else date(year, 8, 10),
        fiscal_period=fiscal_period,
        fiscal_year_end=date(year + 1, 3, 31),
        period_start=date(year, 4, 1),
        period_end=end,
        sales=sales,
        operating_profit=op,
        profit=profit,
        cumulative_eps=999.0,
        shares_outstanding=100.0,
        treasury_shares=20.0,
        bps=10.0,
        disclosure_number=f"fy-fixture-{year}-{fiscal_period}",
        document_type=f"{fiscal_period}FinancialStatements_Consolidated_JP",
    )


def documents(*, prior_quarter=False):
    rows = [
        document(2023, "FY", 1000.0, 100.0, 100.0),
        document(2024, "FY", 1200.0, 180.0, 150.0),
        document(2025, "1Q", 400.0, 80.0, 70.0),
    ]
    if prior_quarter:
        rows.append(document(2024, "1Q", 500.0, 100.0, 100.0))
    return rows


def generation(rows):
    release = InputRelease(release_id="fy-fixture-release", manifest_sha256="b" * 64)
    return generate_models(
        generation_id="fy-fixture-generation",
        as_of=AS_OF,
        evaluation_time=datetime(2025, 9, 1, tzinfo=UTC),
        inputs={
            "market": InputSnapshot(
                kind="lake_release", reference=release.release_id, sha256=release.manifest_sha256
            )
        },
        financial_release=release,
        companies=(
            CompanyModelInputs(
                ticker="1001",
                financial_documents=rows,
                # Short listing/price history must not erase disclosed prior FY comparisons.
                adjusted_bars=(PriceBar(AS_OF, 10.0, None),),
                adjustment_events=(),
                sector="情報・通信業",
            ),
        ),
        trading_dates=(AS_OF,),
        expected_price_date=AS_OF,
        ttm_rules=DEFAULT_TTM_RULES,
        external_observations={},
        market_context=None,
        securities_context={"1001": {"master": {"sector_33": "情報・通信業"}}},
        market_context_revision="fy-fixture-v1",
    ).materialize()


def project(fixed):
    return project_company("1001", models=fixed.models, metadata=fixed)


def active(row, path_id):
    return next((path for path in row.paths if path.path_id == path_id and path.eligible), None)


def with_growth(fixed, update):
    models = []
    for model in fixed.models:
        if model.model_id == "company_growth":
            payload = deepcopy(model.payload)
            update(payload)
            model = model.model_copy(update={"payload": payload})
        models.append(model)
    return fixed.model_copy(update={"models": tuple(models)})


def test_missing_ttm_uses_same_complete_fy_and_preserves_short_history_comparison(tmp_path):
    fixed = generation(documents())
    path = tmp_path / "runs.sqlite"
    ModelGenerationStore(path).publish(fixed)
    selected = select_saved_economic_generation(
        fixed.generation_id, tickers=("1001",), policy=EconomicPathPolicy(), path=path
    )
    card = selected.result.cards[0]
    paths = {item.path_id: item for item in card.paths}
    assert set(paths) == {"earning_power", "business_expansion", "operating_recovery"}
    assert all(item.variant == "full_fy" for item in paths.values())
    income = paths["earning_power"]
    assert income.price_value == pytest.approx(150.0 / 800.0)
    assert income.business_value == pytest.approx(180.0 / 1200.0)
    assert income.evidence["earnings_period_start"] == "2024-04-01"
    assert income.evidence["earnings_period_end"] == "2025-03-31"
    assert (
        income.evidence["profit_observation"]["observation"]["documents"][0]["disclosure_number"]
        == "fy-fixture-2024-FY"
    )
    assert paths["business_expansion"].business_value == pytest.approx(0.2)
    assert paths["operating_recovery"].price_value == pytest.approx(80.0 / 800.0)
    assert card.context["annual_observation"]["full_fy_age_days"] == 153
    assert card.context["annual_observation"]["latest_ttm_period_status"] == (
        "matching_period_not_observed"
    )
    brief = economic_screening_brief(
        TypeAdapter(EconomicPathSelection).dump_python(selected.result, mode="json")
    )
    annual = brief["cards"][0]["context"]["annual_observation"]
    assert annual["full_fy_age_days"] == 153
    assert annual["latest_quarter_observation"]["period_end"] == "2025-06-30"
    assert annual["latest_quarter_observation"]["metrics"]["parent_profit"]["value"] == 70.0
    store = ScreeningSelectionStore(path)
    assert store.publish("fy-selection", selected)
    assert store.read("fy-selection") == selected
    assert not store.publish("fy-selection", selected)


def test_available_ttm_never_chooses_the_more_attractive_fy():
    row = project(generation(documents(prior_quarter=True)))
    income = active(row, "earning_power")
    assert income.variant == "annual_ttm"
    assert income.price_value == pytest.approx(120.0 / 800.0)
    fy = next(
        path for path in row.paths if path.path_id == "earning_power" and path.variant == "full_fy"
    )
    assert fy.price_value > income.price_value
    assert fy.business_value > income.business_value
    assert not any(path.eligible for path in row.paths if path.variant == "full_fy")


def test_missing_profit_operand_is_not_a_missing_ttm_interval():
    rows = documents(prior_quarter=True)
    rows[2] = replace(rows[2], profit=None)
    fixed = generation(rows)
    row = project(fixed)
    assert row.context["annual_observation"]["latest_ttm_period_status"] == "available"
    assert row.context["annual_observation"]["selected_variant"] == "annual_ttm"
    assert active(row, "earning_power") is None
    assert not any(path.eligible for path in row.paths if path.variant == "full_fy")


def test_old_jp_full_fy_cannot_bypass_new_ifrs_quarter():
    rows = documents()
    rows[2] = replace(rows[2], document_type="1QFinancialStatements_Consolidated_IFRS")
    row = project(generation(rows))
    assert row.context["annual_observation"]["latest_ttm_period_status"] == (
        "matching_period_not_observed"
    )
    assert row.context["annual_observation"]["selected_variant"] == "annual_ttm"
    assert not any(path.eligible for path in row.paths if path.variant == "full_fy")


def test_all_current_amounts_missing_does_not_hide_the_missing_interval():
    rows = documents()
    rows[2] = replace(rows[2], sales=None, operating_profit=None, profit=None)
    row = project(generation(rows))
    assert row.context["annual_observation"]["latest_ttm_period_status"] == (
        "matching_period_not_observed"
    )
    assert active(row, "earning_power").variant == "full_fy"


@pytest.mark.parametrize("bad_value", [-1.0, 0.0])
def test_observed_later_loss_or_zero_does_not_restore_old_fy_earning_power(bad_value):
    rows = documents()
    rows[2] = replace(rows[2], profit=bad_value)
    row = project(generation(rows))
    assert active(row, "earning_power") is None
    assert (
        row.context["annual_observation"]["latest_quarter_observation"]["metrics"]["parent_profit"][
            "value"
        ]
        == bad_value
    )


@pytest.mark.parametrize("bad_value", [-1.0, 0.0])
def test_known_quarter_deterioration_or_zero_cannot_escape_through_full_fy(bad_value):
    def update(payload):
        for change in payload["changes"]:
            if change["measurement"] == "quarter" and change["to_end"] == "2025-06-30":
                change["difference"] = bad_value
                # In particular, zero with a nonpositive base still has no growth rate.
                change["rate"] = None

    row = project(with_growth(generation(documents()), update))
    assert active(row, "business_expansion") is None
    assert active(row, "operating_recovery") is None
    # Earning power is not a growth claim: newer positive actuals remain relevant.
    assert active(row, "earning_power").variant == "full_fy"
    assert row.context["latest_growth_changes"]["sales"]["selected_variant"] == "quarter"
    assert row.context["latest_growth_changes"]["operating_profit"]["selected_variant"] == "quarter"


def test_positive_current_earning_power_does_not_require_growth_or_margin_improvement():
    def update(payload):
        for change in payload["changes"]:
            if change["measurement"] == "quarter" and change["to_end"] == "2025-06-30":
                if change["metric"] == "operating_margin":
                    change["difference"] = 0.0
                elif change["metric"] in {"operating_profit", "parent_profit"}:
                    change["difference"] = -1.0

    row = project(with_growth(generation(documents()), update))
    assert active(row, "earning_power").variant == "full_fy"
    assert active(row, "operating_recovery") is None
    assert (
        row.context["latest_growth_changes"]["operating_margin"]["latest_quarter"]["difference"]
        == 0.0
    )


@pytest.mark.parametrize("both", [False, True])
def test_nonconsolidated_profit_is_not_silently_parent_profit(both):
    rows = documents()
    rows = [
        replace(row, document_type="FinancialStatements_NonConsolidated_JP")
        if both or row.fiscal_period != "FY"
        else row
        for row in rows
    ]
    result = project(generation(rows))
    assert active(result, "earning_power") is None
    if both:
        assert active(result, "business_expansion").variant == "full_fy"
    else:
        assert not any(path.eligible for path in result.paths if path.variant == "full_fy")


def test_latest_fy_missing_parent_profit_does_not_use_eps_or_older_profit():
    rows = documents()
    rows[1] = replace(rows[1], profit=None)
    row = project(generation(rows))
    assert active(row, "earning_power") is None
    assert active(row, "capital_productivity") is None
    assert active(row, "business_expansion").variant == "full_fy"
    assert row.context["annual_observation"]["full_fy_period_end"] == "2025-03-31"


def test_changed_calendar_does_not_reuse_a_prior_fy_as_current_annual_basis():
    rows = documents()
    rows[2] = replace(rows[2], fiscal_year_end=date(2026, 4, 30))
    row = project(generation(rows))
    assert not any(path.eligible for path in row.paths if path.variant == "full_fy")


@pytest.mark.parametrize(
    ("fy_end", "quarter_end", "current_fy_end"),
    [
        (date(2025, 2, 15), date(2025, 5, 17), date(2026, 2, 14)),
        (date(2025, 2, 15), date(2025, 5, 17), date(2026, 2, 21)),
        (date(2025, 2, 22), date(2025, 5, 24), date(2026, 2, 21)),
    ],
)
def test_full_fy_fallback_accepts_adjacent_52_and_53_week_years(
    fy_end, quarter_end, current_fy_end
):
    rows = documents()
    rows[0] = replace(
        rows[0],
        period_start=date(2023, 2, 19),
        period_end=date(2024, 2, 17),
        fiscal_year_end=date(2024, 2, 17),
    )
    rows[1] = replace(
        rows[1], period_start=date(2024, 2, 18), period_end=fy_end, fiscal_year_end=fy_end
    )
    rows[2] = replace(
        rows[2],
        period_start=fy_end + timedelta(days=1),
        period_end=quarter_end,
        fiscal_year_end=current_fy_end,
    )
    result = project(generation(rows))
    income = active(result, "earning_power")
    assert income.variant == "full_fy"
    assert income.evidence["earnings_period_end"] == fy_end.isoformat()
    assert active(result, "business_expansion").variant == "full_fy"
    assert active(result, "operating_recovery").variant == "full_fy"


@pytest.mark.parametrize("start_shift", [-1, 1])
def test_full_fy_fallback_still_requires_exact_interval_adjacency(start_shift):
    rows = documents()
    rows[2] = replace(rows[2], period_start=date(2025, 4, 1) + timedelta(days=start_shift))
    result = project(generation(rows))
    assert not any(path.eligible for path in result.paths if path.variant == "full_fy")


def test_latest_short_fy_does_not_search_back_to_an_older_complete_year():
    rows = documents()
    rows[1] = replace(rows[1], period_end=date(2025, 2, 28), fiscal_year_end=date(2025, 2, 28))
    rows[2] = replace(rows[2], period_start=date(2025, 3, 1), fiscal_year_end=date(2026, 2, 28))
    row = project(generation(rows))
    assert row.context["annual_observation"]["full_fy_period_end"] is None
    assert not any(path.variant == "full_fy" for path in row.paths)


@pytest.mark.parametrize("known_by_as_of", [False, True])
@pytest.mark.parametrize("field", ["operating_profit", "profit"])
def test_new_fy_income_veto_uses_disclosure_cutoff_but_keeps_retrospective_context(
    known_by_as_of, field
):
    rows = documents()
    correction = replace(
        rows[2],
        disclosed_at=AS_OF if known_by_as_of else date(2025, 9, 1),
        disclosure_number="current-quarter-loss-correction",
        **{field: -1.0},
    )
    result = project(generation([*rows, correction]))
    income = next(
        path
        for path in result.paths
        if path.path_id == "earning_power" and path.variant == "full_fy"
    )
    assert income.eligible is (not known_by_as_of)
    assert income.evidence["newer_earning_weakness"] is known_by_as_of
    # This is a cutoff on the new FY veto, not a reconstruction of old growth states.
    metric = "parent_profit" if field == "profit" else "operating_profit"
    context = result.context["annual_observation"]
    for key in ("latest_reported_observation", "latest_quarter_observation"):
        value = context[key]["metrics"][metric]
        assert value["value"] == -1.0
        assert value["documents"][0]["disclosure_number"] == correction.disclosure_number
    assert active(result, "business_expansion").variant == "full_fy"
    assert active(result, "operating_recovery").variant == "full_fy"

    selection = select_economic_paths((result,), policy=EconomicPathPolicy())
    saved = TypeAdapter(EconomicPathSelection).dump_python(selection, mode="json")
    original = deepcopy(saved)
    card = economic_screening_brief(saved)["cards"][0]
    for key in ("latest_reported_observation", "latest_quarter_observation"):
        value = card["context"]["annual_observation"][key]["metrics"][metric]
        assert value == {
            "value": -1.0,
            "reason": None,
            "disclosure_dates": [correction.disclosed_at.isoformat()],
        }
    brief_income = next(
        (path for path in card["paths"] if path["path_id"] == "earning_power"), None
    )
    if known_by_as_of:
        assert brief_income is None
    else:
        assert brief_income["evidence"]["newer_earning_weakness"] is False
    assert saved == original


def test_quarter_and_ttm_keep_retrospective_corrections_without_a_disclosure_cutoff():
    rows = [*documents(prior_quarter=True), document(2023, "1Q", 300.0, 50.0, 40.0)]
    correction = replace(
        rows[2],
        disclosed_at=date(2025, 9, 1),
        disclosure_number="future-current-quarter-correction",
        sales=800.0,
        operating_profit=180.0,
        profit=200.0,
    )
    fixed = generation([*rows, correction])
    saved = canonical_json(fixed.model_dump(mode="json"))
    result = project(fixed)
    income = active(result, "earning_power")
    assert income.variant == "annual_ttm"
    # EarningsPrice retains its own as_of contract; growth retains the release's correction.
    assert income.price_value == pytest.approx(120.0 / 800.0)
    assert income.business_value == pytest.approx(260.0 / 1500.0)
    expansion = active(result, "business_expansion")
    assert expansion.variant == "quarter"
    assert expansion.price_value == pytest.approx(1500.0 / 800.0)
    assert expansion.business_value == pytest.approx(0.6)
    recovery = active(result, "operating_recovery")
    assert recovery.variant == "quarter"
    assert recovery.price_value == pytest.approx(80.0 / 800.0)
    assert recovery.business_value == pytest.approx(180.0 / 800.0 - 100.0 / 500.0)
    annual = next(
        path
        for path in result.paths
        if path.path_id == "business_expansion" and path.variant == "annual_ttm"
    )
    assert annual.business_value == pytest.approx(0.25)
    raw = fixed.result("company_growth", "1001").payload
    quarter = next(
        item for item in raw["periods"][-1]["observations"] if item["measurement"] == "quarter"
    )
    assert result.context["annual_observation"]["latest_quarter_observation"] == quarter
    assert expansion.evidence["change_observation"]["observation"] == quarter["metrics"]["sales"]
    assert quarter["metrics"]["sales"]["documents"][0]["disclosed_at"] == "2025-09-01"
    assert canonical_json(fixed.model_dump(mode="json")) == saved


def test_fy_reference_populations_include_companies_that_select_ttm():
    fallback = project(generation(documents()))
    ttm = replace(project(generation(documents(prior_quarter=True))), ticker="1002")
    selection = select_economic_paths((fallback, ttm), policy=EconomicPathPolicy())
    card = next(card for card in selection.cards if card.ticker == "1001")
    assert all(path.price_population == 2 and path.business_population == 2 for path in card.paths)


def test_legacy_growth_does_not_gain_an_inferred_interval_status():
    def update(payload):
        payload["calculation_method"] = "company_growth_retrospective_v3"
        for period in payload["periods"]:
            for observation in period["observations"]:
                observation.pop("period_status", None)
                assert "period_status" not in Observation.model_validate(observation).model_dump(
                    mode="json"
                )

    row = project(with_growth(generation(documents()), update))
    assert row.context["annual_observation"]["latest_ttm_period_status"] is None
    assert not any(path.eligible for path in row.paths if path.variant == "full_fy")


@pytest.mark.parametrize("corrected_index", [0, 1])
def test_future_fy_correction_is_context_not_a_coordinate_or_reference_vote(corrected_index):
    rows = documents()
    correction = replace(
        rows[corrected_index],
        disclosed_at=date(2025, 9, 1),
        disclosure_number="future-fy-correction",
        sales=1000000.0,
        operating_profit=900000.0,
        profit=900000.0,
    )
    future = project(generation([*rows, correction]))
    known = replace(project(generation(rows)), ticker="1002")
    fy_paths = [path for path in future.paths if path.variant == "full_fy"]
    if corrected_index == 1:
        assert all(path.price_value is None and path.business_value is None for path in fy_paths)
        assert not any(path.eligible for path in future.paths)
        # Keep the selected corrected FY, never fall back to the older pre-correction FY.
        saved = future.context["annual_observation"]["full_fy_observation"]
        assert saved["period_end"] == "2025-03-31"
        assert saved["metrics"]["parent_profit"]["value"] == 900000.0
    else:
        assert active(future, "earning_power") is not None  # This year's source is still known.
        assert active(future, "business_expansion") is None
        assert active(future, "operating_recovery") is None
    selection = select_economic_paths((future, known), policy=EconomicPathPolicy())
    card = next(card for card in selection.cards if card.ticker == "1002")
    paths = {path.path_id: path for path in card.paths}
    assert paths["business_expansion"].business_population == 1
    assert paths["operating_recovery"].price_population == 1
    assert paths["operating_recovery"].business_population == 1
    if corrected_index == 1:
        assert all(
            path.price_population == 1 and path.business_population == 1 for path in card.paths
        )


@pytest.mark.parametrize("future_profit", [True, False])
def test_future_fy_source_removes_only_its_axis_not_the_independent_reference(future_profit):
    rows = documents()
    correction = replace(
        rows[1],
        disclosed_at=date(2025, 9, 1),
        disclosure_number="future-partial-correction",
        sales=None if future_profit else 1000000.0,
        operating_profit=None if future_profit else 900000.0,
        profit=900000.0 if future_profit else None,
    )
    future = project(generation([*rows, correction]))
    income = next(
        path
        for path in future.paths
        if path.path_id == "earning_power" and path.variant == "full_fy"
    )
    assert (income.price_value is None) == future_profit
    assert (income.business_value is None) != future_profit
    assert not income.eligible
    known = replace(project(generation(rows)), ticker="1002")
    selection = select_economic_paths((future, known), policy=EconomicPathPolicy())
    card = next(card for card in selection.cards if card.ticker == "1002")
    income = next(path for path in card.paths if path.path_id == "earning_power")
    assert income.price_population == (1 if future_profit else 2)
    assert income.business_population == (2 if future_profit else 1)
