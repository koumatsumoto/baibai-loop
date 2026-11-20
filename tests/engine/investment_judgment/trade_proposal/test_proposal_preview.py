from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from tests.helpers.assessment_v1 import assessment_id, pair_payload, update_review_hash
from tests.helpers.proposal import (
    PREPARED,
    PUBLISHED,
    backend_case,
    bind_review,
    read_saved_proposal,
    reviewed_proposal,
)

from baibai_engine.investment_judgment.stock_assessment.store import AssessmentStoreService
from baibai_engine.investment_judgment.trade_proposal.preview import preview_current_terms
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
    saved = read_saved_proposal(proposal_id=proposal.proposal_id, db_path=case.app)
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


@pytest.mark.parametrize(
    ("calendar_state", "expected_reason"),
    [
        ("unchanged", None),
        ("missing", "order_session_unknown"),
        ("closed", "target_session_not_next_available"),
    ],
)
def test_target_session_controls_planned_quantity_but_not_per_share_valuation(
    tmp_path, calendar_state, expected_reason
):
    case = backend_case(tmp_path)
    draft = reviewed_proposal(case)
    saved = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(draft)
    target = saved.items[0].terms.target_session
    now = PUBLISHED + timedelta(minutes=5)
    assert now.date() < target
    with closing(sqlite3.connect(case.market)) as connection, connection:
        if calendar_state == "missing":
            connection.execute(
                "DELETE FROM jquants_market_calendar WHERE day=?", (target.isoformat(),)
            )
        elif calendar_state == "closed":
            connection.execute(
                "UPDATE jquants_market_calendar SET holiday_division='0' WHERE day=?",
                (target.isoformat(),),
            )
    checked = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: now).check(
        draft
    )
    with closing(sqlite3.connect(case.market)) as connection, connection:
        connection.execute("UPDATE jquants_daily_bars SET close=900")
    before = _snapshot(case.app)
    preview = preview_current_terms(
        proposal_id=saved.proposal_id, db_path=case.app, sqlite_path=case.market, now=now
    )
    row = preview.items[0]
    assert row["saved_terms"] == saved.items[0].terms.model_dump(mode="json")
    assert row["saved_order_readiness"] == "ready"
    session_codes = {"order_session_unknown", "target_session_not_next_available"}
    expected = {expected_reason} if expected_reason else set()
    assert {r.code for r in checked.blockers} & session_codes == expected
    assert {r.code for r in preview.change_reasons} & session_codes == expected
    assert {r["code"] for r in row["blockers"]} & session_codes == expected
    for trial in (
        row["saved_limit_revalidation"],
        row["current_quote_calculation"],
        checked.calculations["1234"],
    ):
        assert {r["code"] for r in trial["hard_blockers"]} & session_codes == expected
        assert trial["current_price_projection"] is not None
        assert trial["limit_price_projection"] is not None
        if expected_reason:
            assert trial["quantity"] == 0
            assert trial["maximum_quantity"] is None
            assert trial["capital_comparison"] is None
        else:
            assert trial["quantity"] > 0
            assert trial["maximum_quantity"] >= trial["quantity"]
            assert trial["capital_comparison"] is not None
    assert read_saved_proposal(proposal_id=saved.proposal_id, db_path=case.app) == saved
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
    assert preview.items[0]["saved_limit_revalidation"]["quantity"] == 300
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
    case.operations.complete(
        case.operation.operation_id,
        proposal_id=proposal.proposal_id,
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


@pytest.mark.parametrize("action", ["no_action", "defer"])
@pytest.mark.parametrize("changed_value", [False, True])
def test_nontrading_preview_reports_new_assessment_without_replacing_saved_decision(
    tmp_path, action, changed_value
):
    case = backend_case(tmp_path)
    proposal = reviewed_proposal(case, action=action)
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    saved = service.publish(proposal)
    before = preview_current_terms(
        proposal_id=saved.proposal_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PUBLISHED + timedelta(minutes=1),
    )
    assert before.status == "unchanged"
    assert (before.items[0]["current_quote_calculation"] is not None) == (action == "no_action")

    document, review = pair_payload(review_number=3)
    if changed_value:
        document["valuation"]["base"]["terminal_value_per_share_yen"] = "2200"
        review["recalculated_projections"][0]["terminal_value_per_share_yen"] = "2200"
        update_review_hash(document, review)
    newer = AssessmentStoreService(
        case.app, clock=lambda: PUBLISHED + timedelta(minutes=2)
    ).publish_reviewed_assessment(
        assessment_id(2), document, review, supersedes_id=case.pair.assessment_id
    )
    snapshot = _snapshot(case.app)
    preview = preview_current_terms(
        proposal_id=saved.proposal_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PUBLISHED + timedelta(minutes=3),
    )
    assert preview.status == "changed"
    assert [reason.code for reason in preview.change_reasons] == ["assessment_revision_changed"]
    row = preview.items[0]
    assert row["latest_assessment_id"] == newer.assessment_id
    assert row["saved_action"] == action
    assert row["current_quote_calculation"] is None
    assert row["blockers"] == []
    assert read_saved_proposal(proposal_id=saved.proposal_id, db_path=case.app) == saved
    assert service.publish(proposal) == saved
    assert _snapshot(case.app) == snapshot


def test_nontrading_preview_uses_existing_instant_order_not_text_or_id_order(tmp_path):
    case = backend_case(tmp_path)
    saved = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(reviewed_proposal(case, action="no_action"))
    prior = case.pair.assessment_id
    for number, instant in (
        (8, "2026-10-14T16:24:00.000100+09:00"),
        (3, "2026-10-14T07:24:00.000400+00:00"),
    ):
        document, review = pair_payload(review_number=number + 100)
        pair = AssessmentStoreService(
            case.app, clock=lambda instant=instant: datetime.fromisoformat(instant)
        ).publish_reviewed_assessment(assessment_id(number), document, review, supersedes_id=prior)
        prior = pair.assessment_id
    preview = preview_current_terms(
        proposal_id=saved.proposal_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PUBLISHED + timedelta(minutes=5),
    )
    assert preview.status == "changed"
    assert preview.items[0]["latest_assessment_id"] == assessment_id(3)


@pytest.mark.parametrize("unreferenced", [False, True])
def test_other_ticker_and_unreferenced_defer_do_not_replace_saved_assessment(
    tmp_path, unreferenced
):
    case = backend_case(tmp_path)
    if unreferenced:
        case.operations.record_unfinished(
            case.operation.operation_id,
            ticker="1234",
            reason="評価を今回の判断に採用する調査が未完了。",
            references=("synthetic-pending-source",),
            now=PREPARED,
        )
    saved = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(reviewed_proposal(case, action="defer" if unreferenced else "no_action"))
    document, review = pair_payload(ticker="1234" if unreferenced else "5678", review_number=3)
    AssessmentStoreService(
        case.app, clock=lambda: PUBLISHED + timedelta(minutes=1)
    ).publish_reviewed_assessment(
        assessment_id(2),
        document,
        review,
        supersedes_id=case.pair.assessment_id if unreferenced else None,
    )
    preview = preview_current_terms(
        proposal_id=saved.proposal_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PUBLISHED + timedelta(minutes=5),
    )
    assert preview.status == "unchanged"
    assert preview.change_reasons == ()
    assert read_saved_proposal(proposal_id=saved.proposal_id, db_path=case.app) == saved
    if unreferenced:
        assert saved.items[0].assessment_ref is None
        assert "latest_assessment_id" not in preview.items[0]
        assert preview.items[0]["current_quote_calculation"] is None
    else:
        assert preview.items[0]["latest_assessment_id"] == case.pair.assessment_id
        assert preview.items[0]["current_quote_calculation"] is not None


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
    assert read_saved_proposal(proposal_id=proposal.proposal_id, db_path=case.app) == proposal
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
    assert read_saved_proposal(proposal_id=proposal.proposal_id, db_path=case.app) == proposal
    with closing(connect_read_only(case.app)) as connection:
        after_pair = load_reviewed_assessment(connection, case.pair.assessment_id)
    assert after_pair == original_pair
    assert after_pair.core_sha256 == original_hash
    assert after_pair.document.valuation.required_annual_return_pct == 3
    assert after_pair.document.valuation.horizon_months == horizon_months
    assert _snapshot(case.app) == before


@pytest.mark.parametrize(
    ("selected", "current_cash", "maximum", "blocked"),
    [
        (100, 600000, 300, False),
        (100, 250000, 200, False),
        (100, 100000, 100, False),
        (100, 99999, 0, True),
        (300, 200000, 200, True),
    ],
)
def test_saved_quantity_is_revalidated_without_resizing_when_cash_changes(
    tmp_path, selected, current_cash, maximum, blocked
):
    from baibai_engine.portfolio.ledger.drafts import apply_draft, build_event_draft
    from baibai_engine.portfolio.ledger.events import ContributionEvent, WithdrawalEvent
    from baibai_engine.portfolio.ledger.store import LedgerStoreService

    case = backend_case(tmp_path, cash_yen=500000)
    raw = reviewed_proposal(case).payload()
    raw["items"][0]["terms"].update(quantity=selected, notional_yen=str(selected * 1000))
    proposal = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    check = service.check(proposal)
    assert check.valid
    assert check.calculations["1234"]["quantity"] == selected
    assert check.calculations["1234"]["maximum_quantity"] == 300
    checked_capital = check.calculations["1234"]["capital_comparison"]
    assert checked_capital["quantity_basis"] == "author_selected"
    assert Decimal(checked_capital["notional_yen"]) == selected * 1000
    assert Decimal(checked_capital["unspent_cash_yen"]) == 500000 - selected * 1000
    published = service.publish(proposal)
    store = LedgerStoreService(case.app)
    values = dict(
        event_id="synthetic-cash-change",
        occurred_at=PUBLISHED + timedelta(minutes=1),
        amount_yen=abs(current_cash - 500000),
    )
    event = (
        ContributionEvent(type="contribution", **values)
        if current_cash > 500000
        else WithdrawalEvent(type="withdrawal", **values)
    )
    apply_draft(store, build_event_draft(store, event), human_confirmed=True)
    before = _snapshot(case.app)
    preview = preview_current_terms(
        proposal_id=proposal.proposal_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PUBLISHED + timedelta(minutes=5),
    )
    row = preview.items[0]
    assert preview.status == ("blocked" if blocked else "changed")
    assert row["saved_terms"]["quantity"] == selected
    assert row["saved_limit_revalidation"]["quantity"] == (0 if blocked else selected)
    assert row["saved_limit_revalidation"]["maximum_quantity"] == maximum
    assert bool(row["saved_limit_revalidation"]["hard_blockers"]) is blocked
    capital = row["saved_limit_revalidation"]["capital_comparison"]
    if blocked:
        assert capital is None
    else:
        assert capital["quantity_basis"] == "author_selected"
        assert Decimal(capital["notional_yen"]) == selected * 1000
        assert Decimal(capital["unspent_cash_yen"]) == current_cash - selected * 1000
        assert Decimal(capital["cash_only_terminal_value_yen"]) == current_cash
        assert Decimal(capital["base"]["profit_loss_yen"]) == selected * 100
    assert read_saved_proposal(proposal_id=proposal.proposal_id, db_path=case.app) == published
    assert _snapshot(case.app) == before
