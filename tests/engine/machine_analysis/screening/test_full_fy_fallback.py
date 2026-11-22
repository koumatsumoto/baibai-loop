"""完全FYの発見経路は、利益の良し悪しでTTMから切り替えない。"""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta

import pytest
from tests.helpers.model_generation import materialize_generation, publish_generation

from baibai_engine.machine_analysis.generation.assembly import CompanyModelInputs, generate_models
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.query import read_research_model_inputs
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease, Observation
from baibai_engine.machine_analysis.models.company_growth.reading import (
    CompanyGrowthReader,
    metric_value,
)
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar
from baibai_engine.machine_analysis.screening.discovery_contract import discovery_publication_id
from baibai_engine.machine_analysis.screening.discovery_index import read_discovery_index
from baibai_engine.machine_analysis.screening.policies import economic_path_inputs
from baibai_engine.machine_analysis.screening.policies.discovery import (
    evaluate_discovery_rows,
    project_discovery_row,
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


def generation(rows, *, as_of=AS_OF, growth_start=None):
    release = InputRelease(release_id="fy-fixture-release", manifest_sha256="b" * 64)
    stream = generate_models(
        generation_id="fy-fixture-generation",
        as_of=as_of,
        evaluation_time=datetime.combine(
            max((as_of, *(row.disclosed_at for row in rows))) + timedelta(days=1), time.min, UTC
        ),
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
                adjusted_bars=(PriceBar(as_of, 10.0, None),),
                adjustment_events=(),
                sector="情報・通信業",
            ),
        ),
        trading_dates=(as_of,),
        expected_price_date=as_of,
        ttm_rules=DEFAULT_TTM_RULES,
        external_observations={},
        market_context=None,
        securities_context={"1001": {"master": {"sector_33": "情報・通信業"}}},
        market_context_revision="fy-fixture-v1",
        growth_start=growth_start,
    )
    return materialize_generation(stream)


def project(fixed):
    stock = tuple(m for m in fixed.models if m.subject == "1001")
    return project_discovery_row("1001", models=stock, metadata=fixed)


def growth_reader(fixed):
    return CompanyGrowthReader(fixed.result("company_growth", "1001").payload)


def latest(fixed, measurement):
    reader = growth_reader(fixed)
    return reader.observation(reader.latest_period, measurement)


def active(row, path_id):
    return next((path for path in row["coordinates"] if path["path_id"] == path_id), None)


def with_growth(fixed, update):
    models = []
    for model in fixed.models:
        if model.model_id == "company_growth":
            payload = deepcopy(model.payload)
            update(payload)
            model = model.model_copy(update={"payload": payload})
        models.append(model)
    return fixed.model_copy(update={"models": tuple(models)})


def without_latest_ttm_interval(fixed):
    """Control just the saved TTM interval; quarter amounts and comparisons are generated."""

    def update(payload):
        state = payload["periods"][-1]
        annual = next(item for item in state["observations"] if item["measurement"] == "annual_ttm")
        assert annual["period_status"] == "available"
        annual["period_status"] = "matching_period_not_observed"
        annual["metrics"] = {
            metric: {"value": None, "reason": "matching_period_not_observed", "documents": []}
            for metric in annual["metrics"]
        }
        payload["changes"] = [
            item
            for item in payload["changes"]
            if item["measurement"] != "annual_ttm" or item["target_cohort"] != state["cohort"]
        ]

    return with_growth(fixed, update)


