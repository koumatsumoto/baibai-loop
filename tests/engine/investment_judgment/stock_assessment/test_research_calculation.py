"""Draft arithmetic shares existing checks and leaves publication and inputs intact."""

import json
import socket
import sqlite3
from copy import deepcopy
from datetime import datetime
from decimal import Decimal

import pytest
from tests.helpers.assessment_v1 import pair_payload

import baibai_engine.cli.investment_judgment as workspace_cli
from baibai_engine.cli.assessment_calculation import main
from baibai_engine.investment_judgment.independent_review.contracts import (
    AssessmentReview,
)
from baibai_engine.investment_judgment.stock_assessment.calculation import (
    CalculationInput,
    calculate,
)
from baibai_engine.investment_judgment.stock_assessment.document import (
    StockAssessmentDocument,
    UnpublishedAssessment,
    assessment_core_hash,
    evaluate_assessment,
)

NOW = datetime.fromisoformat("2026-09-07T18:00:00+09:00")


@pytest.fixture(autouse=True)
def forbid_database_and_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("calculate must not connect to a database or network")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)


def inputs_payload(*, numerator=1, denominator=3, numerator_unit="JPY", denominator_unit="JPY"):
    assessment, _ = pair_payload(as_of="2026-09-07", quote_as_of="2026-09-04")
    assessment["input_snapshot"]["facts"][0]["value"] = 1000
    payload = {key: deepcopy(assessment[key]) for key in ("input_snapshot", "valuation")}
    for name, value, unit in (
        ("numerator", numerator, numerator_unit),
        ("denominator", denominator, denominator_unit),
    ):
        payload["input_snapshot"]["facts"].append(
            dict(
                fact_id=name,
                fact_kind="other",
                value=value,
                unit=unit,
                as_of="2026-09-04",
                source_ids=["annual"],
            )
        )
    payload["derived"] = {
        "metrics": [
            dict(
                metric_id="ratio",
                unit="JPY_per_share"
                if denominator_unit == "shares" and numerator_unit == "JPY"
                else "ratio",
                as_of="2026-09-04",
                input_fact_ids=["numerator", "denominator"],
                assumption="同じ期間・株式basisの一次資料から独立確認",
                source_ids=["annual"],
            )
        ]
    }
    return payload


def evaluated_pair(assessment, review):
    document = StockAssessmentDocument.model_validate(assessment)
    review = deepcopy(review)
    review["reviewed_assessment_sha256"] = assessment_core_hash(document)
    return evaluate_assessment(
        document,
        review=AssessmentReview.model_validate(review),
        identity=UnpublishedAssessment.DRAFT,
        now=NOW,
    )


