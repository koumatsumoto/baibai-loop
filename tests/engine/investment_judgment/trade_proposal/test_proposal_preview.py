from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import timedelta
from decimal import Decimal

import pytest
from tests.helpers.proposal import PUBLISHED, backend_case, bind_review, reviewed_proposal

from baibai_engine.investment_judgment.trade_proposal.query import (
    get_investment_proposal,
    preview_current_terms,
)
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService


def _snapshot(path):
    with closing(sqlite3.connect(path)) as connection, connection:
        return {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()
            for table in (
                "investment_proposal",
                "investment_proposal_item",
                "position_decision_reference",
                "ledger_event",
                "ledger_meta",
            )
        }


def test_preview_separates_actual_quote_and_saved_limit_without_writes(tmp_path):
    case = backend_case(tmp_path)
    proposal = reviewed_proposal(case)
    TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
        proposal
    )
    before = _snapshot(case.app)
    preview = preview_current_terms(
        proposal_id=proposal.proposal_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PUBLISHED + timedelta(minutes=5),
    )
    assert preview.status == "unchanged"
    row = preview.items[0]
    assert Decimal(row["current_quote_calculation"]["calculation_price_yen"]) == Decimal(1100)
    assert row["current_quote_calculation"]["current_price_above_maximum"] is True
    assert row["saved_limit_revalidation"]["hard_blockers"] == []
    assert _snapshot(case.app) == before


def test_current_quote_change_does_not_change_saved_terms_hash_or_fixed_adv(tmp_path):
    case = backend_case(tmp_path)
    proposal = reviewed_proposal(case)
    proposal = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(proposal)
    before = _snapshot(case.app)
    with closing(sqlite3.connect(case.market)) as connection, connection:
        connection.execute("UPDATE jquants_daily_bars SET close=900")
    preview = preview_current_terms(
        proposal_id=proposal.proposal_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PUBLISHED + timedelta(minutes=5),
    )
    assert preview.status == "changed"
    assert Decimal(preview.items[0]["saved_terms"]["limit_price_yen"]) == Decimal(1000)
    assert Decimal(
        preview.items[0]["current_quote_calculation"]["calculation_price_yen"]
    ) == Decimal(900)
    assert preview.current_inputs.quotes[0].price_yen == Decimal(900)
    saved = get_investment_proposal(proposal_id=proposal.proposal_id, db_path=case.app)
    assert saved == proposal
    assert saved.liquidity_context == proposal.liquidity_context
    assert _snapshot(case.app) == before


def test_changed_target_session_blocks_preview_without_mutating_saved_terms(tmp_path):
    case = backend_case(tmp_path)
    proposal = reviewed_proposal(case)
    TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
        proposal
    )
    before = _snapshot(case.app)
    preview = preview_current_terms(
        proposal_id=proposal.proposal_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PUBLISHED + timedelta(days=2),
    )
    assert preview.status == "blocked"
    assert any(
        reason.code == "target_session_not_next_available" for reason in preview.change_reasons
    )
    assert any(
        reason["code"] == "target_session_not_next_available"
        for reason in preview.items[0]["saved_limit_revalidation"]["hard_blockers"]
    )
    assert preview.items[0]["saved_terms"]["target_session"] == "2026-10-15"
    assert "expires_at" not in preview.items[0]["saved_terms"]
    assert _snapshot(case.app) == before


def test_preview_reports_current_budget_warnings_separately_from_saved_terms(tmp_path, monkeypatch):
    import baibai_engine.investment_judgment.trade_proposal.inputs as proposal_inputs

    case = backend_case(tmp_path)
    proposal = reviewed_proposal(case)
    TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
        proposal
    )
    before = _snapshot(case.app)
    saved_warnings = list(proposal.items[0].terms.warnings)
    assert "budget_guide_under" not in saved_warnings
    monkeypatch.setattr(proposal_inputs, "DEFAULT_BUDGET_MIN_YEN", 350000)
    monkeypatch.setattr(proposal_inputs, "DEFAULT_BUDGET_MAX_YEN", 400000)
    preview = preview_current_terms(
        proposal_id=proposal.proposal_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PUBLISHED + timedelta(minutes=5),
    )
    assert preview.status == "changed"
    assert preview.current_inputs.budget_min_yen == 350000
    assert "budget_guide_under" in preview.items[0]["current_terms_warnings"]
    assert preview.items[0]["saved_terms"]["warnings"] == saved_warnings
    assert _snapshot(case.app) == before


def test_preview_does_not_require_active_operation_or_compare_current_progress(tmp_path):
    case = backend_case(tmp_path)
    proposal = reviewed_proposal(case, action="no_action")
    TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
        proposal
    )
    recorded = case.operations.record_proposal(
        case.operation.operation_id, proposal.proposal_id, now=PUBLISHED
    )
    case.operations.complete(
        recorded.operation_id,
        result="完成した比較で見送る。",
        next="重要な変化があれば新Research。",
        completed_at=PUBLISHED,
    )
    assert (
        preview_current_terms(
            proposal_id=proposal.proposal_id,
            db_path=case.app,
            sqlite_path=case.market,
            now=PUBLISHED + timedelta(minutes=5),
        ).status
        == "unchanged"
    )


