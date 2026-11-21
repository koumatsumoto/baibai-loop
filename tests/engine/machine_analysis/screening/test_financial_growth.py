"""財務発見の2経路と、未確定時に次に確かめる事項。詳細数値は元の成長モデルにある。"""

from copy import deepcopy
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.technical_discovery import _models as technical_models

from baibai_engine.investment_judgment.workflow.discovery import _members
from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationMetadata,
    InputSnapshot,
)
from baibai_engine.machine_analysis.models.company_growth.generation import generate_company_growth
from baibai_engine.machine_analysis.models.company_growth.models import (
    Change,
    CompanyGrowth,
    InputRelease,
    Observation,
    PeriodState,
    SourceDocument,
    Value,
)
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.screening.policies.discovery import project_discovery_row
from baibai_engine.machine_analysis.screening.policies.financial_growth import read_growth_discovery


def metadata():
    return GenerationMetadata(
        generation_id="synthetic-fixed-generation",
        as_of=date(2026, 11, 13),
        evaluation_time=datetime(2026, 11, 14, 8, 41, tzinfo=UTC),
        inputs={"market": InputSnapshot(kind="lake_release", reference="fixed", sha256="a" * 64)},
    )


def observation(measurement, start, end, metrics):
    return Observation(
        period_start=date.fromisoformat(start),
        period_end=date.fromisoformat(end),
        measurement=measurement,
        statement_basis="consolidated",
        accounting_standard="JP",
        period_status="available",
        metrics={
            name: Value(
                value=value,
                documents=(
                    SourceDocument(
                        disclosure_number="synthetic",
                        disclosed_at=date(2026, 10, 30),
                        disclosed_time="15:00:00",
                        document_type="FinancialStatements_Consolidated_JP",
                    ),
                ),
            )
            for name, value in metrics.items()
        },
    )


def payloads():
    metrics = {"sales": 1000.0, "operating_profit": 100.0, "operating_margin": 0.1}
    growth = CompanyGrowth(
        ticker="1234",
        status="available",
        requested_start=None,
        requested_end=date(2026, 11, 13),
        periods=(
            PeriodState(
                fiscal_period="3Q",
                fiscal_year_end=date(2026, 12, 31),
                period_start=date(2026, 1, 1),
                period_end=date(2026, 9, 30),
                observations=(
                    observation("quarter", "2026-07-01", "2026-09-30", metrics),
                    observation("annual_ttm", "2025-10-01", "2026-09-30", metrics),
                    observation("reported", "2026-01-01", "2026-09-30", metrics),
                    observation("balance", "2026-09-30", "2026-09-30", {"net_assets": 500.0}),
                ),
            ),
        ),
        changes=tuple(
            Change(
                metric=metric,
                measurement="quarter",
                comparison="year_over_year",
                from_start=date(2025, 7, 1),
                from_end=date(2025, 9, 30),
                to_start=date(2026, 7, 1),
                to_end=date(2026, 9, 30),
                from_value=before,
                to_value=after,
                difference=after - before,
            )
            for metric, before, after in (
                ("sales", 900.0, 1000.0),
                ("operating_profit", 80.0, 100.0),
                ("operating_margin", 0.08, 0.1),
            )
        ),
    ).model_dump(mode="json")
    return {
        "company_growth": growth,
        "company_financials": {
            "financial": {"cash": 50.0, "debt": None, "fcf_ttm": -20.0},
        },
    }


def models(data):
    return tuple(
        GeneratedModel(model_id=key, subject="1234", calculation_revision="test-v1", payload=value)
        for key, value in data.items()
    )


def current(data, measurement):
    return next(
        item
        for item in data["company_growth"]["periods"][-1]["observations"]
        if item["measurement"] == measurement
    )


def change(data, metric):
    return next(item for item in data["company_growth"]["changes"] if item["metric"] == metric)