def test_missing_ttm_uses_same_complete_fy_and_preserves_short_history_comparison(tmp_path):
    fixed = generation(documents())
    path = tmp_path / "runs.sqlite"
    publish_generation(fixed, path=path)
    stock = tuple(m for m in fixed.models if m.subject == "1001")
    row = project_discovery_row("1001", models=stock, metadata=fixed)
    selection = evaluate_discovery_rows(fixed, tickers=("1001",), rows={"1001": row})
    identity = discovery_publication_id(fixed.generation_id)
    store = ScreeningSelectionStore(path)
    assert store.publish(identity, selection)
    assert not store.publish(identity, selection)
    (recorded,) = read_discovery_index(path=path, selection_id=identity).rows
    paths = {item["path_id"]: item for item in recorded["coordinates"]}
    assert set(paths) == {"earning_power", "business_expansion", "operating_recovery"}
    assert all(item["variant"] == "full_fy" for item in paths.values())
    assert paths["earning_power"]["price_value"] == pytest.approx(150.0 / 800.0)
    assert paths["earning_power"]["business_value"] == pytest.approx(180.0 / 1200.0)
    assert paths["business_expansion"]["business_value"] == pytest.approx(0.2)
    assert paths["operating_recovery"]["price_value"] == pytest.approx(80.0 / 800.0)

    saved = read_research_model_inputs(fixed.generation_id, targets=("1001",), path=path)
    source = growth_reader(saved)
    fy = source.latest_full_fy
    assert (fy.period_start, fy.period_end) == (date(2024, 4, 1), date(2025, 3, 31))
    profit = fy.metrics["parent_profit"]
    assert profit.value == 150.0
    assert profit.documents[0].disclosure_number == "fy-fixture-2024-FY"
    assert (fixed.as_of - fy.period_end).days == 153
    assert (
        source.observation(source.latest_period, "annual_ttm").period_status
        == "matching_period_not_observed"
    )
    quarter = source.observation(source.latest_period, "quarter")
    assert (quarter.period_end, quarter.metrics["parent_profit"].value) == (date(2025, 6, 30), 70.0)


def test_available_ttm_never_chooses_the_more_attractive_fy():
    fixed = generation(documents(prior_quarter=True))
    row = project(fixed)
    income = active(row, "earning_power")
    assert income["variant"] == "annual_ttm"
    assert income["price_value"] == pytest.approx(120.0 / 800.0)
    fy = growth_reader(fixed).latest_full_fy
    assert fy.metrics["parent_profit"].value / 800.0 > income["price_value"]
    assert fy.metrics["operating_margin"].value > income["business_value"]
    assert not any(path["variant"] == "full_fy" for path in row["coordinates"])


def test_missing_profit_operand_is_not_a_missing_ttm_interval():
    rows = documents(prior_quarter=True)
    rows[2] = replace(rows[2], profit=None)
    fixed = generation(rows)
    row = project(fixed)
    ttm = latest(fixed, "annual_ttm")
    assert ttm.period_status == "available"
    assert ttm.metrics["parent_profit"].value is None
    assert active(row, "earning_power") is None
    assert not any(path["variant"] == "full_fy" for path in row["coordinates"])


def test_old_jp_full_fy_cannot_bypass_new_ifrs_quarter():
    rows = documents()
    rows[2] = replace(rows[2], document_type="1QFinancialStatements_Consolidated_IFRS")
    fixed = generation(rows)
    row = project(fixed)
    ttm = latest(fixed, "annual_ttm")
    assert ttm.period_status == "matching_period_not_observed"
    assert ttm.accounting_standard == "IFRS"
    assert growth_reader(fixed).latest_full_fy.accounting_standard == "JP"
    assert not any(path["variant"] == "full_fy" for path in row["coordinates"])


def test_all_current_amounts_missing_does_not_hide_the_missing_interval():
    rows = documents()
    rows[2] = replace(rows[2], sales=None, operating_profit=None, profit=None)
    fixed = generation(rows)
    row = project(fixed)
    assert latest(fixed, "annual_ttm").period_status == "matching_period_not_observed"
    assert latest(fixed, "quarter").metrics["parent_profit"].value is None
    assert active(row, "earning_power")["variant"] == "full_fy"


@pytest.mark.parametrize("bad_value", [-1.0, 0.0])
def test_observed_later_loss_or_zero_does_not_restore_old_fy_earning_power(bad_value):
    rows = documents()
    rows[2] = replace(rows[2], profit=bad_value)
    fixed = generation(rows)
    assert active(project(fixed), "earning_power") is None
    assert latest(fixed, "quarter").metrics["parent_profit"].value == bad_value


