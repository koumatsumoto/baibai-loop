from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256

import pytest
from pydantic import TypeAdapter
from tests.helpers.model_generation import materialize_generation, publish_generation

from baibai_engine.investment_judgment.workflow.screening_brief import economic_screening_brief
from baibai_engine.machine_analysis.generation.assembly import CompanyModelInputs, generate_models
from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationMetadata,
    GenerationSubject,
    InputSnapshot,
    ModelGeneration,
)
from baibai_engine.machine_analysis.models.company_financials.earnings import (
    EarningsPrice,
    ProfitComponent,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import TTMRules
from baibai_engine.machine_analysis.models.company_growth.models import (
    Change,
    CompanyGrowth,
    InputRelease,
    Observation,
    PeriodState,
    Value,
)
from baibai_engine.machine_analysis.models.market_context.security import (
    calculate_security_market_context,
)
from baibai_engine.machine_analysis.models.technical_analysis.generation import (
    generate_technical_analysis,
)
from baibai_engine.machine_analysis.models.technical_analysis.price_path import observe_price_path
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar
from baibai_engine.machine_analysis.observations.prices.price_observation import price_calendar
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.policies import economic_path_inputs
from baibai_engine.machine_analysis.screening.policies.economic_path_inputs import (
    project_company,
)
from baibai_engine.machine_analysis.screening.policies.economic_path_selection import (
    evaluate_economic_coordinates,
)
from baibai_engine.machine_analysis.screening.policies.economic_paths import (
    EconomicPathPolicy,
    EconomicPathSelection,
    select_economic_paths,
)
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.primitives.json import canonical_json


def metadata():
    return GenerationMetadata(
        generation_id="economic-fixed",
        as_of=date(2026, 1, 5),
        evaluation_time=datetime(2026, 1, 5, 12, tzinfo=UTC),
        inputs={"market": InputSnapshot(kind="lake_release", reference="fixed", sha256="a" * 64)},
    )


def profit_component(coefficient, amount, start, end):
    return ProfitComponent(
        coefficient=coefficient,
        amount_yen=amount,
        period_start=date.fromisoformat(start),
        period_end=date.fromisoformat(end),
        disclosed_at=date(2026, 1, 5),
        disclosed_time=None,
        disclosure_number="synthetic-profit",
        document_type="FinancialStatements_Consolidated_JP",
    )


def payloads(price=100.0):
    cap = price * 100

    def observation(kind, start):
        return Observation(
            measurement=kind,
            period_start=date.fromisoformat(start),
            period_end=date(2025, 12, 31),
            statement_basis="consolidated",
            accounting_standard="JP",
            metrics={
                "sales": Value(value=1000.0),
                "operating_profit": Value(value=100.0),
                "operating_margin": Value(value=0.1),
            },
        )

    changes = []
    for kind, start, prior in [
        ("quarter", "2025-10-01", "2024-10-01"),
        ("annual_ttm", "2025-01-01", "2024-01-01"),
    ]:
        for metric, difference, rate in [
            ("sales", 100.0, 0.2),
            ("operating_profit", 10.0, 0.1),
            ("operating_margin", 0.02, None),
        ]:
            changes.append(
                Change(
                    measurement=kind,
                    metric=metric,
                    from_start=date.fromisoformat(prior),
                    from_end=date(2024, 12, 31),
                    to_start=date.fromisoformat(start),
                    to_end=date(2025, 12, 31),
                    difference=difference,
                    rate=rate,
                )
            )
    return {
        "company_financials": {
            "expected_price_date": "2026-01-05",
            "earnings_price": TypeAdapter(EarningsPrice).dump_python(
                EarningsPrice(
                    as_of=date(2026, 1, 5),
                    earnings_yield=1000.0 / cap,
                    price_date=date(2026, 1, 5),
                    price_yen=price,
                    shares_ex_treasury=100.0,
                    market_cap_yen=cap,
                    profit_ttm_yen=1000.0,
                    statement_basis="consolidated",
                    accounting_standard="JP",
                    profit_components=(profit_component(1, 1000.0, "2025-01-01", "2025-12-31"),),
                    unavailable_reason=None,
                ),
                mode="json",
            ),
            "financial": {"market_cap": cap, "pbr": cap / 20000.0, "fcf_ttm": -200.0},
        },
        "company_growth": CompanyGrowth(
            ticker="1234",
            status="available",
            requested_start=None,
            requested_end=date(2026, 1, 5),
            periods=(
                PeriodState(
                    fiscal_period="FY",
                    fiscal_year_end=date(2025, 12, 31),
                    period_start=date(2025, 1, 1),
                    period_end=date(2025, 12, 31),
                    observations=(
                        observation("annual_ttm", "2025-01-01"),
                        observation("quarter", "2025-10-01"),
                    ),
                ),
            ),
            changes=tuple(changes),
        ).model_dump(mode="json"),
        "market_context": {"master": {"sector_33": "情報・通信業", "name": "合成企業"}},
    }


def models(data):
    return tuple(
        GeneratedModel(model_id=key, subject="1234", calculation_revision="v1", payload=value)
        for key, value in data.items()
    )


def project(data):
    return project_company("1234", models=models(data), metadata=metadata())


def active(row, name):
    return next((path for path in row.paths if path.path_id == name and path.eligible), None)


def test_valid_paths_use_fixed_observations_and_periods():
    result = project(payloads())
    assert {path.path_id for path in result.paths if path.eligible} == {
        "earning_power",
        "business_expansion",
        "operating_recovery",
        "capital_productivity",
    }
    assert active(result, "earning_power").business_value == 0.1
    assert active(result, "business_expansion").variant == "quarter"
    assert active(result, "operating_recovery").business_value == 0.02


def test_growth_policy_read_bounds_history_without_changing_evidence_or_saved_payload(monkeypatch):
    data = payloads()
    growth = data["company_growth"]
    latest = growth["periods"][-1]
    history = []
    for year in (2022, 2024, 2024):
        state = deepcopy(latest)
        for key in ("fiscal_year_end", "period_start", "period_end"):
            state[key] = state[key].replace("2025", str(year))
        for item in state["observations"]:
            for key in ("period_start", "period_end"):
                item[key] = item[key].replace("2025", str(year))
        history.append(state)
    history[-1]["fiscal_year_end"] = "2025-03-31"
    growth["periods"] = [*history, latest]
    old_changes = deepcopy(growth["changes"])
    for change in old_changes:
        change["to_start"] = change["to_start"].replace("2025", "2022")
        change["to_end"] = change["to_end"].replace("2025", "2022")
    duplicate = deepcopy(growth["changes"][1])
    duplicate.pop("margin_points")
    growth["changes"] = [*old_changes, *growth["changes"], duplicate]
    before = canonical_json(data)

    bounded = economic_path_inputs._growth_policy_inputs(growth)
    assert bounded["periods"] == [*history[1:], latest]
    assert len(bounded["changes"]) == 7
    assert bounded["changes"][-1] is duplicate
    assert all(
        bounded[key] is value for key, value in growth.items() if key not in {"periods", "changes"}
    )
    result = project(data)
    assert (
        result.context["latest_growth_changes"]["operating_profit"]["latest_quarter"]["reason"]
        == "ambiguous_growth_comparison"
    )
    monkeypatch.setattr(economic_path_inputs, "_growth_policy_inputs", lambda payload: payload)
    assert project(data) == result
    assert canonical_json(data) == before


def test_projected_observation_standard_reaches_the_bounded_brief():
    projected = project(payloads())
    selection = select_economic_paths((projected,), policy=EconomicPathPolicy())
    saved = TypeAdapter(EconomicPathSelection).dump_python(selection, mode="json")
    brief = economic_screening_brief(saved)
    assert any(
        observation["accounting_standard"] == "JP"
        for path in brief["cards"][0]["paths"]
        for observation in path["evidence"].get("period_basis", [])
    )


def test_turnover_window_reaches_selection_and_brief_without_changing_candidate_paths():
    data = payloads()
    baseline = project(data)
    window = {
        "start_date": "2025-11-20",
        "end_date": "2026-01-05",
        "price_bar_count": 20,
        "valid_turnover_count": 17,
        "median_yen": 100000.0,
    }
    data["market_context"]["price"] = {"turnover_window": window}
    original = deepcopy(data)
    projected = project(data)
    assert projected.paths == baseline.paths
    selected = select_economic_paths((projected,), policy=EconomicPathPolicy())
    saved = TypeAdapter(EconomicPathSelection).dump_python(selected, mode="json")
    assert saved["cards"][0]["context"]["turnover_window"] == window
    assert economic_screening_brief(saved)["cards"][0]["context"]["turnover_window"] == window
    assert data == original


def test_old_model_does_not_gain_a_turnover_window_or_change_selection_hash():
    selected = select_economic_paths((project(payloads()),), policy=EconomicPathPolicy())
    saved = TypeAdapter(EconomicPathSelection).dump_python(selected, mode="json")
    assert "turnover_window" not in saved["cards"][0]["context"]
    # Captured from the same fixture on main 4ed88898, before window projection.
    assert sha256(canonical_json(saved).encode()).hexdigest() == (
        "31dfe3c282d5aced68d54872768986538e0d4a3b358213529e9297c82c78adeb"
    )
    assert "moving_averages" not in saved["cards"][0]["context"]
    assert "price_path" not in saved["cards"][0]["context"]
    brief = economic_screening_brief(saved)["cards"][0]["context"]
    assert "moving_averages" not in brief
    assert "price_path" not in brief
    assert "turnover_activity_20d" not in brief
    assert "market_conditioned_60d" not in brief["market_relative"]


def test_activity_reaches_brief_without_changing_rank_or_mutating_saved_inputs():
    from baibai_engine.machine_analysis.models.market_context.activity import (
        market_conditioned_returns,
        turnover_activity,
    )

    days = tuple(date(2026, 1, 5) - timedelta(days=n) for n in range(60, -1, -1))
    calendar = price_calendar(dict.fromkeys(days, True), as_of=days[-1])
    prices = dict.fromkeys(days, 100.0)
    conditional = market_conditioned_returns(
        prices=prices, benchmark_prices={"1321": prices}, calendar=calendar
    )
    activity = turnover_activity(turnover=prices, calendar=calendar)
    data = payloads()
    baseline = project(data)
    data["market_context"]["price"] = {"turnover_activity_20d": activity}
    data["market_context"]["relative"] = {"market_conditioned_60d": conditional}
    original = deepcopy(data)
    projected = project(data)
    assert projected.paths == baseline.paths
    baseline_selection = select_economic_paths((baseline,), policy=EconomicPathPolicy())
    selected = select_economic_paths((projected,), policy=EconomicPathPolicy())
    saved = TypeAdapter(EconomicPathSelection).dump_python(selected, mode="json")
    old = TypeAdapter(EconomicPathSelection).dump_python(baseline_selection, mode="json")
    for key in ("rank", "priority", "strongest_path", "paths"):
        assert saved["cards"][0][key] == old["cards"][0][key]
    shown = economic_screening_brief(saved)["cards"][0]["context"]
    assert shown["market_relative"]["market_conditioned_60d"] == conditional
    assert shown["turnover_activity_20d"] == activity
    shown["turnover_activity_20d"]["status"] = "display-only"
    assert saved["cards"][0]["context"]["turnover_activity_20d"]["status"] == "available"
    assert data == original


@pytest.mark.parametrize(
    "state", ["complete", "missing_interval", "old_model", "old_path_statistics", "old_selection"]
)
def test_technical_path_reaches_candidate_and_brief_without_reranking_or_saved_backfill(
    tmp_path, state
):
    data = payloads()
    baseline = project(data)
    end = metadata().as_of
    calendar = {
        end - timedelta(days=i): (end - timedelta(days=i)).weekday() < 5 for i in range(150)
    }
    dates = sorted(day for day, business in calendar.items() if business)
    bars = [PriceBar(day, 100 + i % 3, 1000.0) for i, day in enumerate(dates)]
    states = dict.fromkeys(dates, "priced")
    if state == "missing_interval":
        del states[dates[-10]]
    path = observe_price_path(
        bars, calendar=price_calendar(calendar, as_of=end), day_states=states
    ).payload
    technical = generate_technical_analysis(
        bars, ticker="1234", as_of=end, trading_dates=dates, price_path=path
    )
    if state == "old_model":
        del path["observations"]["G60"]["path_statistics"]
        path["calculation_revision"] = "common-calendar-observed-close-path-v1"
    elif state == "old_path_statistics":
        del path["observations"]["G60"]["path_statistics"]["drawdown_episodes"]
        path["calculation_revision"] = "common-calendar-observed-close-path-v2"
    data["technical_analysis"] = technical
    original = deepcopy(data)
    projected = project(data)
    assert projected.paths == baseline.paths
    assert projected.context["moving_averages"] == technical["moving_averages"]
    assert projected.context["price_path"] == path
    second = replace(project(payloads(price=200)), ticker="5678")
    before = select_economic_paths((baseline, second), policy=EconomicPathPolicy())
    selected = select_economic_paths((projected, second), policy=EconomicPathPolicy())
    assert [(c.ticker, c.rank, c.priority, c.paths) for c in selected.cards] == [
        (c.ticker, c.rank, c.priority, c.paths) for c in before.cards
    ]
    if state == "old_selection":
        del selected.cards[0].context["moving_averages"]
        del selected.cards[0].context["price_path"]
    header = metadata()
    runs = tmp_path / "runs.sqlite"
    publish_generation(ModelGeneration(**header.model_dump(), models=models(data)), path=runs)
    store = ScreeningSelectionStore(runs)
    document = ScreeningSelection(header.generation_id, ("1234", "5678"), selected)
    assert store.publish("technical-context", document)
    stored_bytes = runs.read_bytes()
    restored = store.read("technical-context")
    assert restored == document
    saved = TypeAdapter(EconomicPathSelection).dump_python(restored.result, mode="json")
    canonical_before = canonical_json(saved)
    brief = economic_screening_brief(saved)["cards"][0]["context"]
    if state == "old_selection":
        assert "moving_averages" not in brief
        assert "price_path" not in brief
    else:
        assert brief["moving_averages"] == technical["moving_averages"]
        assert brief["moving_averages"]["sma_200bar"]["status"] == "insufficient_history"
        shown = brief["price_path"]
        assert shown["read_semantics"] == path["read_semantics"]
        assert set(shown["observations"]) == {"R20", "G60"}
        g60 = shown["observations"]["G60"]
        assert "unpriced_dates" not in g60["coverage"]
        assert g60["window_start_date"] == path["observations"]["G60"]["window_start_date"]
        if state == "old_model":
            assert "path_statistics" not in g60
        else:
            assert g60["path_statistics"] == path["observations"]["G60"]["path_statistics"]
            assert g60["path_statistics"]["status"] == (
                "incomplete_price_window" if state == "missing_interval" else "available"
            )
            if state == "missing_interval":
                assert g60["coverage"]["row_counts"]["absent"] == 1
                assert g60["path_statistics"]["drawdown_episodes"] is None
            elif state == "old_path_statistics":
                assert "drawdown_episodes" not in g60["path_statistics"]
            else:
                episodes = g60["path_statistics"]["drawdown_episodes"]
                assert episodes["current"] is not None
                assert episodes["latest_recovered"] is not None
                episodes["current"]["peak_price"] = -1
            g60["path_statistics"]["status"] = "display-only-mutation"
        brief["moving_averages"]["sma_20bar"]["value"] = -1
    assert canonical_json(saved) == canonical_before
    assert data == original
    assert runs.read_bytes() == stored_bytes


@pytest.mark.parametrize("legacy", [False, True])
def test_relative_window_reaches_l3_without_inventing_legacy_dates(legacy):
    data = payloads()
    baseline = project(data)
    end = metadata().as_of
    start = end - timedelta(days=180)
    bars = [PriceBar(start, 100.0, None)] + [
        PriceBar(end - timedelta(days=19 - index), 120.0, None) for index in range(20)
    ]
    market = calculate_security_market_context(
        ticker="1234",
        bars=bars,
        master=None,
        benchmark_ticker="1321",
        benchmark_prices={start: 100.0, end: 110.0},
        peer_prices=(),
        self_range_window_bars=750,
    )
    relative = market["relative"]
    if legacy:
        relative.pop("window_basis", None)
        relative.pop("comparison_windows", None)
    data["market_context"]["relative"] = relative
    original = deepcopy(data)
    projected = project(data)
    assert projected.paths == baseline.paths
    selected = select_economic_paths((projected,), policy=EconomicPathPolicy())
    saved = TypeAdapter(EconomicPathSelection).dump_python(selected, mode="json")
    before = canonical_json(saved)
    assert saved["cards"][0]["context"]["market_relative"] == relative
    brief = economic_screening_brief(saved)["cards"][0]["context"]["market_relative"]
    assert brief["relative_20d"] == pytest.approx(0.1)
    if legacy:
        assert "window_basis" not in brief
        assert "comparison_windows" not in brief
    else:
        assert brief["window_basis"] == "observed_security_bar_differences"
        assert brief["comparison_windows"]["relative_20d"] == {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        }
        brief["comparison_windows"]["relative_20d"]["start_date"] = "display-only-change"
    assert canonical_json(saved) == before
    assert data == original


@pytest.mark.parametrize("basis", ["actual_reported", "forecast_annual", None])
def test_dividend_basis_survives_projection_and_brief_without_changing_rank(basis):
    data = payloads()
    financial = data["company_financials"]["financial"]
    details = {
        "dividend_yield": 0.0,
        "dividend_basis": basis,
        "dps_actual_annual": 0.0,
        "dps_forecast_annual": 0.0 if basis == "forecast_annual" else None,
        "dividend_forecast_source": {
            "status": "confirmed" if basis == "forecast_annual" else "missing",
            "period_start": "2026-04-01",
            "period_end": "2027-03-31",
        },
        "dividend_split_factor": None,
    }
    baseline = project(data)
    financial.update(details)
    source = deepcopy(data)
    projected = project(data)
    assert projected.paths == baseline.paths
    assert data == source
    selected = select_economic_paths((projected,), policy=EconomicPathPolicy())
    saved = TypeAdapter(EconomicPathSelection).dump_python(selected, mode="json")
    brief = economic_screening_brief(saved)["cards"][0]["context"]["financial"]
    assert {key: brief[key] for key in details} == details
    assert {key: projected.context["financial"][key] for key in details} == details


@pytest.mark.parametrize("forecast_eps", [26.7, 0.0, None])
def test_issuer_forecast_eps_survives_without_parent_profit_recalculation(forecast_eps):
    data = payloads()
    baseline = project(data)
    financial = data["company_financials"]["financial"]
    # Parent profit/current ordinary shares can differ from issuer EPS because
    # preferred claims and the forecast share denominator are separate inputs.
    financial.update({"forecast_profit_yen": 12000.0, "forecast_eps_yen": forecast_eps})
    before = deepcopy(data)
    projected = project(data)
    assert projected.paths == baseline.paths
    selected = select_economic_paths((projected,), policy=EconomicPathPolicy())
    saved = TypeAdapter(EconomicPathSelection).dump_python(selected, mode="json")
    brief = economic_screening_brief(saved)["cards"][0]["context"]["financial"]
    assert brief["forecast_eps_yen"] == forecast_eps
    assert projected.context["financial"]["forecast_eps_yen"] == forecast_eps
    assert brief["forecast_profit_yen"] == 12000.0
    assert data == before


def test_price_change_moves_price_axes_not_business_axes():
    before, after = project(payloads()), project(payloads(price=200.0))
    for name in {path.path_id for path in before.paths}:
        a, b = active(before, name), active(after, name)
        assert b.price_value == pytest.approx(a.price_value / 2)
        assert b.business_value == pytest.approx(a.business_value)


def test_missing_earnings_and_negative_cash_do_not_remove_growth_or_recovery():
    data = payloads()
    data["company_financials"]["earnings_price"]["earnings_yield"] = None
    data["company_financials"]["earnings_price"]["unavailable_reason"] = "unavailable_annual_profit"
    result = project(data)
    assert active(result, "earning_power") is None
    assert active(result, "capital_productivity") is None
    assert active(result, "business_expansion") is not None
    assert active(result, "operating_recovery") is not None


def test_stale_price_disables_price_coordinates_but_preserves_business_data():
    data = payloads()
    data["company_financials"]["expected_price_date"] = "2026-01-06"
    result = project(data)
    assert not any(path.eligible for path in result.paths)
    assert any(path.business_value is not None for path in result.paths)
    assert result.context["financial"]["fcf_ttm"] == -200.0


@pytest.mark.parametrize(("difference", "rate"), [(-1.0, -0.1), (0.0, 0.0)])
def test_latest_nonpositive_quarter_does_not_choose_positive_annual(difference, rate):
    data = payloads()
    for change in data["company_growth"]["changes"]:
        if change["measurement"] == "quarter":
            change["difference"] = difference
            change["rate"] = rate if change["metric"] == "sales" else None
    result = project(data)
    assert active(result, "business_expansion") is None
    assert active(result, "operating_recovery") is None
    assert all(
        not path.eligible
        for path in result.paths
        if path.variant == "annual_ttm"
        and path.path_id in {"business_expansion", "operating_recovery"}
    )


def test_zero_quarter_sales_comparison_does_not_use_positive_annual():
    data = payloads()
    for observation in data["company_growth"]["periods"][0]["observations"]:
        if observation["measurement"] == "quarter":
            observation["metrics"]["sales"]["value"] = 0.0
    for change in data["company_growth"]["changes"]:
        if change["measurement"] == "quarter" and change["metric"] == "sales":
            change.update(difference=0.0, rate=None, reason="nonpositive_base")
    result = project(data)
    assert active(result, "business_expansion") is None
    assert result.context["latest_growth_changes"]["sales"]["selected_variant"] == "quarter"
    assert active(result, "operating_recovery") is not None


def test_missing_quarter_uses_same_anchor_annual_not_old_good_quarter():
    data = payloads()
    for change in data["company_growth"]["changes"]:
        if change["measurement"] == "quarter":
            change["difference"] = None
            change["rate"] = None
    old = deepcopy(data["company_growth"]["changes"][0])
    old.update(to_end="2025-09-30", to_start="2025-07-01", rate=100.0)
    data["company_growth"]["changes"].append(old)
    result = project(data)
    assert active(result, "business_expansion").variant == "annual_ttm"
    assert active(result, "business_expansion").business_value == 0.2
    assert active(result, "operating_recovery").variant == "annual_ttm"
    assert result.context["latest_growth_changes"]["sales"]["selected_variant"] == "annual_ttm"


def test_loss_narrowing_difference_remains_valid_with_nonpositive_base():
    data = payloads()
    for observation in data["company_growth"]["periods"][0]["observations"]:
        observation["metrics"]["operating_profit"]["value"] = -40.0
        observation["metrics"]["operating_margin"]["value"] = -0.04
    for change in data["company_growth"]["changes"]:
        if change["metric"] != "sales":
            change["reason"] = "nonpositive_base"
    result = project(data)
    assert active(result, "earning_power") is None
    assert active(result, "operating_recovery") is not None
    assert active(result, "business_expansion") is not None


def test_financial_margin_inapplicability_is_not_recomputed_from_raw_amounts():
    data = payloads()
    data["market_context"]["master"]["sector_33"] = "銀行業"
    for observation in data["company_growth"]["periods"][0]["observations"]:
        observation["metrics"]["operating_margin"] = {
            "value": None,
            "reason": "not_applicable_to_financial_sector",
        }
    for change in data["company_growth"]["changes"]:
        if change["metric"] == "operating_margin":
            change["difference"] = None
    result = project(data)
    assert active(result, "earning_power") is None
    assert active(result, "operating_recovery") is None
    assert active(result, "capital_productivity") is not None


@pytest.mark.parametrize("sector", [None, "0", "", "その他"])
def test_unknown_sector_is_not_an_industry_peer_group(sector):
    data = payloads()
    data["market_context"]["master"]["sector_33"] = sector
    result = project(data)
    assert not any(path.eligible for path in result.paths)
    income = next(path for path in result.paths if path.path_id == "earning_power")
    assert income.price_value == 0.1
    assert income.business_value is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("price_yen", 0.0),
        ("price_yen", -1.0),
        ("price_yen", None),
        ("shares_ex_treasury", 0.0),
        ("shares_ex_treasury", -1.0),
        ("shares_ex_treasury", None),
        ("market_cap_yen", 0.0),
        ("market_cap_yen", -1.0),
        ("market_cap_yen", None),
        ("price_date", None),
    ],
)
def test_price_gate_requires_all_positive_price_inputs_on_expected_date(field, value):
    data = payloads()
    data["company_financials"]["earnings_price"][field] = value
    result = project(data)
    assert not result.context["pricing_valid"]
    assert not any(path.eligible for path in result.paths)
    assert all(path.price_value is None for path in result.paths)
    assert (
        next(path for path in result.paths if path.path_id == "earning_power").business_value == 0.1
    )