def add_prior_fy(data):
    values = {"sales": 3000.0, "operating_profit": 500.0, "operating_margin": 1 / 6}
    prior = PeriodState(
        fiscal_period="FY",
        fiscal_year_end=date(2025, 12, 31),
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        observations=(
            observation("reported", "2025-01-01", "2025-12-31", values),
            observation("annual_ttm", "2025-01-01", "2025-12-31", values),
        ),
    ).model_dump(mode="json")
    data["company_growth"]["periods"].insert(0, prior)
    annual = current(data, "annual_ttm")
    annual["period_status"] = "matching_period_not_observed"
    for item in annual["metrics"].values():
        item.update(value=None, reason="matching_period_not_observed")


def add_prior_quarter(data):
    prior = PeriodState(
        fiscal_period="3Q",
        fiscal_year_end=date(2025, 12, 31),
        period_start=date(2025, 1, 1),
        period_end=date(2025, 9, 30),
        observations=(
            observation(
                "quarter",
                "2025-07-01",
                "2025-09-30",
                {"sales": 900.0, "operating_profit": 80.0, "operating_margin": 0.08},
            ),
        ),
    ).model_dump(mode="json")
    data["company_growth"]["periods"].insert(0, prior)
    return prior["observations"][0]


def mark_financial_margin_inapplicable(data, *, missing_op=False):
    for measurement in ("quarter", "annual_ttm"):
        current(data, measurement)["metrics"]["operating_margin"].update(
            value=None, reason="not_applicable_to_financial_sector"
        )
        if missing_op:
            current(data, measurement)["metrics"]["operating_profit"].update(
                value=None, reason="not_observed"
            )
    change(data, "operating_margin").update(
        difference=None, reason="not_applicable_to_financial_sector"
    )
    if missing_op:
        change(data, "operating_profit").update(difference=None, reason="operand_unavailable")


def read(data):
    return read_growth_discovery("1234", models=models(data), metadata=metadata())


def evaluate(data):
    return read(data)[0]


@pytest.mark.parametrize(
    ("annual_profit", "margin_change", "matched"),
    [
        (100.0, 0.02, ("profit_growth", "operating_recovery")),
        (100.0, 0.0, ("profit_growth",)),
        (-100.0, 0.02, ("operating_recovery",)),
        (-100.0, 0.0, ()),
    ],
)
def test_growing_profits_and_operating_recovery_are_independent(
    annual_profit, margin_change, matched
):
    data = payloads()
    current(data, "annual_ttm")["metrics"]["operating_profit"]["value"] = annual_profit
    change(data, "operating_margin")["difference"] = margin_change
    before = deepcopy(data)
    result = evaluate(data)
    assert result.matched_paths == matched
    assert result.state == ("research_candidate" if matched else "not_selected")
    assert result.missing == ()
    assert data == before


def test_financial_hypothesis_does_not_require_a_market_price_technical_signal_or_forecast():
    data = payloads()
    assert not project_discovery_row("1234", models=models(data), metadata=metadata())[
        "coordinates"
    ]
    original = evaluate(data)
    data["company_financials"].update(
        expected_price_date=None, earnings_price={"invalid": "unused"}
    )
    data["company_financials"]["financial"]["forecast_profit_yen"] = 9999999
    data["market_context"] = {"master": {"sector_33": "その他"}}
    data["technical_analysis"] = {"arbitrary_score": -999999}
    assert evaluate(data) == original
    del data["company_financials"]
    assert evaluate(data) == original


@pytest.mark.parametrize(
    ("prior", "profit"), [(-30.0, -10.0), (-30.0, 0.0), (-30.0, 10.0), (0.0, 10.0), (10.0, 20.0)]
)
def test_recovery_does_not_erase_an_annual_loss_or_relabel_reported_quarter_profits(prior, profit):
    data = payloads()
    current(data, "annual_ttm")["metrics"]["operating_profit"]["value"] = -100.0
    quarter = current(data, "quarter")["metrics"]
    quarter["operating_profit"]["value"] = profit
    quarter["operating_margin"]["value"] = profit / 1000
    change(data, "operating_profit").update(
        from_value=prior,
        to_value=profit,
        difference=profit - prior,
        rate=None,
        reason="nonpositive_base" if prior <= 0 else None,
    )
    change(data, "operating_margin").update(
        from_value=prior / 900, to_value=profit / 1000, difference=profit / 1000 - prior / 900
    )
    result, source = read(data)
    assert result.matched_paths == ("operating_recovery",)
    saved_quarter = source.observation(source.latest_period, "quarter")
    (comparison,) = source.year_over_year(saved_quarter, "operating_profit")
    assert (comparison.from_value, comparison.to_value) == (prior, profit)
    assert saved_quarter.metrics["operating_profit"].value == profit
    assert (
        source.observation(source.latest_period, "annual_ttm").metrics["operating_profit"].value
        == -100
    )