@pytest.mark.parametrize("bad_value", [-1.0, 0.0])
def test_known_quarter_deterioration_or_zero_cannot_escape_through_full_fy(bad_value):
    rows = documents()
    rows[-1] = replace(rows[-1], sales=bad_value)
    prior = document(2024, "1Q", 0.0, 80.0 - bad_value, 70.0)
    fixed = without_latest_ttm_interval(generation([*rows, prior]))
    row = project(fixed)
    assert active(row, "business_expansion") is None
    assert active(row, "operating_recovery") is None
    # Earning power is not a growth claim: newer positive actuals remain relevant.
    assert active(row, "earning_power")["variant"] == "full_fy"
    reader = growth_reader(fixed)
    quarter = reader.observation(reader.latest_period, "quarter")
    for metric in ("sales", "operating_profit"):
        (change,) = reader.year_over_year(quarter, metric)
        (source,) = reader.comparison_prior(quarter, change)
        assert source.metrics[metric].documents[0].disclosure_number == prior.disclosure_number
        assert change.difference == bad_value
        if metric == "sales":
            assert change.rate is None  # Numeric decline/zero and unavailable rate can coexist.


def test_positive_current_earning_power_does_not_require_growth_or_margin_improvement():
    prior = document(2024, "1Q", 405.0, 81.0, 71.0)
    fixed = without_latest_ttm_interval(generation([*documents(), prior]))
    row = project(fixed)
    assert active(row, "earning_power")["variant"] == "full_fy"
    assert active(row, "operating_recovery") is None
    reader = growth_reader(fixed)
    quarter = reader.observation(reader.latest_period, "quarter")
    (change,) = reader.year_over_year(quarter, "operating_margin")
    (prior_observation,) = reader.comparison_prior(quarter, change)
    assert (
        prior_observation.metrics["sales"].documents[0].disclosure_number == prior.disclosure_number
    )
    assert change.difference == 0.0


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
        assert active(result, "business_expansion")["variant"] == "full_fy"
    else:
        assert not any(path["variant"] == "full_fy" for path in result["coordinates"])


def test_latest_fy_missing_parent_profit_does_not_use_eps_or_older_profit():
    rows = documents()
    rows[1] = replace(rows[1], profit=None)
    fixed = generation(rows)
    row = project(fixed)
    assert active(row, "earning_power") is None
    assert active(row, "capital_productivity") is None
    assert active(row, "business_expansion")["variant"] == "full_fy"
    reader = growth_reader(fixed)
    fy = reader.latest_full_fy
    assert fy.period_end == date(2025, 3, 31)
    assert fy.metrics["parent_profit"].value is None
    (year,) = (s for s in reader.model.periods if s.period_end == fy.period_end)
    assert reader.observation(year, "reported").metrics["reported_basic_eps"].value == 999.0


def test_bounded_fy_read_keeps_exact_prior_source_for_the_cutoff(monkeypatch):
    def update(payload):
        source = payload["periods"][1]
        sibling = deepcopy(source)
        sibling["fiscal_year_end"] = "2024-06-30"
        sibling["cohort"][2] = "2024-06-30"
        for item in source["observations"]:
            if item["measurement"] == "annual_ttm":
                item["metrics"]["sales"]["documents"][0]["disclosed_at"] = "2025-09-01"
        payload["periods"].insert(2, sibling)

    fixed = with_growth(generation([document(2020, "FY", 500.0, 50.0, 40.0), *documents()]), update)
    payload = fixed.result("company_growth", "1001").payload
    before = canonical_json(payload)
    bounded = economic_path_inputs._growth_policy_inputs(payload)
    assert [item["period_end"] for item in bounded["periods"]] == [
        "2024-03-31",
        "2025-03-31",
        "2025-06-30",
    ]
    row = project(fixed)
    reader = growth_reader(fixed)
    fy = reader.latest_full_fy
    (change,) = reader.year_over_year(fy, "sales")
    (prior,) = reader.comparison_prior(fy, change)
    assert any(doc.disclosed_at > fixed.as_of for doc in prior.metrics["sales"].documents)
    assert active(row, "business_expansion") is None
    assert active(row, "earning_power") is not None
    monkeypatch.setattr(economic_path_inputs, "_growth_policy_inputs", lambda payload: payload)
    assert project(fixed) == row
    assert canonical_json(payload) == before


def test_changed_calendar_does_not_reuse_a_prior_fy_as_current_annual_basis():
    rows = documents()
    rows[2] = replace(rows[2], fiscal_year_end=date(2026, 4, 30))
    row = project(generation(rows))
    assert not any(path["variant"] == "full_fy" for path in row["coordinates"])


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
    fixed = generation(rows)
    result = project(fixed)
    income = active(result, "earning_power")
    assert income["variant"] == "full_fy"
    assert growth_reader(fixed).latest_full_fy.period_end == fy_end
    assert active(result, "business_expansion")["variant"] == "full_fy"
    assert active(result, "operating_recovery")["variant"] == "full_fy"


