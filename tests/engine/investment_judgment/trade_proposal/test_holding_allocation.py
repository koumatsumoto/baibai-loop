import json
from decimal import Decimal
from hashlib import sha256

import pytest
from pydantic import ValidationError
from tests.helpers.proposal import PREPARED, PUBLISHED, backend_case, reviewed_proposal

from baibai_engine.cli.investment_judgment import main
from baibai_engine.investment_judgment.trade_proposal.contracts import (
    ProposalError,
    TradeProposalDraft,
)
from baibai_engine.investment_judgment.trade_proposal.holding_allocation import (
    AllocationAlternatives,
    compare_holding_allocations,
)
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService


@pytest.fixture
def draft(tmp_path):
    # Two unresolved holding outcomes may be compared without promoting either to a sale.
    raw = reviewed_proposal(backend_case(tmp_path, holding=True), action="defer").payload()
    fixed = raw["inputs"]
    fixed["available_cash_yen"] = "80"
    fixed["active_reservations"] = [
        dict(
            reservation_id="sample",
            ticker="1234",
            reserved_yen="20",
            decision_reference=None,
            sector="A",
            common_factors=[],
        ),
        dict(
            reservation_id="pending-sale",
            ticker="7777",
            reserved_yen="0",
            decision_reference=None,
            sector="B",
            common_factors=[],
        ),
    ]
    fixed["positions"][0].update(
        sector="A", quantity=200, quantity_basis_confirmed=True, cost_yen="250"
    )
    fixed["quotes"][0].update(price_yen="2")
    for ticker, qty, price, sector in [("5678", 100, "2.1", "B"), ("7777", 200, "1", "B")]:
        fixed["positions"].append(
            {
                **fixed["positions"][0],
                "ticker": ticker,
                "sector": sector,
                "quantity": qty,
                "cost_yen": "100",
            }
        )
        fixed["quotes"].append({**fixed["quotes"][0], "ticker": ticker, "price_yen": price})
        raw["research_scope"]["targets"].append({"ticker": ticker, "purpose": "holding"})
        raw["research_scope"]["outcomes"].append(
            {
                "ticker": ticker,
                "purpose": "holding",
                "status": "unfinished",
                "unfinished_reason": "資料待ち",
                "references": ["synthetic-input"],
            }
        )
        raw["items"].append(
            {
                **raw["items"][0],
                "ticker": ticker,
                "assessment_ref": None,
                "blockers": [{"code": "research_unfinished", "message": "合成保留"}],
            }
        )
    fixed["total_capital_yen"] = "910"
    return TradeProposalDraft.model_validate(raw)


def plans(**changes):
    source = {
        "known_at": PREPARED.isoformat(),
        "covered_through": "2027-03-31",
        "source_ref": "合成普通株発行者・同じ1株の権利資料",
        "yen_per_share_by_ex_date": {
            "2026-09-29": 4,
            "2026-10-14": 5,
            "2027-03-31": "0.10",
            "2027-04-01": 3,
        },
    }
    # The source may cover more than the common comparison window.
    source["covered_through"] = "2027-04-01"
    return AllocationAlternatives.model_validate(
        {
            "quote_on": "2026-10-14",
            "assumed_sale_on": "2026-10-15",
            "dividend_rights_through": "2027-03-31",
            "sector": "A",
            "gross_recovery_target_yen": 205,
            "alternatives": {"a": {"1234": 100}, "b": {"5678": 100}},
            "dividend_windows": {
                "1234": source,
                "5678": {**source, "yen_per_share_by_ex_date": {}},
            },
            **changes,
        }
    )


def test_close_proceeds_are_similar_but_concentration_is_not(draft):
    result = compare_holding_allocations(draft, plans())
    keep, a, b = result["keep_all"], *result["alternatives"].values()
    assert [x["gross_recovery_yen"] for x in (keep, a, b)] == [0, 200, 210]
    assert [x["gross_surplus_to_target_yen"] for x in (keep, a, b)] == [-205, -5, 5]
    assert [x["remaining_sector_yen"] for x in (keep, a, b)] == [400, 200, 400]
    assert (
        a["sector_pct_of_cash_and_stocks"]
        < keep["sector_pct_of_cash_and_stocks"]
        == b["sector_pct_of_cash_and_stocks"]
    )
    assert (
        a["sector_pct_of_remaining_stocks"]
        < keep["sector_pct_of_remaining_stocks"]
        < b["sector_pct_of_remaining_stocks"]
    )
    assert [x["cash_including_reserved_yen"] for x in (keep, a, b)] == [100, 300, 310]
    assert [x["foregone_forecast_dividend_yen"] for x in (keep, a, b)] == [0, 10, 0]
    assert a["whole_positions_gross_spread_to_recorded_cost_yen"] is None
    assert b["whole_positions_gross_spread_to_recorded_cost_yen"] == 110
    assert result["recorded_active_reservation_tickers"] == ["1234", "7777"]
    assert [x["cash_including_reserved_yen"] + x["remaining_stocks_yen"] for x in (keep, a, b)] == [
        910
    ] * 3