def test_observed_increase_can_use_its_difference_without_a_positive_denominator():
    data = payloads()
    change(data, "operating_margin")["difference"] = 0.0
    change(data, "sales").update(
        from_value=0.0, to_value=25.0, difference=25.0, rate=None, reason="nonpositive_base"
    )
    assert evaluate(data).matched_paths == ("profit_growth",)
    change(data, "sales").update(to_value=0.0, difference=0.0)
    assert evaluate(data).state == "not_selected"


def test_unresolved_reports_gaps_from_both_routes_even_if_one_already_has_a_known_failure():
    data = payloads()
    current(data, "annual_ttm")["metrics"]["operating_profit"]["value"] = -1
    current(data, "balance")["accounting_standard"] = None
    change(data, "sales").update(difference=None, reason="operand_unavailable")
    change(data, "operating_margin").update(difference=None, reason="operand_unavailable")
    result = evaluate(data)
    assert result.state == "input_unresolved"
    assert result.matched_paths == ()
    assert result.missing == (
        "balance_quarter_basis",
        "quarter_operating_margin_increase",
        "quarter_sales_increase",
    )
    row = project_discovery_row("1234", models=models(data), metadata=metadata())
    assert row["missing"] == ["growth:" + issue for issue in result.missing]
    # Negative equity now disqualifies both. Neither missing observations nor a forecast repair it.
    current(data, "balance")["metrics"]["net_assets"]["value"] = -1
    data["company_financials"]["financial"]["forecast_profit_yen"] = 10**9
    disqualified = evaluate(data)
    assert disqualified.state == "not_selected"
    assert disqualified.missing == ()


def test_missing_quarter_comparison_is_not_replaced_by_positive_cumulative_growth():
    data = payloads()
    for item in data["company_growth"]["changes"]:
        item.update(difference=None, reason="operand_unavailable")
    cumulative = deepcopy(change(data, "sales"))
    cumulative.update(
        measurement="reported",
        to_start="2026-01-01",
        from_start="2025-01-01",
        difference=1000.0,
        reason=None,
    )
    data["company_growth"]["changes"].append(cumulative)
    result = evaluate(data)
    assert result.state == "input_unresolved"
    assert result.missing == (
        "quarter_operating_margin_increase",
        "quarter_operating_profit_increase",
        "quarter_sales_increase",
    )


def test_explicitly_inapplicable_margin_neither_vetoes_growth_nor_becomes_a_data_gap():
    data = payloads()
    mark_financial_margin_inapplicable(data)
    assert evaluate(data).matched_paths == ("profit_growth",)
    mark_financial_margin_inapplicable(data, missing_op=True)
    # Available ordinary income must not silently replace the missing operating profit.
    data["company_financials"]["financial"].update(
        operating_profit=999, operating_profit_source="ordinary_profit"
    )
    current(data, "quarter")["metrics"]["parent_profit"] = {"value": 1234}
    result = evaluate(data)
    assert result.state == "input_unresolved"
    assert result.missing == (
        "positive_annual_operating_profit",
        "positive_quarter_operating_profit",
        "quarter_operating_profit_increase",
        "quarter_operating_profit_observed",
    )
    for measurement in ("quarter", "annual_ttm"):
        current(data, measurement)["metrics"]["operating_profit"]["reason"] = (
            "not_applicable_to_financial_sector"
        )
    change(data, "operating_profit")["reason"] = "not_applicable_to_financial_sector"
    all_inapplicable = evaluate(data)
    assert all_inapplicable.state == "not_selected"
    assert all_inapplicable.missing == ()