@pytest.mark.parametrize("start_shift", [-1, 1])
def test_full_fy_fallback_still_requires_exact_interval_adjacency(start_shift):
    rows = documents()
    rows[2] = replace(rows[2], period_start=date(2025, 4, 1) + timedelta(days=start_shift))
    result = project(generation(rows))
    assert not any(path["variant"] == "full_fy" for path in result["coordinates"])


def test_latest_short_fy_does_not_search_back_to_an_older_complete_year():
    rows = documents()
    rows[1] = replace(rows[1], period_end=date(2025, 2, 28), fiscal_year_end=date(2025, 2, 28))
    rows[2] = replace(rows[2], period_start=date(2025, 3, 1), fiscal_year_end=date(2026, 2, 28))
    fixed = generation(rows)
    row = project(fixed)
    assert growth_reader(fixed).latest_full_fy is None
    assert not any(path["variant"] == "full_fy" for path in row["coordinates"])


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
        sales=None,
        operating_profit=-1.0 if field == "operating_profit" else None,
        profit=-1.0 if field == "profit" else None,
    )
    fixed = generation([*rows, correction])
    result = project(fixed)
    assert active(result, "earning_power") is None
    # A known loss vetoes. A later revision obscures, but does not establish a known loss.
    # The retained release can still report the correction and its exact source date.
    metric = "parent_profit" if field == "profit" else "operating_profit"
    reader = growth_reader(fixed)
    for measurement in ("reported", "quarter"):
        value = reader.observation(reader.latest_period, measurement).metrics[metric]
        assert value.value == -1.0
        assert value.reason is None
        (document,) = value.documents
        assert document.disclosure_number == correction.disclosure_number
        assert document.disclosed_at == correction.disclosed_at
    assert active(result, "business_expansion")["variant"] == "full_fy"
    recovery = active(result, "operating_recovery")
    if not known_by_as_of and field == "operating_profit":
        assert recovery is None
    else:
        assert recovery["variant"] == "full_fy"


@pytest.mark.parametrize("by_as_of", [False, True])
def test_quarter_and_ttm_keep_retrospective_corrections_but_do_not_publish_mixed_coordinates(
    by_as_of,
):
    rows = [*documents(prior_quarter=True), document(2023, "1Q", 300.0, 50.0, 40.0)]
    correction = replace(
        rows[2],
        disclosed_at=AS_OF if by_as_of else AS_OF + timedelta(days=1),
        disclosure_number="future-current-quarter-correction",
        sales=800.0,
        operating_profit=180.0,
        profit=200.0,
    )
    fixed = generation([*rows, correction])
    saved = canonical_json(fixed.model_dump(mode="json"))
    result = project(fixed)
    earnings = fixed.result("company_financials", "1001").payload["earnings_price"]
    if by_as_of:
        assert earnings["earnings_yield"] == pytest.approx(250.0 / 800.0)
        assert active(result, "earning_power")["business_value"] == pytest.approx(260.0 / 1500.0)
        assert active(result, "business_expansion")["business_value"] == pytest.approx(0.6)
        assert active(result, "operating_recovery")["price_value"] == pytest.approx(80.0 / 800.0)
    else:
        # Independent book/profit still uses only the as-of financial owner.
        assert {p["path_id"] for p in result["coordinates"]} == {"capital_productivity"}
        assert earnings["earnings_yield"] == pytest.approx(120.0 / 800.0)
    reader = growth_reader(fixed)
    (annual_sales,) = reader.year_over_year(
        reader.observation(reader.latest_period, "annual_ttm"), "sales"
    )
    assert annual_sales.rate == pytest.approx(0.25)
    raw = fixed.result("company_growth", "1001").payload
    quarter = next(
        item for item in raw["periods"][-1]["observations"] if item["measurement"] == "quarter"
    )
    assert quarter["metrics"]["sales"]["value"] == 800.0
    assert (
        quarter["metrics"]["sales"]["documents"][0]["disclosure_number"]
        == correction.disclosure_number
    )
    assert (
        quarter["metrics"]["sales"]["documents"][0]["disclosed_at"]
        == correction.disclosed_at.isoformat()
    )
    assert canonical_json(fixed.model_dump(mode="json")) == saved