@pytest.mark.parametrize("with_remaining_reward", [False, True])
def test_preview_checks_saved_remaining_reward_after_valuation_becomes_old(
    tmp_path, with_remaining_reward
):
    case = backend_case(tmp_path, holding=True)
    proposal = reviewed_proposal(case, action="hold")
    if with_remaining_reward:
        raw = proposal.payload()
        raw["items"][0]["remaining_reward"] = {
            "status": "sufficient",
            "reason": "同日起点で残存見返りが十分と確認した。",
        }
        proposal = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    proposal = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(proposal)
    before = _snapshot(case.app)
    preview = preview_current_terms(
        proposal_id=proposal.proposal_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PUBLISHED + timedelta(days=1),
    )
    assert preview.status == ("blocked" if with_remaining_reward else "changed")
    reason_codes = {reason.code for reason in preview.change_reasons}
    assert ("valuation_basis_requires_refresh" in reason_codes) == with_remaining_reward
    assert preview.items[0]["current_quote_calculation"]["current_price_projection"] is None
    assert get_investment_proposal(proposal_id=proposal.proposal_id, db_path=case.app) == proposal
    assert _snapshot(case.app) == before


@pytest.mark.parametrize(
    ("horizon_months", "expected_maximum"),
    [(12, "1067.9612"), (30, "1021.6441"), (36, "1006.6558")],
)
def test_rate_and_horizon_flow_through_context_publication_and_preview(
    tmp_path, horizon_months, expected_maximum
):
    from baibai_engine.investment_judgment.stock_assessment.document import assessment_core_hash
    from baibai_engine.investment_judgment.stock_assessment.scenario import maximum_entry_price
    from baibai_engine.investment_judgment.stock_assessment.store import load_reviewed_assessment
    from baibai_engine.investment_judgment.trade_proposal.inputs import build_proposal_context
    from baibai_engine.storage.sqlite.application.read import connect_read_only

    case = backend_case(tmp_path, required_rate="3", horizon_months=horizon_months)
    original_hash = assessment_core_hash(case.pair.document)
    with closing(connect_read_only(case.app)) as connection:
        original_pair = load_reviewed_assessment(connection, case.pair.assessment_id)
    context = build_proposal_context(
        operation_id=case.operation.operation_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PUBLISHED,
    )
    calculation = context.payload()["calculations"]["1234"]
    valuation = original_pair.document.valuation
    maximum = maximum_entry_price(
        valuation.base,
        horizon_months=valuation.horizon_months,
        required_annual_return_pct=valuation.required_annual_return_pct,
    )
    assert valuation.horizon_months == horizon_months
    assert Decimal(str(maximum)).quantize(Decimal("0.0001")) == Decimal(expected_maximum)
    assert calculation["calculation_at_maximum_price"]["hard_blockers"] == []
    assert calculation["buy_at_actual_quote"]["hard_blockers"][0]["code"] == "limit_above_pmax"
    raw = reviewed_proposal(case).payload()
    raw["items"][0]["terms"]["maximum_entry_price_yen"] = calculation[
        "calculation_at_maximum_price"
    ]["maximum_price_yen"]
    proposal = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    assert service.check(proposal).valid
    proposal = service.publish(proposal)
    before = _snapshot(case.app)
    preview = preview_current_terms(
        proposal_id=proposal.proposal_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PUBLISHED + timedelta(minutes=5),
    )
    assert preview.status == "unchanged"
    assert preview.items[0]["saved_limit_revalidation"]["hard_blockers"] == []
    assert (
        preview.items[0]["current_quote_calculation"]["hard_blockers"][0]["code"]
        == "limit_above_pmax"
    )
    for calculation_key in ("current_quote_calculation", "saved_limit_revalidation"):
        current = preview.items[0][calculation_key]
        assert Decimal(current["maximum_price_yen"]).quantize(Decimal("0.0001")) == Decimal(
            expected_maximum
        )
        assert current["current_price_projection"]["horizon_months"] == horizon_months
    assert _snapshot(case.app) == before
    assert assessment_core_hash(case.pair.document) == original_hash
    with closing(connect_read_only(case.app)) as connection:
        assert load_reviewed_assessment(connection, case.pair.assessment_id) == original_pair
    # Only the current observation changes. Saved rate, exact pair and proposal stay fixed.
    with closing(sqlite3.connect(case.market)) as connection, connection:
        connection.execute("UPDATE jquants_daily_bars SET close=1000")
    changed = preview_current_terms(
        proposal_id=proposal.proposal_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PUBLISHED + timedelta(minutes=6),
    )
    assert changed.status == "changed"
    assert changed.current_inputs.quotes[0].price_yen == Decimal(1000)
    assert changed.items[0]["current_quote_calculation"]["hard_blockers"] == []
    assert changed.items[0]["saved_limit_revalidation"]["hard_blockers"] == []
    for calculation_key in ("current_quote_calculation", "saved_limit_revalidation"):
        current = changed.items[0][calculation_key]
        assert Decimal(current["maximum_price_yen"]).quantize(Decimal("0.0001")) == Decimal(
            expected_maximum
        )
        assert current["current_price_projection"]["horizon_months"] == horizon_months
    assert get_investment_proposal(proposal_id=proposal.proposal_id, db_path=case.app) == proposal
    with closing(connect_read_only(case.app)) as connection:
        after_pair = load_reviewed_assessment(connection, case.pair.assessment_id)
    assert after_pair == original_pair
    assert after_pair.core_sha256 == original_hash
    assert after_pair.document.valuation.required_annual_return_pct == 3
    assert after_pair.document.valuation.horizon_months == horizon_months
    assert _snapshot(case.app) == before