@pytest.mark.parametrize("change", ["missing", "short", "unknown"])
def test_unknown_dividend_is_not_zero_or_a_missing_price(draft, change):
    raw = plans().model_dump(mode="json")
    windows = raw["dividend_windows"]
    if change == "missing":
        del windows["1234"]
    elif change == "short":
        windows["1234"].update(covered_through="2027-02-28", yen_per_share_by_ex_date={})
    else:
        windows["1234"]["yen_per_share_by_ex_date"]["2027-03-31"] = None
    result = compare_holding_allocations(draft, AllocationAlternatives.model_validate(raw))
    a, b = result["alternatives"].values()
    assert a["foregone_forecast_dividend_yen"] is None
    assert a["incomplete_dividend_tickers"] == ["1234"]
    assert a["gross_recovery_yen"] == 200
    assert (
        b["foregone_forecast_dividend_yen"]
        == result["keep_all"]["foregone_forecast_dividend_yen"]
        == 0
    )


def test_reference_date_share_units_and_partial_information(draft):
    raw = draft.payload()
    raw["inputs"]["positions"][2]["quantity_basis_confirmed"] = False
    raw["inputs"]["total_capital_yen"] = None
    obj = TradeProposalDraft.model_validate(raw)
    unknown = compare_holding_allocations(obj, plans())
    assert unknown["alternatives"]["a"]["gross_recovery_yen"] == 200
    assert unknown["alternatives"]["a"]["cash_pct_of_cash_and_stocks"] is None
    assert unknown["alternatives"]["a"]["remaining_sector_yen"] == 200
    assert unknown["recorded_total_capital_yen"] is None
    assumed = compare_holding_allocations(obj, plans(assume_unchanged_share_basis=True))
    assert assumed["alternatives"]["a"]["cash_pct_of_cash_and_stocks"] is not None
    assert assumed["recorded_total_capital_yen"] is None
    assert assumed["unconfirmed_share_basis_tickers"] == ["7777"]
    raw["inputs"]["quotes"][2]["price_as_of"] = "2026-10-13"
    no_quote = compare_holding_allocations(
        TradeProposalDraft.model_validate(raw), plans(assume_unchanged_share_basis=True)
    )
    assert no_quote["missing_reference_close_tickers"] == ["7777"]
    assert no_quote["alternatives"]["a"]["cash_pct_of_cash_and_stocks"] is None
    assert no_quote["alternatives"]["a"]["foregone_forecast_dividend_yen"] == 10


def test_liquidation_has_no_remaining_stock_denominator(draft):
    r = compare_holding_allocations(
        draft,
        plans(
            gross_recovery_target_yen=None,
            alternatives={"all": {"1234": 200, "5678": 100, "7777": 200}},
        ),
    )["alternatives"]["all"]
    assert (r["gross_recovery_yen"], r["cash_pct_of_cash_and_stocks"]) == (810, 100)
    assert r["remaining_sector_yen"] == r["sector_pct_of_cash_and_stocks"] == 0
    assert r["sector_pct_of_remaining_stocks"] is None
    assert r["gross_surplus_to_target_yen"] is None
    assert r["foregone_forecast_dividend_yen"] is None


@pytest.mark.parametrize(
    "bad",
    [
        {"quote_on": "2026-10-15"},
        {"assumed_sale_on": "2026-10-14"},
        {"assumed_sale_on": "2026-10-17"},
        {"dividend_rights_through": "2026-10-13"},
        {"sector": "A typo"},
        {"alternatives": {"outside": {"2222": 100}}},
        {"alternatives": {"over": {"1234": 300}}},
        {"alternatives": {"odd": {"1234": 99}}},
        {"alternatives": {"nothing": {}}},
        {"alternatives": {"bad": {"1234": True}}},
        {"gross_recovery_target_yen": "NaN"},
    ],
)
def test_refuses_invalid_or_out_of_scope_hypothesis(draft, bad):
    with pytest.raises((ProposalError, ValidationError)):
        compare_holding_allocations(draft, plans(**bad))


def test_next_session_ex_rights_keeps_accrued_income_but_cannot_reuse_cum_close(draft):
    raw = plans().model_dump(mode="json")
    raw["dividend_windows"]["1234"]["yen_per_share_by_ex_date"]["2026-10-15"] = 6
    result = compare_holding_allocations(draft, AllocationAlternatives.model_validate(raw))
    a, b = result["alternatives"].values()
    assert result["known_ex_rights_since_quote_tickers"] == ["1234"]
    assert result["recorded_total_capital_yen"] == 910
    assert a["foregone_forecast_dividend_yen"] == 10  # Only the later entitlement.
    assert a["gross_recovery_yen"] is None
    assert a["cash_including_reserved_yen"] is None
    assert b["gross_recovery_yen"] == 210
    assert b["sector_pct_of_cash_and_stocks"] is None