def test_legacy_growth_does_not_gain_an_inferred_interval_status():
    def update(payload):
        payload["calculation_method"] = "company_growth_retrospective_v3"
        for period in payload["periods"]:
            for observation in period["observations"]:
                observation.pop("period_status", None)
                assert "period_status" not in Observation.model_validate(observation).model_dump(
                    mode="json"
                )

    fixed = with_growth(generation(documents()), update)
    row = project(fixed)
    assert latest(fixed, "annual_ttm").period_status is None
    assert not any(path["variant"] == "full_fy" for path in row["coordinates"])


@pytest.mark.parametrize("corrected_index", [0, 1])
def test_future_fy_correction_is_context_not_a_coordinate(corrected_index):
    rows = documents()
    correction = replace(
        rows[corrected_index],
        disclosed_at=date(2025, 9, 1),
        disclosure_number="future-fy-correction",
        sales=1000000.0,
        operating_profit=900000.0,
        profit=900000.0,
    )
    fixed = generation([*rows, correction])
    future = project(fixed)
    if corrected_index == 1:
        assert future["coordinates"] == []
        # Keep the selected corrected FY, never fall back to the older pre-correction FY.
        fy = growth_reader(fixed).latest_full_fy
        assert fy.period_end == date(2025, 3, 31)
        value = fy.metrics["parent_profit"]
        assert value.value == 900000.0
        assert value.documents[0].disclosed_at > fixed.as_of
    else:
        assert active(future, "earning_power") is not None  # This year's source is still known.
        assert active(future, "business_expansion") is None
        assert active(future, "operating_recovery") is None


@pytest.mark.parametrize("future_profit", [True, False])
def test_future_fy_source_cannot_publish_earning_power_with_only_one_observable_axis(future_profit):
    rows = documents()
    correction = replace(
        rows[1],
        disclosed_at=date(2025, 9, 1),
        disclosure_number="future-partial-correction",
        sales=None if future_profit else 1000000.0,
        operating_profit=None if future_profit else 900000.0,
        profit=900000.0 if future_profit else None,
    )
    fixed = generation([*rows, correction])
    future = project(fixed)
    assert active(future, "earning_power") is None
    fy = growth_reader(fixed).latest_full_fy
    assert (metric_value(fy, "parent_profit", disclosed_by=fixed.as_of) is None) == future_profit
    assert (metric_value(fy, "operating_margin", disclosed_by=fixed.as_of) is None) != future_profit


@pytest.mark.parametrize("original", [-1.0, 0.0, None])
def test_later_correction_does_not_treat_unknown_q_as_no_adverse_evidence(original):
    rows = documents()
    rows[2] = replace(rows[2], operating_profit=original)
    known = generation(rows)
    assert (active(project(known), "earning_power") is None) is (original is not None)
    later = replace(
        rows[2],
        disclosed_at=AS_OF + timedelta(days=1),
        operating_profit=80,
        disclosure_number="later-positive-q-revision",
    )
    fixed = generation([*rows, later])
    report = growth_reader(fixed)
    quarter = report.observation(report.latest_period, "quarter")
    assert metric_value(quarter, "operating_profit") == 80
    assert metric_value(quarter, "operating_profit", disclosed_by=AS_OF) is None
    assert active(project(fixed), "earning_power") is None


def test_q_conflict_does_not_restore_fy_and_unrelated_operands_remain_eligible():
    rows = documents()
    rows[2] = replace(rows[2], operating_profit=-1, disclosed_time="15:30:00")
    conflict = replace(rows[2], operating_profit=80, disclosure_number="same-time-contradiction")
    fixed = generation([*rows, conflict])
    assert latest(fixed, "quarter").metrics["operating_profit"].reason == "conflicting_revisions"
    result = project(fixed)
    assert active(result, "earning_power") is None
    assert active(result, "operating_recovery") is None
    assert active(result, "business_expansion")["variant"] == "full_fy"