def test_income_uses_actual_composite_ttm_interval_not_earliest_component_start():
    data = payloads()
    data["company_financials"]["earnings_price"]["profit_components"] = TypeAdapter(
        tuple[ProfitComponent, ...]
    ).dump_python(
        (
            profit_component(1, 500.0, "2025-04-01", "2025-09-30"),
            profit_component(1, 900.0, "2024-04-01", "2025-03-31"),
            profit_component(-1, 400.0, "2024-04-01", "2024-09-30"),
        ),
        mode="json",
    )
    data["company_growth"]["periods"][0]["period_end"] = "2025-09-30"
    annual = data["company_growth"]["periods"][0]["observations"][0]
    annual.update(period_start="2024-10-01", period_end="2025-09-30")
    data["company_growth"]["periods"][0]["observations"][1].update(
        period_start="2025-07-01",
        period_end="2025-09-30",
    )
    income = active(project(data), "earning_power")
    assert income is not None
    assert income.evidence["earnings_period_start"] == "2024-10-01"
    assert income.evidence["earnings_period_end"] == "2025-09-30"
    annual["period_start"] = "2024-04-01"
    result = project(data)
    assert active(result, "earning_power") is None
    assert next(path for path in result.paths if path.path_id == "earning_power").price_value == 0.1


