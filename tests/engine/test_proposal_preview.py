from __future__ import annotations

import sqlite3
from datetime import timedelta
from decimal import Decimal

from tests.helpers.proposal_v1 import PUBLISHED, backend_case, reviewed_proposal

from baibai_engine.research.proposal_query import get_investment_proposal, preview_current_terms
from baibai_engine.research.proposal_service import InvestmentProposalService


def _snapshot(path):
    with sqlite3.connect(path) as connection:
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
    InvestmentProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
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
    InvestmentProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
        proposal
    )
    before = _snapshot(case.app)
    with sqlite3.connect(case.market) as connection:
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


def test_expired_preview_is_blocked_and_does_not_invent_expiry_fact(tmp_path):
    case = backend_case(tmp_path)
    proposal = reviewed_proposal(case)
    InvestmentProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
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
    assert preview.items[0]["saved_terms"]["expires_at"] == "2026-10-15T15:30:00+09:00"
    assert _snapshot(case.app) == before


def test_preview_reports_current_budget_warnings_separately_from_saved_terms(tmp_path, monkeypatch):
    from baibai_engine.research import proposal_inputs

    case = backend_case(tmp_path)
    proposal = reviewed_proposal(case)
    InvestmentProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
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
    InvestmentProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
        proposal
    )
    recorded = case.operations.record_proposal(
        case.operation.operation_id, proposal.proposal_id, now=PUBLISHED
    )
    case.operations.complete(
        recorded.operation_id,
        recorded.payload.model_copy(
            update={"result": "完成した比較で見送る。", "next": "重要な変化があれば新Research。"}
        ),
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