def test_prior_quarter_revision_does_not_turn_late_comparison_into_annual_fallback():
    rows = [*documents(prior_quarter=True), document(2023, "1Q", 300.0, 50.0, 40.0)]
    rows[2] = replace(rows[2], sales=650.0, operating_profit=130.0)
    later = replace(
        rows[3],
        disclosed_at=AS_OF + timedelta(days=1),
        sales=450.0,
        operating_profit=70.0,
        disclosure_number="later-prior-q-revision",
    )
    fixed = generation([*rows, later])
    result = project(fixed)
    reader = growth_reader(fixed)
    quarter = reader.observation(reader.latest_period, "quarter")
    (sales,) = reader.year_over_year(quarter, "sales")
    assert sales.rate is not None
    assert sales.rate > 0
    assert (
        reader.comparison_prior(quarter, sales)[0].metrics["sales"].documents[0].disclosed_at
        > AS_OF
    )
    assert active(result, "business_expansion") is None
    assert active(result, "operating_recovery") is None


@pytest.mark.parametrize(
    ("through", "reason"),
    [
        (date(2025, 12, 1), "inconsistent_cumulative_revision"),
        (date(2025, 11, 14), "cumulative_revision_order_unresolved"),
    ],
)
def test_cumulative_revision_can_erase_a_known_q_loss_without_a_number_or_clock(through, reason):
    as_of = date(2025, 11, 28)
    rows = documents()
    rows[2] = replace(rows[2], operating_profit=100, profit=20)
    q2 = replace(
        rows[2],
        fiscal_period="2Q",
        document_type="2QFinancialStatements_Consolidated_JP",
        period_end=date(2025, 9, 30),
        disclosed_at=date(2025, 11, 14),
        disclosed_time="15:00:00",
        disclosure_number="same-original-midyear",
        sales=700,
        operating_profit=80,
        profit=70,
    )
    original = generation([*rows, q2], as_of=as_of)
    assert metric_value(latest(original, "quarter"), "operating_profit", disclosed_by=as_of) == -20
    assert active(project(original), "earning_power") is None
    correction = replace(
        rows[2],
        disclosed_at=through,
        disclosure_number="restated-q1-without-time",
        sales=None,
        operating_profit=200,
        profit=None,
    )
    fixed = generation([*rows, q2, correction], as_of=as_of)
    quarterly = latest(fixed, "quarter").metrics["operating_profit"]
    assert quarterly.value is None
    assert not quarterly.documents
    assert quarterly.reason == reason
    # The surviving midyear cumulative profit cannot answer the adverse-quarter test.
    assert latest(fixed, "reported").metrics["operating_profit"].value == 80
    assert latest(fixed, "annual_ttm").period_status == "matching_period_not_observed"
    result = project(fixed)
    assert active(result, "earning_power") is None
    assert active(result, "operating_recovery") is None
    assert active(result, "business_expansion")["variant"] == "full_fy"


def _cumulative_q2(year, sales, operating_profit, parent_profit):
    return replace(
        document(year, "1Q", sales, operating_profit, parent_profit),
        fiscal_period="2Q",
        document_type="2QFinancialStatements_Consolidated_JP",
        period_end=date(year, 9, 30),
        disclosed_at=date(year, 11, 10),
        disclosed_time="15:30:00",
        disclosure_number=f"cumulative-h1-{year}",
    )


def _late_q1_revisions(q1, cutoff, *, only_sales=False):
    first = replace(
        q1,
        disclosed_at=cutoff + timedelta(days=3),
        disclosed_time="15:30:00",
        disclosure_number=f"{q1.disclosure_number}-late-a",
        sales=100.0 if only_sales else None,
        operating_profit=None if only_sales else 5.0,
        profit=None,
    )
    second = replace(
        first,
        disclosure_number=f"{q1.disclosure_number}-late-b",
        sales=380.0 if only_sales else None,
        operating_profit=None if only_sales else 100.0,
    )
    return first, second