@pytest.mark.parametrize("basis", [None, "unknown", "nonconsolidated"])
def test_income_requires_observed_matching_statement_basis(basis):
    data = payloads()
    data["company_financials"]["earnings_price"]["statement_basis"] = basis
    if basis != "nonconsolidated":
        data["company_growth"]["periods"][0]["observations"][0]["statement_basis"] = (
            basis or "unknown"
        )
    result = project(data)
    assert active(result, "earning_power") is None
    assert active(result, "business_expansion") is not None
    assert active(result, "capital_productivity") is not None


def test_period_variants_retain_both_reference_coordinates_before_selection():
    result = project(payloads())
    for name in ("business_expansion", "operating_recovery"):
        variants = {path.variant: path for path in result.paths if path.path_id == name}
        assert set(variants) == {"quarter", "annual_ttm"}
        assert variants["quarter"].eligible
        assert not variants["annual_ttm"].eligible
        assert variants["annual_ttm"].business_value is not None
        assert variants["annual_ttm"].price_value is not None
    expansion = [path for path in result.paths if path.path_id == "business_expansion"]
    assert expansion[0].price_group == expansion[1].price_group
    assert expansion[0].price_value == expansion[1].price_value


def test_newest_anchor_does_not_search_older_complete_observations():
    data = payloads()
    data["company_growth"]["periods"].append(
        PeriodState(
            fiscal_period="1Q",
            fiscal_year_end=date(2026, 12, 31),
            period_start=date(2026, 1, 1),
            period_end=date(2026, 3, 31),
            observations=(),
        ).model_dump(mode="json")
    )
    result = project(data)
    assert result.context["latest_growth_period"] == "2026-03-31"
    assert active(result, "earning_power") is None
    assert active(result, "business_expansion") is None
    assert active(result, "operating_recovery") is None
    assert active(result, "capital_productivity") is not None


