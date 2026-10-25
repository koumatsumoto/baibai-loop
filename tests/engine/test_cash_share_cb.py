"""Cash-face/excess-share CB arithmetic, including the published contract shape."""

import json
from decimal import Decimal

import pytest

from baibai_engine.investment_judgment import workspace_cli
from baibai_engine.investment_judgment.cash_share_cb import CashShareCBInput, calculate_cash_share_cb


def _case(**overrides):
    # Rounding/settlement form: https://www2.jpx.co.jp/disc/69320/140120251031585161.pdf
    # The publication starts at 2,262 yen. This scenario assumes 2,230.5 yen before
    # hypothetical dividend adjustments; it does not establish current contract terms.
    # Equity value and future prices are assumptions, not a forecast.
    payload = {
        "valuation_as_of": "2026-10-13",
        "terminal_as_of": "2028-10-13",
        "face_value_yen_per_bond": "102040000",
        "bond_count": 49,
        "settlement_group_bond_counts": [1] * 49,
        "share_unit": 100,
        "odd_lot_cash_price_basis": "settlement_vwap",
        "conversion_price_yen": "2230.5",
        "existing_shares": 14773415,
        "equity_value_yen_with_bond_liability": "39600000000",
        "special_dividend_adjustments": [
            {"special_dividend_per_share_yen": "38", "reference_close_yen": "2518"},
            {"special_dividend_per_share_yen": "38", "reference_close_yen": "2518"},
        ],
        "settlement_vwap_yen": "2518",
        "reference_close_sensitivity_yen": ["2300", "2700"],
    }
    payload.update(overrides)
    return payload


def test_contract_rounding_with_assumed_adjusted_conversion_price():
    result = calculate_cash_share_cb(CashShareCBInput.model_validate(_case()))
    assert result["face_cash_yen"] == "4999960000"
    assert [item["conversion_price_yen"] for item in result["dividend_adjustments"]] == [
        "2196.8",
        "2163.6",
    ]
    assert result["conversion_price_yen"] == "2163.6"
    assert result["unconverted"]["gross_excess_shares"] == 0
    assert Decimal(result["unconverted"]["terminal_price_yen"]) > 2600
    assert result["separate_settlement_vwap"]["settlement_vwap_yen"] == "2518"
    assert result["separate_settlement_vwap"]["gross_excess_shares"] > 0
    assert result["separate_settlement_vwap"]["delivered_shares"] % 100 == 0
    assert result["separate_settlement_vwap"]["odd_lot_shares_cash_settled"] > 0
    settlement = result["separate_settlement_vwap"]
    assert (
        (14773415 + settlement["delivered_shares"]) * Decimal(settlement["terminal_price_yen"])
        + Decimal(settlement["odd_lot_cash_yen"])
    ) == pytest.approx(Decimal("39600000000"), abs=Decimal("0.000001"))
    fixed = result["fixed_point"]
    assert Decimal(fixed["continuous_price_yen"]) == pytest.approx(2610, rel=0.01)
    # Per-bond whole-share rounding gives a two-point oscillation in this case.
    assert fixed["rounded_fixed_point_price_yen"] is None
    assert Decimal(fixed["rounding_gap_yen"][1]) - Decimal(fixed["rounding_gap_yen"][0]) < Decimal(
        "0.01"
    )
    assert fixed["gross_excess_shares_at_continuous_price"] % 49 == 0
    assert fixed["gross_excess_shares_at_continuous_price"] == 395626
    sensitivity = result["reference_close_sensitivity"]
    assert [item["reference_close_yen"] for item in sensitivity] == ["2300", "2700"]
    assert sensitivity[0]["conversion_price_yen"] < sensitivity[1]["conversion_price_yen"]
    assert [item["conversion_price_yen"] for item in sensitivity] == ["2157.3", "2168.1"]


def test_face_cash_extinguishes_existing_liability_without_second_deduction():
    result = calculate_cash_share_cb(
        CashShareCBInput.model_validate(
            _case(
                face_value_yen_per_bond="100",
                bond_count=1,
                settlement_group_bond_counts=[1],
                share_unit=1,
                conversion_price_yen="10",
                existing_shares=10,
                equity_value_yen_with_bond_liability="1000",
                special_dividend_adjustments=[],
                settlement_vwap_yen="20",
                reference_close_sensitivity_yen=[],
            )
        )
    )
    assert result["face_cash_yen"] == "100"
    assert result["separate_settlement_vwap"]["gross_excess_shares"] == 5
    assert float(result["separate_settlement_vwap"]["terminal_price_yen"]) == pytest.approx(
        1000 / 15
    )
    assert Decimal(result["unconverted"]["terminal_price_yen"]) == 100