@pytest.mark.parametrize(
    ("sector", "sales", "profit", "state", "route", "gap"),
    [
        ("銀行業", 90, 25, "not_selected", None, False),
        ("証券・商品先物取引業", 90, 25, "not_selected", None, False),
        ("保険業", 90, 25, "not_selected", None, False),
        ("その他金融業", 90, 25, "not_selected", None, False),
        ("銀行業", 110, 25, "research_candidate", "profit_growth", False),
        ("銀行業", 110, None, "input_unresolved", None, True),
        ("情報・通信業", 90, 25, "research_candidate", "operating_recovery", False),
    ],
)
def test_generated_sector_margin_only_leads_to_a_gap_for_a_missing_viable_route(
    sector, sales, profit, state, route, gap
):
    meta = metadata()
    docs = []
    for end, revenue, op in (
        (date(2025, 3, 31), 100, 20),
        (date(2025, 12, 31), 450, 85),
        (date(2026, 3, 31), sales, profit),
    ):
        period = "FY" if end.month == 12 else "1Q"
        docs.append(
            JQuantsFinancialSummary(
                ticker="1234",
                disclosed_at=end + timedelta(days=20),
                disclosure_number=f"synthetic-{end}",
                document_type=f"{period}FinancialStatements_Consolidated_JP",
                fiscal_period=period,
                fiscal_year_end=date(end.year, 12, 31),
                period_start=date(end.year, 1, 1),
                period_end=end,
                sales=revenue,
                operating_profit=op,
                equity=500,
            )
        )
    growth = generate_company_growth(
        docs,
        ticker="1234",
        sector=sector,
        as_of=meta.as_of,
        input_release=InputRelease(release_id="fixture", manifest_sha256="a" * 64),
    )
    margin = next(
        c
        for c in growth.changes
        if c.metric == "operating_margin"
        and c.measurement == "quarter"
        and c.to_end == docs[-1].period_end
    )
    if sector != "情報・通信業":
        assert margin.difference is None
        assert margin.reason == "not_applicable_to_financial_sector"
    ta, market = technical_models()
    market.payload["master"]["sector_33"] = sector
    market.payload["price"].update(
        close=110, resolved_date=str(meta.as_of), price_basis="price_adjusted_close"
    )
    row = project_discovery_row(
        "1234",
        models=(*models({"company_growth": growth.model_dump(mode="json")}), ta, market),
        metadata=meta,
    )
    assert row["price_ready"]
    assert row["financial_state"] == state
    assert row["matched_paths"]["growth"] == ([route] if route else [])
    assert ("input_gap" in _members(row)) == gap
    assert "growth:quarter_operating_margin_increase" not in row["missing"]
    if gap:
        assert "growth:quarter_operating_profit_increase" in row["missing"]


@pytest.mark.parametrize("reason", ["nonpositive_sales", "future_reason_not_yet_classified"])
def test_only_explicit_financial_sector_inapplicability_may_be_excluded(reason):
    data = payloads()
    current(data, "annual_ttm")["metrics"]["operating_profit"]["value"] = -1
    data["market_context"] = {"master": {"sector_33": "銀行業"}}
    current(data, "quarter")["metrics"]["operating_margin"].update(value=None, reason=reason)
    check = evaluate(data)
    assert check.state == "input_unresolved"
    assert check.missing == ("quarter_operating_margin_observed",)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("statement_basis", "nonconsolidated"),
        ("accounting_standard", None),
        ("period_end", "2026-06-30"),
    ],
)
def test_balance_basis_or_date_gap_is_unresolved_not_economic_failure(field, value):
    data = payloads()
    current(data, "balance")[field] = value
    result = evaluate(data)
    assert result.state == "input_unresolved"
    assert result.missing == ("balance_quarter_basis",)


def test_comparison_intervals_cannot_be_mixed():
    data = payloads()
    change(data, "operating_profit")["from_start"] = "2025-04-01"
    result = evaluate(data)
    assert result.state == "input_unresolved"
    assert result.missing == ("growth_comparison_interval", "recovery_comparison_interval")