def test_recovery_requires_both_changes_from_same_comparison_interval():
    data = payloads()
    for change in data["company_growth"]["changes"]:
        if change["metric"] == "operating_margin":
            change["from_start"] = "2024-07-01"
    result = project(data)
    assert active(result, "operating_recovery") is None
    assert active(result, "business_expansion") is not None


def test_negative_reported_profit_is_retained_in_capital_proxy_not_called_roe():
    data = payloads()
    data["company_financials"]["earnings_price"]["earnings_yield"] = -0.1
    data["company_financials"]["earnings_price"]["profit_components"][0]["amount_yen"] = -1000.0
    result = project(data)
    assert active(result, "earning_power") is None
    assert active(result, "capital_productivity") is None
    capital = next(path for path in result.paths if path.path_id == "capital_productivity")
    assert capital.business_value == pytest.approx(-0.05)
    assert "not_verified_common_or_average_equity_roe" in capital.evidence["interpretation"]
    assert active(result, "business_expansion") is not None


def test_overflowing_derived_coordinates_are_missing_not_nonfinite():
    data = payloads()
    earnings = data["company_financials"]["earnings_price"]
    earnings.update(
        price_yen=1e-308, shares_ex_treasury=1.0, market_cap_yen=1e-308, earnings_yield=1e308
    )
    data["company_financials"]["financial"].update(market_cap=1e-308, pbr=1e308)
    for observation in data["company_growth"]["periods"][0]["observations"]:
        observation["metrics"]["sales"]["value"] = 1e308
    for change in data["company_growth"]["changes"]:
        if change["metric"] == "operating_profit":
            change["difference"] = 1e308
    result = project(data)
    assert active(result, "business_expansion") is None
    assert active(result, "operating_recovery") is None
    assert active(result, "capital_productivity") is None
    assert (
        next(path for path in result.paths if path.path_id == "capital_productivity").business_value
        is None
    )


def test_added_models_are_referenced_without_automatic_extra_coordinates_or_votes():
    data = payloads()
    before = project(data)
    data["new_company_model"] = {"discovery": "detail_only", "score": 100.0}
    planned = metadata().model_copy(
        update={
            "schema_version": 2,
            "subject_plan": {
                "1234": GenerationSubject(
                    scope="stock",
                    model_ids=(*data, "not_generated_model"),
                )
            },
        }
    )
    after = project_company("1234", models=models(data), metadata=planned)
    assert after.paths == before.paths
    assert after.model_revisions["new_company_model"] == "v1"
    assert after.not_generated == ("not_generated_model",)


def test_latest_growth_changes_remain_context_when_improvements_are_not_positive():
    data = payloads()
    data["company_financials"]["financial"]["tradable_share_change_yoy"] = 0.15
    for change in data["company_growth"]["changes"]:
        if change["measurement"] == "quarter":
            change["difference"] = -1.0
            change["rate"] = -0.1 if change["metric"] == "sales" else None
    result = project(data)
    assert active(result, "business_expansion") is None
    assert active(result, "operating_recovery") is None
    latest = result.context["latest_growth_changes"]
    assert latest["sales"]["latest_quarter"]["rate"] == -0.1
    for metric in ("sales", "operating_profit", "operating_margin"):
        assert latest[metric]["selected_variant"] == "quarter"
        assert latest[metric]["latest_quarter"]["difference"] == -1.0
    assert result.context["financial"]["tradable_share_change_yoy"] == 0.15


def test_external_context_preserves_actual_group_comparisons_without_original_snapshots():
    data = payloads()
    before = project(data)
    data["external_assessment"] = {
        "model_valuations": [
            {
                "inputs": {
                    "latest": {
                        "distributor": "valuation_distributor",
                        "per_consensus_origin": "valuation_origin",
                        "per_consensus_period": "FY2026",
                        "currency": "JPY",
                        "snapshot_only_payload": "not_a_summary",
                    },
                    "previous": {
                        "per_consensus_origin": "valuation_origin",
                        "per_consensus_period": "FY2026",
                    },
                },
                "comparisons": [
                    {
                        "metric": "per_consensus",
                        "state": "decreased",
                        "comparable": True,
                        "latest_value": 9.0,
                        "previous_value": 10.0,
                        "delta": -1.0,
                    }
                ],
            }
        ],
        "analyst_ratings": [
            {
                "distributor": "rating_distributor",
                "inputs": {
                    "latest": {
                        "origin": "rating_origin",
                        "rating_scale_min": 1.0,
                        "rating_scale_max": 5.0,
                        "rating_scale_direction": "lower_is_positive",
                    }
                },
                "comparison": {
                    "metric": "rating",
                    "state": "count_changed",
                    "comparable": False,
                    "reason": "count_changed",
                    "latest_count": 2,
                    "previous_count": 1,
                    "delta": None,
                },
            }
        ],
        "quants_scores": [
            {
                "inputs": {
                    "latest": {
                        "distributor": "kabumap",
                        "origin": "Quants Research",
                        "score_as_of_date": "2026-01-05",
                        "score_scale_max": 10,
                    }
                },
                "comparisons": [
                    {
                        "metric": "growth_score",
                        "state": "increased",
                        "comparable": True,
                        "latest_value": 8.0,
                        "previous_value": 7.0,
                        "delta": 1.0,
                    }
                ],
            }
        ],
        "tradingview": {
            "availability": "forecast_fields_present",
            "inputs": {
                "latest": {"target_period_label": "FY2026", "target_period_end": "2026-12-31"}
            },
            "comparisons": [
                {
                    "metric": "eps_forecast_next_fy",
                    "state": "not_comparable",
                    "comparable": False,
                    "reason": "metric_period_mapping_unconfirmed",
                    "latest_value": 12.0,
                    "previous_value": 10.0,
                    "delta": None,
                }
            ],
        },
    }
    result = project(data)
    assert result.paths == before.paths
    external = result.context["external_assessment"]
    summaries = {item["kind"]: item for item in external["comparisons"]}
    assert set(summaries) == {"model_valuations", "analyst_ratings", "quants_scores", "tradingview"}
    valuation = summaries["model_valuations"]
    assert valuation["origin"] == "valuation_origin"
    assert valuation["target_period"] == valuation["previous_target_period"] == "FY2026"
    assert valuation["delta"] == -1.0
    assert summaries["analyst_ratings"]["state"] == "count_changed"
    assert summaries["analyst_ratings"]["delta"] is None
    assert summaries["analyst_ratings"]["basis"]["rating_scale_direction"] == "lower_is_positive"
    assert summaries["quants_scores"]["delta"] == 1.0
    assert summaries["tradingview"]["state"] == "not_comparable"
    assert summaries["tradingview"]["delta"] is None
    assert summaries["tradingview"]["target_period"] is None
    assert summaries["tradingview"]["source_target_period_label"] == "FY2026"
    assert "snapshot_only_payload" not in str(external)