def test_below_conversion_and_unconverted_have_no_new_shares():
    result = calculate_cash_share_cb(
        CashShareCBInput.model_validate(_case(settlement_vwap_yen="2000"))
    )
    assert result["separate_settlement_vwap"]["gross_excess_shares"] == 0
    assert (
        result["separate_settlement_vwap"]["terminal_price_yen"]
        == result["unconverted"]["terminal_price_yen"]
    )


def test_notification_grouping_controls_fractional_share_rounding():
    separate = calculate_cash_share_cb(CashShareCBInput.model_validate(_case()))
    together = calculate_cash_share_cb(
        CashShareCBInput.model_validate(_case(settlement_group_bond_counts=[49]))
    )
    assert together["face_cash_yen"] == separate["face_cash_yen"]
    assert (
        together["separate_settlement_vwap"]["gross_excess_shares"]
        >= separate["separate_settlement_vwap"]["gross_excess_shares"]
    )
    for result in (separate, together):
        item = result["separate_settlement_vwap"]
        assert (
            item["gross_excess_shares"]
            == item["delivered_shares"] + item["odd_lot_shares_cash_settled"]
        )
        assert Decimal(item["odd_lot_cash_yen"]) == (
            Decimal(item["odd_lot_shares_cash_settled"]) * Decimal("2518")
        )


def test_sub_yen_conversion_adjustment_carries_forward():
    event = {"special_dividend_per_share_yen": "0.4", "reference_close_yen": "2500"}
    result = calculate_cash_share_cb(
        CashShareCBInput.model_validate(
            _case(
                special_dividend_adjustments=[event, event, event],
                reference_close_sensitivity_yen=[],
            )
        )
    )
    assert result["dividend_adjustments"][0]["conversion_price_yen"] == "2230.5"
    assert result["dividend_adjustments"][0]["deferred_difference_yen"] != "0"
    assert Decimal(result["conversion_price_yen"]) < Decimal("2230.5")


def test_reference_price_and_settlement_vwap_dates_remain_distinct():
    adjustments = [
        {
            "special_dividend_per_share_yen": "38",
            "reference_close_yen": "2518",
            "reference_window_start": "2027-01-15",
            "reference_window_end": "2027-02-28",
            "final_record_date": "2027-03-31",
            "effective_on": "2027-06-10",
        }
    ]
    result = calculate_cash_share_cb(
        CashShareCBInput.model_validate(
            _case(
                special_dividend_adjustments=adjustments,
                settlement_vwap_window_start="2028-11-01",
                settlement_vwap_window_end="2028-11-15",
                settlement_notice_on="2028-11-16",
                settlement_on="2028-11-20",
            )
        )
    )
    assert result["dividend_adjustments"][0]["reference_close_yen"] == "2518"
    assert result["dividend_adjustments"][0]["effective_on"] == "2027-06-10"
    assert result["settlement_vwap_window_start"] == "2028-11-01"
    assert result["settlement_notice_on"] == "2028-11-16"
    assert result["settlement_on"] == "2028-11-20"
    assert result["terminal_as_of"] == "2028-10-13"


def test_cli_reuses_research_calculate_without_writing(tmp_path, capsys):
    path = tmp_path / "cb.json"
    path.write_text(json.dumps(_case()))
    before = path.read_bytes()
    assert workspace_cli.main(["calculate", "--cash-share-cb", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["face_cash_yen"] == "4999960000"
    assert path.read_bytes() == before
    assert not (tmp_path / "cb-output.json").exists()


@pytest.mark.parametrize(
    "change",
    [
        {"conversion_price_yen": "0"},
        {"settlement_vwap_yen": "NaN"},
        {"reference_close_sensitivity_yen": ["0"]},
        {"equity_value_yen_with_bond_liability": "-1"},
        {"settlement_group_bond_counts": [48]},
    ],
)
def test_invalid_assumptions_fail_closed(change):
    with pytest.raises(ValueError, match=r"positive|finite|nonnegative|every bond"):
        CashShareCBInput.model_validate(_case(**change))


def test_dividend_adjustment_that_rounds_conversion_to_zero_fails_closed():
    inputs = CashShareCBInput.model_validate(
        _case(
            conversion_price_yen="0.1",
            special_dividend_adjustments=[
                {"special_dividend_per_share_yen": "2499.9", "reference_close_yen": "2500"}
            ],
            reference_close_sensitivity_yen=[],
        )
    )
    with pytest.raises(ValueError, match="no positive conversion price"):
        calculate_cash_share_cb(inputs)