def test_unknown_share_basis_cannot_invent_zero_remaining_stocks_or_exposure(draft):
    raw = draft.payload()
    raw["inputs"]["positions"][0]["quantity_basis_confirmed"] = False
    raw["inputs"]["total_capital_yen"] = None
    obj = TradeProposalDraft.model_validate(raw)
    all_sold = {"all": {"1234": 200, "5678": 100, "7777": 200}}
    r = compare_holding_allocations(obj, plans(alternatives=all_sold))["alternatives"]["all"]
    assert r["remaining_stocks_yen"] is None
    assert r["remaining_sector_yen"] is None
    assumed = compare_holding_allocations(
        obj, plans(alternatives=all_sold, assume_unchanged_share_basis=True)
    )
    assert assumed["alternatives"]["all"]["remaining_sector_yen"] == 0
    assert assumed["recorded_total_capital_yen"] is None


def test_known_ex_date_of_unreviewed_holding_affects_denominator_not_reduction_scope(draft):
    raw = draft.payload()
    for section in ("targets", "outcomes"):
        raw["research_scope"][section] = raw["research_scope"][section][:-1]
    raw["items"] = raw["items"][:-1]
    obj = TradeProposalDraft.model_validate(raw)
    plans_raw = plans().model_dump(mode="json")
    windows = plans_raw["dividend_windows"]
    windows["7777"] = {**windows["1234"], "yen_per_share_by_ex_date": {"2026-10-15": 1}}
    result = compare_holding_allocations(obj, AllocationAlternatives.model_validate(plans_raw))
    assert result["known_ex_rights_since_quote_tickers"] == ["7777"]
    assert result["alternatives"]["a"]["gross_recovery_yen"] == 200
    assert result["alternatives"]["a"]["cash_pct_of_cash_and_stocks"] is None
    plans_raw["alternatives"]["outside"] = {"7777": 100}
    with pytest.raises(ProposalError, match="fixed holdings review"):
        compare_holding_allocations(obj, AllocationAlternatives.model_validate(plans_raw))


def test_empty_rights_window_cannot_lose_a_future_right(draft):
    r = compare_holding_allocations(
        draft, plans(dividend_rights_through="2026-10-15", dividend_windows={})
    )
    assert r["alternatives"]["a"]["foregone_forecast_dividend_yen"] == 0


def test_normalized_duplicate_dates_and_names_are_not_silently_overwritten():
    raw = plans().model_dump(mode="json")
    raw["dividend_windows"]["1234"]["yen_per_share_by_ex_date"]["20270331"] = 15
    with pytest.raises(ValidationError, match="duplicate ex-rights date"):
        AllocationAlternatives.model_validate(raw)
    with pytest.raises(ValidationError, match="duplicate comparison name"):
        plans(alternatives={"plan A": {"1234": 100}, " plan A  ": {"5678": 100}})


def test_future_source_is_not_admitted(draft):
    raw = plans().model_dump(mode="json")
    raw["dividend_windows"]["1234"]["known_at"] = "2026-10-14T16:10:00.000001+09:00"
    with pytest.raises(ProposalError, match="cutoff"):
        compare_holding_allocations(draft, AllocationAlternatives.model_validate(raw))


def test_cli_draft_and_exact_saved_source_use_same_inputs_without_writes(tmp_path, capsys):
    case = backend_case(tmp_path, holding=True)
    proposal = reviewed_proposal(case, action="hold")
    issued = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(proposal)
    draft_path = tmp_path / "draft.json"
    draft_path.write_text(proposal.model_dump_json())
    case_file = tmp_path / "case.json"
    case_file.write_text(
        plans(
            sector=proposal.inputs.positions[0].sector,
            alternatives={"full": {"1234": 100}},
            dividend_windows={},
        ).model_dump_json()
    )
    before = sha256(case.app.read_bytes()).hexdigest()
    cmd = ["trade-proposal-compare-holdings", str(case_file)]
    assert main([*cmd, "--draft", str(draft_path)]) == 0
    drafted = json.loads(capsys.readouterr().out)
    assert main([*cmd, "--db", str(case.app), "--proposal-id", issued.proposal_id]) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved.pop("source_role") == "exact_saved_proposal_not_current_terms"
    assert drafted.pop("source_role") == "draft_not_verified_publication"
    assert saved == drafted
    assert saved["draft_sha256"] == issued.review.draft_sha256
    assert Decimal(saved["alternatives"]["full"]["gross_recovery_yen"]) == 110000
    assert sha256(case.app.read_bytes()).hexdigest() == before