@pytest.mark.parametrize(
    "kind", ["model_valuations", "analyst_ratings", "quants_scores", "tradingview"]
)
@pytest.mark.parametrize("legacy_saved_summary", [False, True])
def test_external_previous_clocks_survive_selection_and_brief(tmp_path, kind, legacy_saved_summary):
    data = payloads()
    baseline = project(data)
    latest = {"fetched_at_utc": "2026-01-04T15:30:00Z"}
    previous = {"fetched_at_utc": "2026-01-01T15:30:00Z"}
    comparison = {
        "metric": "growth_score" if kind == "quants_scores" else "rating",
        "state": "increased" if kind == "quants_scores" else "not_comparable",
        "comparable": kind == "quants_scores",
        "latest_value": 8.0,
        "previous_value": 7.0,
        "delta": 1.0 if kind == "quants_scores" else None,
    }
    if kind == "quants_scores":
        latest["score_as_of_date"] = "2025-12-30"
        previous["score_as_of_date"] = "2025-12-29"
    assessment = {"inputs": {"latest": latest, "previous": previous}}
    if kind == "analyst_ratings":
        assessment["comparison"] = comparison
    else:
        assessment["comparisons"] = [comparison]
    data["external_assessment"] = {kind: assessment if kind == "tradingview" else [assessment]}
    original = deepcopy(data)
    projected = project(data)
    assert projected.paths == baseline.paths
    assert data == original
    summary = projected.context["external_assessment"]["comparisons"][0]
    assert summary["previous_fetched_at_utc"] == previous["fetched_at_utc"]
    assert summary["previous_score_as_of_date"] == previous.get("score_as_of_date")
    assert summary["comparable"] is comparison["comparable"]
    assert summary["delta"] == comparison["delta"]
    if legacy_saved_summary:
        del summary["previous_fetched_at_utc"]
        del summary["previous_score_as_of_date"]
    header = metadata()
    runs = tmp_path / "runs.sqlite"
    publish_generation(ModelGeneration(**header.model_dump(), models=models(data)), path=runs)
    result = select_economic_paths((projected,), policy=EconomicPathPolicy())
    prior = select_economic_paths((baseline,), policy=EconomicPathPolicy()).cards[0]
    assert (result.cards[0].rank, result.cards[0].priority) == (prior.rank, prior.priority)
    selected = ScreeningSelection(header.generation_id, ("1234",), result)
    store = ScreeningSelectionStore(runs)
    assert store.publish("external-clocks", selected)
    stored_bytes = runs.read_bytes()
    restored = store.read("external-clocks")
    assert restored == selected
    saved = TypeAdapter(EconomicPathSelection).dump_python(restored.result, mode="json")
    before = canonical_json(saved)
    brief = economic_screening_brief(saved)["cards"][0]["context"]["external_assessment"]
    group = brief["sources"][0]
    assert group["fetched_at_utc"] == latest["fetched_at_utc"]
    if legacy_saved_summary:
        assert "previous_fetched_at_utc" not in group
        assert "previous_score_as_of_date" not in group
    else:
        assert group["previous_fetched_at_utc"] == previous["fetched_at_utc"]
        assert group.get("previous_score_as_of_date") == previous.get("score_as_of_date")
    if kind == "quants_scores":
        change = brief["comparable_changes"][0]
        assert change["score_as_of_date"] == latest["score_as_of_date"]
        if legacy_saved_summary:
            assert "previous_fetched_at_utc" not in change
            assert "previous_score_as_of_date" not in change
        else:
            assert change["previous_fetched_at_utc"] == previous["fetched_at_utc"]
            assert change["previous_score_as_of_date"] == previous["score_as_of_date"]
    else:
        assert brief["comparable_changes"] == []
    assert canonical_json(saved) == before
    assert runs.read_bytes() == stored_bytes


@pytest.mark.parametrize("legacy_saved_summary", [False, True])
@pytest.mark.parametrize(
    "rating_counts",
    [
        (9, 6, 0, 1, 0),
        (0, 1, 0, 0, 0),
        (None, None, None, None, None),
    ],
)
def test_minkabu_receipt_and_rating_counts_survive_selection_and_brief_without_rewriting_history(
    tmp_path, legacy_saved_summary, rating_counts
):
    data = payloads()
    counts = dict(
        zip(
            ("strong_buy_count", "buy_count", "neutral_count", "sell_count", "strong_sell_count"),
            rating_counts,
            strict=True,
        )
    )
    latest = {"availability": "available", "display_as_of_date": "2025-12-30"}
    data["external_assessment"] = {"minkabu": {"latest": latest}}
    baseline = project(data)
    received = "2026-01-04T15:30:00Z"
    latest["fetched_at_utc"] = received
    latest.update(counts)
    source = deepcopy(data)
    projected = project(data)
    assert projected.paths == baseline.paths
    assert data == source
    summary = projected.context["external_assessment"]["minkabu_latest"]
    assert summary["fetched_at_utc"] == received
    assert summary["display_as_of_date"] == "2025-12-30"
    assert {key: summary[key] for key in counts} == counts
    if legacy_saved_summary:
        # Existing saved cards lack these fields; reading must not backfill them.
        del summary["fetched_at_utc"]
        for key in counts:
            del summary[key]
    header = metadata()
    runs = tmp_path / "runs.sqlite"
    publish_generation(ModelGeneration(**header.model_dump(), models=models(data)), path=runs)
    result = select_economic_paths((projected,), policy=EconomicPathPolicy())
    prior = select_economic_paths((baseline,), policy=EconomicPathPolicy()).cards[0]
    assert (result.cards[0].rank, result.cards[0].priority) == (prior.rank, prior.priority)
    selected = ScreeningSelection(header.generation_id, ("1234",), result)
    store = ScreeningSelectionStore(runs)
    assert store.publish("minkabu-clock", selected)
    stored_bytes = runs.read_bytes()
    restored = store.read("minkabu-clock")
    assert restored == selected
    saved = TypeAdapter(EconomicPathSelection).dump_python(restored.result, mode="json")
    before = canonical_json(saved)
    brief = economic_screening_brief(saved)["cards"][0]["context"]["external_assessment"]["minkabu"]
    if legacy_saved_summary:
        assert "fetched_at_utc" not in brief
    else:
        assert brief["fetched_at_utc"] == received
    assert {key: brief[key] for key in counts if key in brief} == (
        {}
        if legacy_saved_summary
        else {key: value for key, value in counts.items() if value is not None}
    )
    assert brief["display_as_of_date"] == "2025-12-30"
    assert canonical_json(saved) == before
    assert runs.read_bytes() == stored_bytes


