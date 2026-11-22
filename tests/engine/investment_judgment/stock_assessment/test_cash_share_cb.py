"""Cash-face/excess-share CB arithmetic, including the published contract shape."""

import json
from decimal import Decimal

import pytest

import baibai_engine.cli.investment_judgment as workspace_cli
from baibai_engine.investment_judgment.stock_assessment.cash_share_cb import (
    CashShareCBInput,
    calculate_cash_share_cb,
)


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


def test_cli_rejects_ambiguous_cb_terms_without_output(tmp_path, capsys):
    raw = json.dumps(_case())
    term = '"settlement_vwap_yen": "2518"'
    assert raw.count(term) == 1
    contradictory = raw.replace(term, term + ', "settlement_vwap_yen": "1518"')
    path = tmp_path / "issuer-terms.json"
    path.write_text(contradictory, encoding="utf-8")
    before = path.read_bytes()
    args = ["calculate", "--cash-share-cb", str(path)]
    assert workspace_cli.main(args) == 2
    result = capsys.readouterr()
    assert result.out == ""
    assert "invalid calculation inputs YAML" in result.err
    assert "1518" not in result.err
    assert path.read_bytes() == before

    path.write_text(raw, encoding="utf-8")
    assert workspace_cli.main(args) == 0
    assert (
        json.loads(capsys.readouterr().out)["separate_settlement_vwap"]["gross_excess_shares"] > 0
    )


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


@pytest.mark.parametrize(
    ("settlement_on", "notice_on", "valid"),
    [
        ("2028-11-12", None, False),
        ("2028-11-15", None, False),
        ("2028-11-16", None, True),
        ("2028-11-16", "2028-11-16", True),
        (None, None, True),
    ],
)
def test_known_vwap_end_must_precede_settlement_even_without_notice(
    settlement_on, notice_on, valid
):
    payload = _case(
        settlement_vwap_window_start="2028-11-01",
        settlement_vwap_window_end="2028-11-15",
        settlement_notice_on=notice_on,
        settlement_on=settlement_on,
    )
    if not valid:
        with pytest.raises(ValueError, match="settlement VWAP window must precede settlement"):
            CashShareCBInput.model_validate(payload)
        return
    result = calculate_cash_share_cb(CashShareCBInput.model_validate(payload))
    assert result["settlement_vwap_window_end"] == "2028-11-15"
    assert result["settlement_notice_on"] == notice_on
    assert result["settlement_on"] == settlement_on


@pytest.mark.parametrize(
    ("effective_on", "record_date", "valid"),
    [
        ("2027-05-15", None, False),
        ("2027-05-20", None, False),
        ("2027-05-21", None, False),
        ("2027-05-22", None, True),
        ("2027-05-22", "2027-05-21", True),
        (None, None, True),
    ],
)
def test_known_reference_window_and_effective_date_leave_room_for_record_date(
    effective_on, record_date, valid
):
    payload = _case(
        special_dividend_adjustments=[
            {
                "special_dividend_per_share_yen": "38",
                "reference_close_yen": "2518",
                "reference_window_start": "2027-05-01",
                "reference_window_end": "2027-05-20",
                "final_record_date": record_date,
                "effective_on": effective_on,
            }
        ]
    )
    if not valid:
        with pytest.raises(ValueError, match="reference close window must leave a record date"):
            CashShareCBInput.model_validate(payload)
        return
    result = calculate_cash_share_cb(CashShareCBInput.model_validate(payload))
    adjustment = result["dividend_adjustments"][0]
    assert adjustment["reference_window_end"] == "2027-05-20"
    assert adjustment["final_record_date"] == record_date
    assert adjustment["effective_on"] == effective_on
    assert adjustment["conversion_price_yen"] == "2196.8"


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


def _dated_adjustments():
    return (
        {
            "special_dividend_per_share_yen": "0.4",
            "reference_close_yen": "100.0",
            "effective_on": "2027-07-01",
        },
        {
            "special_dividend_per_share_yen": "1.0",
            "reference_close_yen": "100.0",
            "effective_on": "2028-07-01",
        },
    )


@pytest.mark.parametrize("intervening_undated", [False, True])
def test_reversed_known_dividend_effective_dates_are_rejected(intervening_undated):
    early, late = _dated_adjustments()
    unknown = {k: v for k, v in early.items() if k != "effective_on"}
    with pytest.raises(
        ValueError, match="dividend adjustment effective dates must be nondecreasing"
    ):
        CashShareCBInput.model_validate(
            _case(
                special_dividend_adjustments=[
                    late,
                    *([unknown] if intervening_undated else []),
                    early,
                ]
            )
        )


@pytest.mark.parametrize("mode", ["chronological", "same_date", "undated", "intervening_undated"])
def test_dividend_adjustment_keeps_the_authors_valid_sequence(mode):
    early, late = _dated_adjustments()
    if mode == "same_date":
        late["effective_on"] = early["effective_on"]
    if mode == "undated":
        del late["effective_on"], early["effective_on"]
        adjustments, expected = [late, early], "99.0"
    else:
        # Zero is an explicit undated event, not grounds to create a date.
        intervening = (
            [{"special_dividend_per_share_yen": "0.0", "reference_close_yen": "100.0"}]
            if mode == "intervening_undated"
            else []
        )
        adjustments, expected = [early, *intervening, late], "98.6"
    result = calculate_cash_share_cb(
        CashShareCBInput.model_validate(
            _case(
                conversion_price_yen="100.0",
                special_dividend_adjustments=adjustments,
                reference_close_sensitivity_yen=[],
            )
        )
    )
    assert result["conversion_price_yen"] == expected
    assert [row["effective_on"] for row in result["dividend_adjustments"]] == [
        event.get("effective_on") for event in adjustments
    ]