def test_saved_comparison_key_and_conflicting_correction_are_not_interchangeable():
    data = payloads()
    change(data, "sales")["to_start"] = int(datetime(2026, 7, 1, tzinfo=UTC).timestamp())
    assert evaluate(data).matched_paths == ("operating_recovery",)
    data = payloads()
    duplicate = deepcopy(change(data, "operating_profit"))
    duplicate["difference"] = -50.0
    data["company_growth"]["changes"].append(duplicate)
    result = evaluate(data)
    assert result.state == "input_unresolved"
    assert result.missing == (
        "growth_comparison_interval",
        "quarter_operating_profit_increase",
        "recovery_comparison_interval",
    )


@pytest.mark.parametrize("mode", ["full_fy", "metric_missing", "basis_change", "later_disclosure"])
def test_full_fy_substitutes_only_for_an_unobserved_comparable_ttm(mode):
    data = payloads()
    add_prior_fy(data)
    change(data, "operating_margin")["difference"] = 0
    if mode == "metric_missing":
        current(data, "annual_ttm")["period_status"] = "available"
    elif mode == "basis_change":
        current(data, "annual_ttm")["accounting_standard"] = "IFRS"
    elif mode == "later_disclosure":
        for item in data["company_growth"]["periods"][0]["observations"]:
            item["metrics"]["operating_profit"]["documents"][0]["disclosed_at"] = "2026-11-15"
    result, source = read(data)
    assert source.latest_full_fy.metrics["operating_profit"].value == 500.0
    assert (
        source.observation(source.latest_period, "annual_ttm").metrics["operating_profit"].value
        is None
    )
    if mode == "full_fy":
        assert result.matched_paths == ("profit_growth",)
    else:
        assert result.state == "input_unresolved"
        assert "positive_annual_operating_profit" in result.missing


@pytest.mark.parametrize(
    ("disclosed_at", "state"),
    [("2026-11-13", "not_selected"), ("2026-11-14", "research_candidate")],
)
def test_latest_known_loss_blocks_the_fy_fallback_but_a_later_disclosure_does_not(
    disclosed_at, state
):
    data = payloads()
    add_prior_fy(data)
    change(data, "operating_margin")["difference"] = 0
    loss = deepcopy(current(data, "reported")["metrics"]["operating_profit"])
    loss["value"] = -5
    loss["documents"][0]["disclosed_at"] = disclosed_at
    current(data, "reported")["metrics"]["parent_profit"] = loss
    result = evaluate(data)
    assert result.state == state
    assert result.matched_paths == (("profit_growth",) if state == "research_candidate" else ())


@pytest.mark.parametrize("generated", [False, True])
def test_missing_or_empty_growth_model_is_unknown_not_negative_equity(generated):
    data = payloads()
    if generated:
        data["company_growth"].update(
            status="unavailable", reason="no_financial_inputs", periods=[], changes=[]
        )
    else:
        del data["company_growth"]
    result, source = read(data)
    assert result.state == "input_unresolved"
    assert "positive_net_assets" in result.missing
    assert (source.model is not None) == generated


def test_disclosed_prior_does_not_backfill_the_missing_comparable_operand():
    data = payloads()
    delta = change(data, "operating_profit")
    delta["from_value"] = None
    result, source = read(data)
    assert "operating_recovery" in result.matched_paths
    assert (
        source.year_over_year(
            source.observation(source.latest_period, "quarter"), "operating_profit"
        )[0].from_value
        is None
    )
    delta.update(difference=None, rate=None, reason="accounting_standard_incomparable")
    prior = add_prior_quarter(data)
    prior["accounting_standard"] = "IFRS"
    prior["metrics"]["operating_profit"]["value"] = 1234567
    result = evaluate(data)
    assert result.state == "input_unresolved"
    assert result.missing == ("quarter_operating_profit_increase",)


def test_rejects_mixed_subjects_duplicate_growth_models_and_misattributed_payload():
    saved = models(payloads())
    with pytest.raises(ValueError, match="matching subject"):
        read_growth_discovery("9999", models=saved, metadata=metadata())
    with pytest.raises(ValueError, match="unique"):
        read_growth_discovery("1234", models=(*saved, saved[0]), metadata=metadata())
    data = payloads()
    data["company_growth"]["ticker"] = "9999"
    with pytest.raises(ValueError, match="different subject"):
        evaluate(data)