@pytest.mark.parametrize(
    "prior_q1",
    [
        "clean",
        "late",
        "conflict",
        "only_sales",
        "absent",
        "unreported",
        "unrelated_conflict",
        "conflict_outside_saved_window",
        "absent_outside_saved_window",
    ],
)
def test_matching_prior_quarter_uncertainty_cannot_restore_annual_routes(monkeypatch, prior_q1):
    cutoff = date(2025, 11, 28)
    reports = [
        document(2023, "FY", 1000.0, 100.0, 80.0),
        document(2024, "FY", 1600.0, 350.0, 300.0),
        document(2023, "1Q", 260.0, 20.0, 15.0),
        document(2025, "1Q", 450.0, 120.0, 100.0),
        _cumulative_q2(2023, 520.0, 40.0, 35.0),
        _cumulative_q2(2024, 700.0, 140.0, 110.0),
        _cumulative_q2(2025, 800.0, 190.0, 160.0),
    ]
    native_prior = document(2024, "1Q", 300.0, 40.0, 30.0)
    if prior_q1 in {"unreported", "unrelated_conflict"}:
        reports.append(replace(native_prior, sales=None, operating_profit=None))
        if prior_q1 == "unrelated_conflict":
            decoy = replace(native_prior, fiscal_year_end=date(2025, 4, 30))
            reports.extend(_late_q1_revisions(decoy, cutoff))
    elif not prior_q1.startswith("absent"):
        reports.append(native_prior)
        if prior_q1 in {"late", "conflict", "only_sales", "conflict_outside_saved_window"}:
            revisions = _late_q1_revisions(
                native_prior, cutoff, only_sales=prior_q1 == "only_sales"
            )
            reports.extend(revisions[:1] if prior_q1 == "late" else revisions)

    growth_start = date(2024, 7, 1) if prior_q1.endswith("outside_saved_window") else None
    fixed = generation(reports, as_of=cutoff, growth_start=growth_start)
    reader = growth_reader(fixed)
    quarter = reader.observation(reader.latest_period, "quarter")
    (change,) = reader.year_over_year(quarter, "operating_margin")
    if prior_q1 == "clean":
        assert change.difference == pytest.approx(-0.05)
    else:
        assert change.difference is None
    if prior_q1 in {"late", "conflict", "only_sales", "unreported", "unrelated_conflict"}:
        (source,) = reader.comparison_prior(quarter, change)
        assert source.metrics["operating_margin"].reason == "operand_unavailable"
        assert source.period_start == date(2024, 7, 1)

    raw = fixed.result("company_growth", "1001").payload
    unchanged = canonical_json(raw)
    bounded = economic_path_inputs._growth_policy_inputs(raw)
    result = project(fixed)
    if prior_q1 == "conflict":
        predecessor = [item for item in bounded["periods"] if item["period_end"] == "2024-06-30"]
        assert {item["fiscal_year_end"] for item in predecessor} == {"2025-03-31"}
    assert active(result, "earning_power")["variant"] == "annual_ttm"
    assert active(result, "capital_productivity") is not None
    for route in ("business_expansion", "operating_recovery"):
        row = active(result, route)
        if prior_q1 in {
            "absent",
            "absent_outside_saved_window",
            "unreported",
            "unrelated_conflict",
        }:
            assert row["variant"] == "annual_ttm"
        else:
            assert row is None
    monkeypatch.setattr(economic_path_inputs, "_growth_policy_inputs", lambda payload: payload)
    assert project(fixed) == result
    assert canonical_json(raw) == unchanged


@pytest.mark.parametrize("current_q1", ["clean", "late", "conflict", "missing_op"])
def test_current_cumulative_operand_conflict_cannot_restore_fy_earning_power(current_q1):
    cutoff = date(2025, 11, 28)
    q1 = document(2025, "1Q", 450.0, 100.0, 20.0)
    if current_q1 == "missing_op":
        q1 = replace(q1, operating_profit=None)
    reports = [
        document(2023, "FY", 1000.0, 100.0, 80.0),
        document(2024, "FY", 1600.0, 350.0, 300.0),
        q1,
        _cumulative_q2(2025, 700.0, 80.0, 70.0),
    ]
    if current_q1 in {"late", "conflict"}:
        revisions = _late_q1_revisions(q1, cutoff)
        reports.extend(revisions[:1] if current_q1 == "late" else revisions)
    fixed = generation(reports, as_of=cutoff)
    assert latest(fixed, "annual_ttm").period_status == "matching_period_not_observed"
    quarter_op = latest(fixed, "quarter").metrics["operating_profit"]
    if current_q1 == "clean":
        assert quarter_op.value == -20
    else:
        assert quarter_op.value is None
    if current_q1 in {"conflict", "missing_op"}:
        assert quarter_op.reason == "operand_unavailable"
    earning_power = active(project(fixed), "earning_power")
    if current_q1 == "missing_op":
        assert earning_power["variant"] == "full_fy"
    else:
        assert earning_power is None