@pytest.mark.parametrize("legacy_saved_summary", [False, True])
def test_quants_not_computed_availability_survives_selection_and_brief(
    tmp_path, legacy_saved_summary
):
    data = payloads()
    baseline = project(data)
    data["external_assessment"] = {
        "method_version": "same-series-v4",
        "quants_scores": [
            {
                "inputs": {
                    "latest": {
                        "distributor": "kabumap",
                        "origin": "Quants Research",
                        "availability": "not_computed",
                        "score_as_of_date": None,
                        "fetched_at_utc": "2026-01-04T15:30:00Z",
                        "source_url": "https://example.invalid/quants/1234",
                    },
                    "previous": {"availability": "available", "origin": "Quants Research"},
                },
                "comparisons": [
                    {
                        "metric": metric,
                        "state": "missing",
                        "reason": "latest_missing",
                        "comparable": False,
                        "latest_value": None,
                        "previous_value": previous,
                        "delta": None,
                    }
                    for metric, previous in (("growth_score", 8.0), ("composite_score", 65.0))
                ],
            }
        ],
    }
    source = deepcopy(data)
    projected = project(data)
    assert projected.paths == baseline.paths
    assert data == source
    summaries = projected.context["external_assessment"]["comparisons"]
    assert all(item["availability"] == "not_computed" for item in summaries)
    assert all(item["previous_availability"] == "available" for item in summaries)
    assert all(item["latest_value"] is None for item in summaries)
    assert all(item["score_as_of_date"] is None for item in summaries)
    if legacy_saved_summary:
        for item in summaries:
            del item["availability"]
            del item["previous_availability"]
    header = metadata()
    runs = tmp_path / "runs.sqlite"
    publish_generation(ModelGeneration(**header.model_dump(), models=models(data)), path=runs)
    result = select_economic_paths((projected,), policy=EconomicPathPolicy())
    selected = ScreeningSelection(header.generation_id, ("1234",), result)
    store = ScreeningSelectionStore(runs)
    assert store.publish("quants-missing", selected)
    stored_bytes = runs.read_bytes()
    restored = store.read("quants-missing")
    assert restored == selected
    saved = TypeAdapter(EconomicPathSelection).dump_python(restored.result, mode="json")
    before = canonical_json(saved)
    brief = economic_screening_brief(saved)["cards"][0]["context"]["external_assessment"]
    assert brief["comparable_changes"] == []
    group = brief["sources"][0]
    if legacy_saved_summary:
        assert "availability" not in group
        assert "previous_availability" not in group
    else:
        assert group["availability"] == "not_computed"
        assert group["previous_availability"] == "available"
    assert group["fetched_at_utc"] == "2026-01-04T15:30:00Z"
    assert "score_as_of_date" not in group
    state = group["states"][0]
    assert state["state"] == "missing"
    assert state["metrics"] == {"growth_score": None, "composite_score": None}
    assert state["previous_values"] == {"growth_score": 8.0, "composite_score": 65.0}
    assert canonical_json(saved) == before
    assert runs.read_bytes() == stored_bytes


@pytest.mark.parametrize(
    "state", ["first_observation", "recovered", "count_changed", "not_comparable"]
)
def test_external_noncomparison_states_are_not_promoted_to_revisions(state):
    data = payloads()
    data["external_assessment"] = {
        "analyst_ratings": [
            {
                "distributor": "received_rating",
                "comparison": {
                    "metric": "rating",
                    "state": state,
                    "comparable": False,
                    "latest_value": 4.0,
                    "delta": None,
                },
            }
        ]
    }
    summary = project(data).context["external_assessment"]["comparisons"][0]
    assert summary["state"] == state
    assert summary["comparable"] is False
    assert summary["delta"] is None
    assert "revision" not in summary


def test_capital_proxy_uses_pbr_not_unselected_equity_or_edinet_scope():
    data = payloads()
    data["company_financials"]["financial"].update(
        same_state_equity_yen=999999.0, consolidation_basis="standalone"
    )
    capital = active(project(data), "capital_productivity")
    assert capital.business_value == pytest.approx(0.05)
    assert capital.evidence["book_amount_implied_by_pbr"] == 20000.0
    assert "not_exposed" in capital.evidence["book_provenance"]
    data["company_financials"]["financial"]["market_cap"] = 999.0
    assert active(project(data), "capital_productivity") is None


def test_selected_book_reaches_l3_context_without_changing_economic_coordinates():
    data = payloads()
    baseline = project(data)
    assert "book_equity" not in baseline.context["financial"]
    book = {
        "amount_yen": 20000.0,
        "method": "assets_times_equity_ratio",
        "measurement_date": "2025-06-30",
        "statement_basis": "consolidated",
        "disclosure_number": "book-source",
        "disclosed_at": "2025-08-12",
        "operands": {"total_assets_yen": 40000.0, "equity_to_asset_ratio": 0.5},
    }
    data["company_financials"]["financial"]["book_equity"] = book
    changed = project(data)
    for before, after in zip(baseline.paths, changed.paths, strict=True):
        assert (before.price_value, before.business_value, before.eligible) == (
            after.price_value,
            after.business_value,
            after.eligible,
        )
    assert changed.context["financial"]["book_equity"] == book
    assert active(changed, "capital_productivity").evidence["book_provenance"] == (
        "selected_model_book_in_context_financial_book_equity"
    )


def test_explicit_missing_book_stays_distinct_from_unrecorded_book():
    data = payloads()
    data["company_financials"]["financial"]["book_equity"] = None
    result = project(data)
    assert "book_equity" in result.context["financial"]
    assert result.context["financial"]["book_equity"] is None
    assert active(result, "capital_productivity").evidence["book_provenance"] == (
        "individual_book_source_scope_and_measurement_date_not_exposed"
    )


def test_unrecorded_balance_context_does_not_rewrite_old_selection():
    data = payloads()
    baseline = project(data)
    data["company_financials"]["financial"].update(
        net_cash=100.0,
        investment_securities=50.0,
        edinet_source_submit_datetime="2025-08-12 09:00",
    )
    assert project(data) == baseline
    assert "edinet_reported_balances" not in baseline.context["financial"]


@pytest.mark.parametrize("available", [True, False])
def test_reported_balances_reach_l3_brief_without_changing_paths_or_filling_claims(available):
    inputs = payloads()
    baseline = project(inputs)
    balances = (
        {
            "amount_unit": "JPY",
            "amounts": {
                "cash": 100.0,
                "debt": 40.0,
                "net_cash": 60.0,
                "investment_securities": 0.0,
            },
            "source_doc_id": "S100BALANCE",
            "source_submit_datetime": "2025-08-12 09:00",
            "consolidation_basis": "consolidated",
            "measurement_date": {"date": None, "reason": "fact_date_not_retained"},
            "filing_fiscal_period": {"start": "2025-04-01", "end": "2026-03-31"},
            "interpretation": "reported_financial_balances_not_common_equity_surplus",
        }
        if available
        else None
    )
    inputs["company_financials"]["financial"]["edinet_reported_balances"] = balances
    original = deepcopy(inputs)
    projected = project(inputs)
    assert projected.paths == baseline.paths
    assert projected.context["financial"]["edinet_reported_balances"] == balances
    selected = select_economic_paths((projected,), policy=EconomicPathPolicy())
    saved = TypeAdapter(EconomicPathSelection).dump_python(selected, mode="json")
    brief = economic_screening_brief(saved)["cards"][0]["context"]["financial"]
    assert "edinet_reported_balances" in brief
    assert brief["edinet_reported_balances"] == balances
    if available:
        brief["edinet_reported_balances"]["amounts"]["cash"] = 1.0
    assert inputs == original


def test_economic_result_roundtrip_and_history_identity(tmp_path):
    header = metadata()
    generation = ModelGeneration(**header.model_dump(), models=models(payloads()))
    runs = tmp_path / "runs.sqlite"
    publish_generation(generation, path=runs)
    result = evaluate_economic_coordinates(
        header,
        tickers=("1234",),
        coordinates={"1234": project_company("1234", models=generation.models, metadata=header)},
        policy=EconomicPathPolicy(),
    ).selection
    store = ScreeningSelectionStore(runs)
    assert store.publish("policy-new", result)
    assert not store.publish("policy-new", result)
    assert store.read("policy-new") == result
    codec = TypeAdapter(ScreeningSelection)
    assert codec.validate_json(codec.dump_json(result)) == result


def test_identical_growth_date_matches_collapse_without_duplicate_votes():
    data = payloads()
    before = project(data)
    data["company_growth"]["changes"] += deepcopy(data["company_growth"]["changes"])
    assert project(data).paths == before.paths


def test_conflicting_growth_date_matches_only_make_that_metric_unavailable():
    data = payloads()
    duplicate = deepcopy(data["company_growth"]["changes"][1])
    duplicate["difference"] = 999.0
    data["company_growth"]["changes"].append(duplicate)
    result = project(data)
    latest = result.context["latest_growth_changes"]
    assert latest["operating_profit"]["latest_quarter"]["reason"] == ("ambiguous_growth_comparison")
    assert latest["operating_margin"]["latest_quarter"]["difference"] == 0.02
    assert active(result, "business_expansion") is not None
    assert active(result, "earning_power") is not None


def test_saved_growth_evidence_keeps_missing_fields_and_original_number_representation():
    data = payloads()
    growth = data["company_growth"]
    annual = growth["periods"][0]["observations"][0]
    annual.pop("share_basis")
    annual["metrics"]["sales"] = {"value": 1000}
    sales_change = growth["changes"][0]
    sales_change.pop("margin_points")
    sales_change["difference"] = 100
    before = canonical_json(data)
    result = project(data)
    expansion = active(result, "business_expansion")
    assert (
        canonical_json(expansion.evidence["sales_observation"]["observation"]) == '{"value":1000}'
    )
    assert canonical_json(expansion.evidence["sales_change"]) == canonical_json(sales_change)
    assert result.context["annual_observation"]["latest_ttm_metrics"] == annual["metrics"]
    assert result.context["annual_observation"]["latest_ttm_period_status"] is None
    assert result.context["annual_observation"]["latest_ttm_basis"] == {
        key: annual.get(key)
        for key in ("period_start", "period_end", "statement_basis", "accounting_standard")
    }
    assert canonical_json(data) == before