def test_cli_generates_ratio_compatible_with_existing_tolerance_without_writes(tmp_path, capsys):
    payload = inputs_payload()
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps(payload))
    before = path.read_bytes()
    paths_before = set(tmp_path.rglob("*"))
    assert workspace_cli.main(["calculate", str(path)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    result = json.loads(captured.out)
    metric = result["derived"]["metrics"][0]
    assert metric["value"] == 0.3333333333333333
    assert metric["formula"] == "ratio"
    assert metric["formula_version"] == "ratio-v1"
    assessment, review = pair_payload(as_of="2026-09-07", quote_as_of="2026-09-04")
    assessment.update(
        input_snapshot=payload["input_snapshot"],
        derived=result["derived"],
        valuation=result["valuation"],
    )
    review["recalculated_projections"] = result["recalculated_projections"]
    assert evaluated_pair(assessment, review).assessment_readiness == "ready"
    assessment["derived"]["metrics"][0]["value"] = 0.3333
    assert any("value mismatch" in error for error in evaluated_pair(assessment, review).errors)
    assert path.read_bytes() == before
    assert set(tmp_path.rglob("*")) == paths_before


@pytest.mark.parametrize(
    ("price", "terminal", "cash", "months", "rate", "total", "annual", "pmax"),
    [
        (1000, 1204, 30, 12, 12, 23.4, 23.4, 1101.7857142857142),
        (1000, 1210, 0, 24, 10, 21, 10, 1000),
        (1000, 1210, 0, 6, 21, 21, 46.41, 1100),
        (1000, 0, 0, 12, 12, -100, -100, 0),
        (1000, 800, 200, 12, 12, 0, 0, 892.8571428571429),
        # Public study sensitivities: arithmetic, not validation of the business assumptions.
        # The 30-month case includes an unpaid right in terminal, not cash received.
        (
            2508,
            2673.6552366347546,
            240,
            30,
            6,
            16.174451221481445,
            6.18037427794863,
            2518.68292909091,
        ),
        (2539, 2544, 40, 36, 6, 1.7723513194170933, 0.5873274840467164, 2169.576227355468),
        (
            2539,
            3138.972900283149,
            40,
            36,
            6,
            25.20570698240052,
            7.780793084579649,
            2669.126947314855,
        ),
    ],
)
def test_projection_numbers_use_recorded_price_horizon_rate(
    price, terminal, cash, months, rate, total, annual, pmax
):
    payload = inputs_payload()
    payload["input_snapshot"]["facts"][0]["value"] = price
    payload["valuation"].update(horizon_months=months, required_annual_return_pct=rate)
    payload["valuation"]["base"].update(
        terminal_value_per_share_yen=terminal, cash_distribution_per_share_yen=cash
    )
    result = calculate(CalculationInput.model_validate(payload))
    numbers = result["projection_numbers"]
    assert numbers["price_yen"] == price
    assert numbers["valuation_as_of"] == "2026-09-07"
    assert numbers["price_as_of"] == "2026-09-04"
    assert numbers["horizon_months"] == months
    assert numbers["required_annual_return_pct"] == rate
    assert numbers["base"]["total_value_yen"] == terminal + cash
    assert numbers["base"]["total_return_pct"] == pytest.approx(total)
    assert numbers["base"]["annualized_return_pct"] == pytest.approx(annual)
    assert numbers["base"]["pmax_raw_yen"] == pytest.approx(pmax)


@pytest.mark.parametrize(
    ("basis", "observed_at"),
    [
        ("realtime", "2026-09-04T09:10:00+09:00"),
        ("realtime", "2026-09-04T00:10:00Z"),
        ("last_close_unadjusted", "2026-09-04T15:30:00+09:00"),
        ("last_close_unadjusted", None),
    ],
)
def test_projection_keeps_selected_price_clock_without_inventing_missing_time(basis, observed_at):
    payload = inputs_payload()
    fact = payload["input_snapshot"]["facts"][0]
    other = {**fact, "fact_id": "other-price", "value": 2000}
    payload["input_snapshot"]["facts"].insert(0, other)
    fact.update(price_basis=basis, observed_at=observed_at)
    payload["price_comparison"] = {"price_yen": 900}
    before = deepcopy(payload)
    result = calculate(CalculationInput.model_validate(payload))
    numbers = result["projection_numbers"]
    assert numbers["price_yen"] == 1000
    assert numbers["price_basis"] == basis
    assert numbers["price_observed_at"] == (
        datetime.fromisoformat(observed_at).isoformat() if observed_at is not None else None
    )
    assert "price_observed_at" not in result["price_comparison_numbers"]
    assert payload == before


def test_comparison_keeps_observation_and_review_materials_and_uses_hypothetical_quantity():
    payload = inputs_payload()
    payload["input_snapshot"]["facts"][0]["value"] = 8511
    payload["valuation"].update(horizon_months=24, required_annual_return_pct=12)
    payload["valuation"]["base"].update(
        terminal_value_per_share_yen="7439.5959595959595959595959595959595959595959595958",
        cash_distribution_per_share_yen=0,
    )
    payload["valuation"]["downside"].update(
        terminal_value_per_share_yen="2459.797979797979797979797979797979797979797979798",
        cash_distribution_per_share_yen=0,
    )
    before = deepcopy(payload)
    original = calculate(CalculationInput.model_validate(payload))
    payload["price_comparison"] = {"price_yen": 6000, "hypothetical_quantity": 100}
    result = calculate(CalculationInput.model_validate(payload))
    comparison = result.pop("price_comparison_numbers")
    assert result == original
    assert payload["input_snapshot"] == before["input_snapshot"]
    assert payload["valuation"] == before["valuation"]
    assert comparison["price_context"] == "hypothetical_entry"
    assert comparison["price_yen"] == 6000
    assert "price_as_of" not in comparison
    assert "price_basis" not in comparison
    assert comparison["hypothetical_quantity"] == 100
    assert comparison["hypothetical_outlay_yen"] == "600000"
    assert comparison["base"]["annualized_return_pct"] == pytest.approx(11.3522635573)
    assert comparison["downside"]["annualized_return_pct"] == pytest.approx(-35.9713868676)
    assert float(comparison["base"]["profit_loss_yen"]) == pytest.approx(143959.5959596)
    assert float(comparison["downside"]["profit_loss_yen"]) == pytest.approx(-354020.2020202)
    assert (
        comparison["base"]["pmax_raw_yen"] == original["projection_numbers"]["base"]["pmax_raw_yen"]
    )
    assert comparison["base"]["pmax_raw_yen"] < 6000
    assert comparison["execution_assumption"] == "full_fill_at_comparison_price"
    assert comparison["investor_taxes_and_fees_included"] is False
    assert comparison["return_basis"] == "conditional_pretax_without_reinvestment"
    assert comparison["order_readiness_assessed"] is False
    assert not {"available_cash_yen", "unspent_cash_yen", "maximum_quantity"} & comparison.keys()


def test_comparison_without_quantity_does_not_invent_capital_or_sizing():
    payload = inputs_payload()
    payload["price_comparison"] = {"price_yen": 1000}
    result = calculate(CalculationInput.model_validate(payload))
    comparison = result["price_comparison_numbers"]
    assert "hypothetical_quantity" not in comparison
    assert "hypothetical_outlay_yen" not in comparison
    for name in ("base", "downside"):
        assert comparison[name] == result["projection_numbers"][name]
        assert "profit_loss_yen" not in comparison[name]


def test_comparison_is_not_a_board_lot_rule_and_counts_distribution_once():
    payload = inputs_payload()
    payload["price_comparison"] = {"price_yen": "999.999", "hypothetical_quantity": 1}
    payload["valuation"]["base"].update(
        terminal_value_per_share_yen="1000.125", cash_distribution_per_share_yen="100.875"
    )
    payload["valuation"]["downside"].update(
        terminal_value_per_share_yen=0, cash_distribution_per_share_yen=0
    )
    comparison = calculate(CalculationInput.model_validate(payload))["price_comparison_numbers"]
    assert comparison["hypothetical_outlay_yen"] == "999.999"
    assert Decimal(comparison["base"]["investment_total_value_yen"]) == Decimal(1101)
    assert Decimal(comparison["base"]["profit_loss_yen"]) == Decimal("101.001")
    assert Decimal(comparison["downside"]["profit_loss_yen"]) == Decimal("-999.999")
    assert comparison["downside"]["annualized_return_pct"] == -100


@pytest.mark.parametrize("price", [0, -1, True, "bad", float("nan"), float("inf")])
def test_invalid_comparison_price_is_rejected(price):
    payload = inputs_payload()
    payload["price_comparison"] = {"price_yen": price}
    with pytest.raises(ValueError, match="price_yen"):
        calculate(CalculationInput.model_validate(payload))


@pytest.mark.parametrize("quantity", [0, -1, True, 1.5, "100", Decimal(100)])
def test_comparison_quantity_is_an_explicit_positive_integer(quantity):
    payload = inputs_payload()
    payload["price_comparison"] = {"price_yen": 1000, "hypothetical_quantity": quantity}
    with pytest.raises(ValueError, match="hypothetical_quantity"):
        calculate(CalculationInput.model_validate(payload))


@pytest.mark.parametrize("valuation", [None, "unresolved"])
def test_comparison_requires_projections_without_creating_or_adopting_them(valuation):
    payload = inputs_payload()
    payload["price_comparison"] = {"price_yen": 1000}
    payload["valuation"] = (
        None
        if valuation is None
        else {
            "status": "unresolved",
            "market_price_fact_id": None,
            "horizon_months": None,
            "required_annual_return_pct": None,
            "base": None,
            "downside": None,
            "unresolved_reason": "未採択",
        }
    )
    with pytest.raises(ValueError, match="price comparison requires a resolved valuation"):
        calculate(CalculationInput.model_validate(payload))


def test_comparison_does_not_replace_an_invalid_observed_price_basis():
    payload = inputs_payload()
    payload["input_snapshot"]["facts"][0]["price_basis"] = "last_close_adjusted"
    payload["price_comparison"] = {"price_yen": 1000}
    with pytest.raises(ValueError, match="numeric unadjusted market_price"):
        calculate(CalculationInput.model_validate(payload))


def test_cli_comparison_does_not_change_input_or_create_files(tmp_path, capsys):
    payload = inputs_payload()
    payload["price_comparison"] = {"price_yen": 900, "hypothetical_quantity": 100}
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps(payload))
    before = path.read_bytes()
    paths_before = set(tmp_path.rglob("*"))
    assert main([str(path)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    result = json.loads(captured.out)
    assert result["projection_numbers"]["price_yen"] == 1000
    assert result["price_comparison_numbers"]["hypothetical_outlay_yen"] == "90000"
    assert path.read_bytes() == before
    assert set(tmp_path.rglob("*")) == paths_before


@pytest.mark.parametrize(
    ("numerator_unit", "denominator_unit", "unit", "value"),
    [
        ("JPY", "shares", "JPY_per_share", 1204),
        ("JPY", "JPY", "ratio", 1204),
        ("shares", "shares", "ratio", 1204),
    ],
)
def test_existing_ratio_units(numerator_unit, denominator_unit, unit, value):
    result = calculate(
        CalculationInput.model_validate(
            inputs_payload(
                numerator=1204000000,
                denominator=1000000,
                numerator_unit=numerator_unit,
                denominator_unit=denominator_unit,
            )
        )
    )
    assert result["derived"]["metrics"][0]["unit"] == unit
    assert result["derived"]["metrics"][0]["value"] == value


@pytest.mark.parametrize("value", [True, "3", float("nan"), float("inf"), 10**31])
def test_invalid_ratio_fact_values(value):
    with pytest.raises(ValueError, match=r"numeric|validation error"):
        calculate(CalculationInput.model_validate(inputs_payload(numerator=value)))


@pytest.mark.parametrize(
    "fault",
    [
        "zero",
        "missing",
        "unit",
        "duplicate_fact",
        "duplicate_metric",
        "value",
        "formula",
        "ratio_overflow",
    ],
)
def test_invalid_ratio_definition_does_not_emit_partial_output(tmp_path, capsys, fault):
    payload = inputs_payload()
    metric = payload["derived"]["metrics"][0]
    if fault == "zero":
        payload["input_snapshot"]["facts"][-1]["value"] = 0
    elif fault == "missing":
        metric["input_fact_ids"][1] = "missing"
    elif fault == "unit":
        metric["unit"] = "percent"
    elif fault == "duplicate_fact":
        payload["input_snapshot"]["facts"].append(deepcopy(payload["input_snapshot"]["facts"][0]))
    elif fault == "duplicate_metric":
        payload["derived"]["metrics"].append(deepcopy(metric))
    elif fault == "ratio_overflow":
        payload["input_snapshot"]["facts"][-1]["value"] = 1e-310
    else:
        metric[fault] = 1 if fault == "value" else "eval"
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps(payload))
    assert main([str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")


@pytest.mark.parametrize(
    "fault", ["missing", "unit", "adjusted", "zero", "negative", "bool", "string", "non_price"]
)
def test_invalid_projection_price(fault):
    payload = inputs_payload()
    price = payload["input_snapshot"]["facts"][0]
    if fault == "missing":
        payload["valuation"]["market_price_fact_id"] = "missing"
    elif fault == "unit":
        price["unit"] = "JPY"
    elif fault == "adjusted":
        price["price_basis"] = "last_close_adjusted"
    elif fault == "non_price":
        payload["valuation"]["market_price_fact_id"] = "numerator"
    else:
        price["value"] = {"zero": 0, "negative": -1, "bool": True, "string": "1000"}[fault]
    with pytest.raises(ValueError, match=r"price|positive"):
        calculate(CalculationInput.model_validate(payload))


@pytest.mark.parametrize("unresolved", [False, True])
def test_missing_valuation_has_no_manufactured_projection(unresolved):
    payload = inputs_payload()
    payload["input_snapshot"]["facts"] = payload["input_snapshot"]["facts"][1:]
    payload["valuation"] = (
        dict(
            status="unresolved",
            market_price_fact_id=None,
            horizon_months=None,
            required_annual_return_pct=None,
            base=None,
            downside=None,
            unresolved_reason="期間・価値未確定",
        )
        if unresolved
        else None
    )
    result = calculate(CalculationInput.model_validate(payload))
    assert result["projection_numbers"] is None
    assert result["recalculated_projections"] == []
    assert result["derived"]["metrics"][0]["value"] == 0.3333333333333333


def test_calculation_keeps_saved_core_and_independent_review_semantics():
    assessment, review = pair_payload(as_of="2026-09-07", quote_as_of="2026-09-04")
    before = deepcopy((assessment, review))
    original_hash = assessment_core_hash(StockAssessmentDocument.model_validate(assessment))
    author_inputs = {key: deepcopy(assessment[key]) for key in ("input_snapshot", "valuation")}
    author = calculate(CalculationInput.model_validate(author_inputs))
    # Equivalent numeric serialization must retain the existing canonical core/hash.
    assessment["valuation"] = author["valuation"]
    review["recalculated_projections"] = author["recalculated_projections"]
    assert assessment_core_hash(StockAssessmentDocument.model_validate(assessment)) == original_hash
    assert evaluated_pair(assessment, review).assessment_readiness == "ready"
    reviewer_inputs = deepcopy(author_inputs)
    reviewer_inputs["valuation"]["base"]["terminal_value_per_share_yen"] = 1260
    reviewer = calculate(CalculationInput.model_validate(reviewer_inputs))
    review["recalculated_projections"] = reviewer["recalculated_projections"]
    assert any("terminal" in error for error in evaluated_pair(assessment, review).errors)
    assert author_inputs == {key: before[0][key] for key in ("input_snapshot", "valuation")}
    # No prose parser: the same incorrect terminal from both passes still agrees.
    assessment["valuation"] = reviewer["valuation"]
    assert evaluated_pair(assessment, review).assessment_readiness == "ready"


def test_assessment_ratio_reports_unit_and_value_errors_together():
    payload = inputs_payload()
    result = calculate(CalculationInput.model_validate(payload))
    assessment, review = pair_payload(as_of="2026-09-07", quote_as_of="2026-09-04")
    assessment.update(input_snapshot=payload["input_snapshot"], derived=result["derived"])
    assessment["derived"]["metrics"][0].update(unit="percent", value=0.3333)
    errors = evaluated_pair(assessment, review).errors
    assert any("unit mismatch" in error for error in errors)
    assert any("value mismatch" in error for error in errors)