def test_different_saved_comparison_shapes_remain_ambiguous_even_when_typed_values_match():
    data = payloads()
    duplicate = deepcopy(data["company_growth"]["changes"][1])
    duplicate.pop("margin_points")
    data["company_growth"]["changes"].append(duplicate)
    result = project(data)
    change = result.context["latest_growth_changes"]["operating_profit"]["latest_quarter"]
    assert change["reason"] == "ambiguous_growth_comparison"
    assert change["matching_comparison_count"] == 2
    assert active(result, "business_expansion") is not None


def test_missing_comparison_field_does_not_create_a_match_from_the_owner_default():
    data = payloads()
    before = project(data)
    duplicate = deepcopy(data["company_growth"]["changes"][1])
    duplicate.pop("comparison")
    data["company_growth"]["changes"].append(duplicate)
    after = project(data)
    assert after == before
    data["company_growth"]["changes"][1].pop("comparison")
    result = project(data)
    assert result.context["latest_growth_changes"]["operating_profit"]["latest_quarter"] == {}


@pytest.mark.parametrize("metric", ["operating_profit", "operating_margin"])
@pytest.mark.parametrize("bad_value", [-1.0, 0.0])
def test_bad_quarter_with_missing_companion_cannot_escape_to_annual(metric, bad_value):
    data = payloads()
    for change in data["company_growth"]["changes"]:
        if change["measurement"] == "quarter" and change["metric"] != "sales":
            change["difference"] = bad_value if change["metric"] == metric else None
    result = project(data)
    assert active(result, "operating_recovery") is None
    latest = result.context["latest_growth_changes"][metric]
    assert latest["selected_variant"] == "quarter"
    assert latest["latest_quarter"]["difference"] == bad_value


def test_positive_quarter_with_missing_companion_retains_counterevidence_on_annual_fallback():
    data = payloads()
    for change in data["company_growth"]["changes"]:
        if change["measurement"] == "quarter" and change["metric"] == "operating_margin":
            change["difference"] = None
    result = project(data)
    assert active(result, "operating_recovery").variant == "annual_ttm"
    latest = result.context["latest_growth_changes"]
    assert latest["operating_profit"]["latest_quarter"]["difference"] == 10.0
    assert latest["operating_margin"]["latest_quarter"]["difference"] is None
    assert latest["operating_margin"]["selected_variant"] == "annual_ttm"
    assert latest["operating_margin"]["selected_comparison"]["difference"] == 0.02


def test_generated_unavailable_growth_reason_is_not_not_generated():
    data = payloads()
    data["company_growth"] = CompanyGrowth(
        ticker="1234",
        status="unavailable",
        reason="accounting_period_not_observed",
        requested_start=None,
        requested_end=date(2026, 1, 5),
    ).model_dump(mode="json")
    result = project(data)
    assert result.context["growth_status"] == "unavailable"
    assert result.context["growth_reason"] == "accounting_period_not_observed"
    assert "company_growth" in result.model_revisions
    assert "company_growth" not in result.not_generated
    assert "status" not in result.context["external_assessment"]


def owner_generation(
    *, alternate_quarter_profit=None, alternate_prior_quarter_profit=None, reverse_documents=False
):
    as_of = date(2026, 8, 31)
    release = InputRelease(release_id="owner-economic-fixture", manifest_sha256="b" * 64)

    def document(*, year, fiscal_period, end, sales, op, profit, disclosed):
        return JQuantsFinancialSummary(
            ticker="1001",
            disclosed_at=disclosed,
            fiscal_period=fiscal_period,
            fiscal_year_end=date(year + 1, 3, 31),
            period_start=date(year, 4, 1),
            period_end=end,
            sales=sales,
            operating_profit=op,
            profit=profit,
            shares_outstanding=100.0,
            treasury_shares=20.0,
            bps=10.0,
            disclosure_number=f"owner-{year}-{fiscal_period}",
            document_type=f"{fiscal_period}FinancialStatements_Consolidated_JP",
        )

    documents = [
        document(
            year=2025,
            fiscal_period="1Q",
            end=date(2025, 6, 30),
            sales=250.0,
            op=10.0,
            profit=15.0,
            disclosed=date(2025, 8, 10),
        ),
        document(
            year=2025,
            fiscal_period="FY",
            end=date(2026, 3, 31),
            sales=1200.0,
            op=100.0,
            profit=100.0,
            disclosed=date(2026, 5, 15),
        ),
        document(
            year=2026,
            fiscal_period="1Q",
            end=date(2026, 6, 30),
            sales=400.0,
            op=30.0,
            profit=25.0,
            disclosed=date(2026, 8, 10),
        ),
    ]
    if alternate_quarter_profit is not None:
        documents.append(
            replace(
                documents[-1],
                fiscal_year_end=date(2027, 6, 30),
                operating_profit=alternate_quarter_profit,
                disclosure_number="owner-fy-end-change",
            )
        )
    if alternate_prior_quarter_profit is not None:
        documents.append(
            replace(
                documents[0],
                fiscal_year_end=date(2026, 6, 30),
                operating_profit=alternate_prior_quarter_profit,
                disclosure_number="owner-prior-fy-end-change",
            )
        )
    if reverse_documents:
        documents.reverse()
    stream = generate_models(
        generation_id="owner-economic-generation",
        as_of=as_of,
        evaluation_time=datetime(2026, 9, 1, tzinfo=UTC),
        inputs={
            "market": InputSnapshot(
                kind="lake_release", reference=release.release_id, sha256=release.manifest_sha256
            )
        },
        financial_release=release,
        companies=(
            CompanyModelInputs(
                ticker="1001",
                financial_documents=documents,
                adjusted_bars=(PriceBar(as_of, 10.0, None),),
                adjustment_events=(),
                sector="情報・通信業",
            ),
        ),
        trading_dates=tuple(as_of - timedelta(days=252 - i) for i in range(253)),
        expected_price_date=as_of,
        ttm_rules=TTMRules(
            full_year_min_days=330,
            full_year_max_days=370,
            period_length_tolerance_ratio=0.1,
            period_end_tolerance_days=45,
        ),
        external_observations={},
        market_context=None,
        securities_context={
            "1001": {"master": {"sector_33": "情報・通信業", "name": "owner fixture"}}
        },
        market_context_revision="owner-fixture-v1",
    )
    return materialize_generation(stream)


def test_owner_fy_and_cumulative_quarter_flow_through_economic_policy():
    generation = owner_generation()
    selection = evaluate_economic_coordinates(
        generation,
        tickers=("1001",),
        coordinates={
            "1001": project_company("1001", models=generation.models, metadata=generation)
        },
        policy=EconomicPathPolicy(),
    ).selection
    assert selection.result.tickers == ("1001",)
    card = selection.result.cards[0]
    paths = {item.path_id: item for item in card.paths}
    income = paths["earning_power"]
    assert income.price_value == pytest.approx(110.0 / 800.0)
    assert income.business_value == pytest.approx(120.0 / 1350.0)
    assert income.evidence["same_period_and_statement_basis"] is True
    assert income.evidence["earnings_period_start"] == "2025-07-01"
    assert income.evidence["earnings_period_end"] == "2026-06-30"
    assert income.evidence["business_observation"]["statement_basis"] == "consolidated"
    expansion = paths["business_expansion"]
    assert expansion.variant == "quarter"
    assert expansion.price_value == pytest.approx(1350.0 / 800.0)
    assert expansion.business_value == pytest.approx(0.6)
    recovery = paths["operating_recovery"]
    assert recovery.price_value == pytest.approx(20.0 / 800.0)
    assert recovery.business_value == pytest.approx(30.0 / 400.0 - 10.0 / 250.0)
    assert card.context["growth_status"] == "available"


def test_native_fiscal_year_end_tie_uses_owner_last_anchor_and_local_ambiguity():
    generation = owner_generation(alternate_quarter_profit=50.0)
    growth = generation.result("company_growth", "1001").payload
    assert growth["periods"][-1]["fiscal_year_end"] == "2027-06-30"
    row = project_company("1001", models=generation.models, metadata=generation)
    quarter = next(
        item
        for item in row.paths
        if item.path_id == "operating_recovery" and item.variant == "quarter"
    )
    assert quarter.evidence["operating_observation"]["observation"]["value"] == 50.0
    latest = row.context["latest_growth_changes"]
    assert latest["operating_profit"]["latest_quarter"]["reason"] == ("ambiguous_growth_comparison")
    assert latest["sales"]["latest_quarter"]["rate"] == pytest.approx(0.6)


@pytest.mark.parametrize("alternate_prior_profit", [10.0, 50.0])
def test_prior_native_period_tie_keeps_same_values_but_cannot_invent_recovery(
    alternate_prior_profit,
):
    generation = owner_generation(alternate_prior_quarter_profit=alternate_prior_profit)
    reversed_generation = owner_generation(
        alternate_prior_quarter_profit=alternate_prior_profit, reverse_documents=True
    )
    growth_model = generation.result("company_growth", "1001")
    assert growth_model == reversed_generation.result("company_growth", "1001")
    assert growth_model.calculation_revision == "company_growth_retrospective_v12"
    growth = growth_model.payload
    before = canonical_json(growth)
    priors = [state for state in growth["periods"] if state["period_end"] == "2025-06-30"]
    assert [state["fiscal_year_end"] for state in priors] == ["2026-03-31", "2026-06-30"]
    quarters = [
        next(item for item in state["observations"] if item["measurement"] == "quarter")
        for state in priors
    ]
    assert [item["metrics"]["operating_profit"]["value"] for item in quarters] == [
        10.0,
        alternate_prior_profit,
    ]
    assert [
        item["metrics"]["operating_profit"]["documents"][0]["disclosure_number"]
        for item in quarters
    ] == ["owner-2025-1Q", "owner-prior-fy-end-change"]
    row = project_company("1001", models=generation.models, metadata=generation)
    reversed_row = project_company(
        "1001", models=reversed_generation.models, metadata=reversed_generation
    )
    assert row == reversed_row
    latest = row.context["latest_growth_changes"]
    sales = latest["sales"]["latest_quarter"]
    assert (sales["from_value"], sales["to_value"], sales["difference"]) == (250.0, 400.0, 150.0)
    assert sales["rate"] == pytest.approx(0.6)
    assert active(row, "business_expansion") is not None
    for metric, expected in (
        ("operating_profit", 20.0),
        ("operating_margin", 30.0 / 400.0 - 10.0 / 250.0),
    ):
        comparison = latest[metric]["latest_quarter"]
        assert comparison["from_start"] == "2025-04-01"
        assert comparison["from_end"] == "2025-06-30"
        if alternate_prior_profit == 50.0:
            assert comparison["reason"] == "ambiguous_growth_comparison"
            assert all(
                comparison[field] is None
                for field in ("from_value", "difference", "rate", "margin_points", "sign_change")
            )
        else:
            assert comparison["reason"] is None
            assert comparison["difference"] == pytest.approx(expected)
    assert (active(row, "operating_recovery") is None) == (alternate_prior_profit == 50.0)
    assert canonical_json(growth) == before


def test_cash_investment_components_and_source_survive_the_same_fcf():
    first = payloads()
    first["company_financials"]["financial"].update(
        edinet_ocf_ttm=100.0,
        capex_ttm=300.0,
        edinet_capex_source="purchase_of_fixed_assets",
        consolidation_basis="consolidated",
        edinet_failure_reasons="cash_flow_detail_partial",
        edinet_source_period_start="2024-04-01",
        edinet_source_period_end="2025-03-31",
    )
    second = deepcopy(first)
    second["company_financials"]["financial"].update(edinet_ocf_ttm=-100.0, capex_ttm=100.0)
    a, b = project(first).context["financial"], project(second).context["financial"]
    assert a["fcf_ttm"] == b["fcf_ttm"] == -200.0
    assert a["edinet_ocf_ttm"] == 100.0
    assert b["edinet_ocf_ttm"] == -100.0
    assert a["capex_ttm"] == 300.0
    assert b["capex_ttm"] == 100.0
    for field in (
        "edinet_capex_source",
        "consolidation_basis",
        "edinet_failure_reasons",
        "edinet_source_period_start",
        "edinet_source_period_end",
    ):
        assert a[field] == b[field] == first["company_financials"]["financial"][field]


def test_reported_flow_and_ttm_period_context_is_not_relabelled_as_one_interval():
    inputs = payloads()
    reported = {
        "period_kind": "interim",
        "amounts": {"operating_cash_flow": 812_758_000.0, "free_cash_flow": 461_335_000.0},
        "measurement_period": {"start": None, "end": None, "reason": "fact_dates_not_retained"},
        "filing_fiscal_period": {"start": "2026-01-01", "end": "2026-12-31"},
        "annual_quality": {"free_cash_flow": "approximated"},
    }
    period = {"start": "2025-07-01", "end": "2026-06-30"}
    inputs["company_financials"]["financial"].update(
        edinet_reported_flows=reported, ocf_ttm_period=period, ocf_ttm=-1_422_368_000.0
    )
    financial = project(inputs).context["financial"]
    assert financial["edinet_reported_flows"] == reported
    assert financial["ocf_ttm_period"] == period
    assert financial["ocf_ttm"] == -1_422_368_000.0


@pytest.mark.parametrize("period_kind", ["annual", "interim"])
def test_reported_cf_details_reach_l3_brief_without_changing_candidate_paths(period_kind):
    inputs = payloads()
    baseline = project(inputs)
    reported = {
        "period_kind": "annual",
        "amount_unit": "JPY",
        "amounts": {
            "operating_cash_flow": 100.0,
            "capex": 30.0,
            "free_cash_flow": 70.0,
            "ocf_receivables_cash_effect": -10.0,
            "ocf_inventories_cash_effect": 0.0,
            "ocf_payables_cash_effect": 5.0,
            "ocf_contract_liabilities_cash_effect": 6.0,
            "ocf_advances_received_cash_effect": 7.0,
            "ocf_other_payables_cash_effect": None,
            "capex_ppe_reported": 20.0,
            "capex_intangible_reported": None,
        },
        "source_doc_id": "S100DETAIL",
        "consolidation_basis": "consolidated",
        "measurement_period": {"start": None, "end": None, "reason": "fact_dates_not_retained"},
        "filing_fiscal_period": {"start": "2025-01-01", "end": "2025-12-31"},
        "annual_quality": {"free_cash_flow": "exact"},
    }
    if period_kind == "interim":
        reported["period_kind"] = "interim"
        reported["amounts"]["capex"] = None
        reported["amounts"]["free_cash_flow"] = None
        for key in reported["amounts"]:
            if key.startswith("ocf_"):
                reported["amounts"][key] = None
        reported["annual_quality"] = {"free_cash_flow": "unavailable"}
    inputs["company_financials"]["financial"]["edinet_reported_flows"] = reported
    original = deepcopy(inputs)
    projected = project(inputs)
    assert projected.paths == baseline.paths
    assert projected.context["financial"]["edinet_reported_flows"] == reported
    selected = select_economic_paths((projected,), policy=EconomicPathPolicy())
    saved = TypeAdapter(EconomicPathSelection).dump_python(selected, mode="json")
    brief = economic_screening_brief(saved)["cards"][0]["context"]["financial"]
    assert brief["edinet_reported_flows"] == reported
    assert inputs == original


@pytest.mark.parametrize("flag", ["true", "false", "", None])
def test_accounting_change_notice_reaches_brief_without_disabling_paths(flag):
    data = payloads()
    baseline = project(data)
    disclosure = {
        "disclosure_number": "synthetic-current",
        "disclosed_at": "2026-01-05",
        "flags": {"ChgNoASRev": flag, "RetroRst": "false"},
    }
    data["company_financials"]["earnings_price"]["accounting_disclosures"] = [disclosure]
    projected = project(data)
    assert projected.paths == baseline.paths
    selected = select_economic_paths((projected,), policy=EconomicPathPolicy())
    saved = TypeAdapter(EconomicPathSelection).dump_python(selected, mode="json")
    context = economic_screening_brief(saved)["cards"][0]["context"]["accounting_change_context"]
    assert context["read_semantics"] == "positive_native_flags_not_comparability_clearance"
    assert context["disclosures"] == ([disclosure] if flag == "true" else [])


def test_holder_report_remains_evidence_and_does_not_rewrite_path_scores():
    data = payloads()
    baseline = project(data)
    data["company_financials"]["holder_capital_evidence"] = {
        "coverage": "partial",
        "scope": "retained_metadata_and_originals_only",
        "receipt_precision": "day_not_intraday",
        "observations": [
            {
                "ticker": "1234",
                "document_id": "S100TEST",
                "total_stocks": 2500000,
                "source_kind": "third_party_holder_report_not_issuer_attestation",
            }
        ],
    }
    with_evidence = project(data)
    assert with_evidence.paths == baseline.paths
    assert (
        with_evidence.context["holder_capital_evidence"]["observations"][0]["total_stocks"]
        == 2500000
    )
