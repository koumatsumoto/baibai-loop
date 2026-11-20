"""保有判断は既存注文の取消ではない。台帳予約への注意は採否や取引を変えない。"""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from functools import partial

import pytest
from tests.helpers import proposal as helper

from baibai_engine.investment_judgment.trade_proposal import service as service_module
from baibai_engine.investment_judgment.trade_proposal.contracts import ProposalConflictError
from baibai_engine.investment_judgment.trade_proposal.preview import preview_current_terms
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.investment_judgment.trade_proposal.warnings import build_proposal_warnings
from baibai_engine.portfolio.ledger.decision_references import resolve_buy_reference
from baibai_engine.portfolio.ledger.drafts import apply_draft, build_broker_fact_draft
from baibai_engine.portfolio.ledger.store import LedgerStoreService
from baibai_web.sources.proposal import read_proposal_views

WARNING = "hold_active_reservation_requires_confirmation:1234"


def _order(case, *, side="sell", ticker="1234", status="open", now=helper.PUBLISHED, expires=None):
    store = LedgerStoreService(case.app)
    draft, _ = build_broker_fact_draft(
        store,
        partial(resolve_buy_reference, db_path=case.app),
        decision_reference=None if side == "buy" else "synthetic-working-sell",
        direct_fact=side == "buy",
        status=status,
        side=side,
        occurred_at=now - timedelta(minutes=11),
        ticker=ticker,
        quantity=100 if status == "open" else None,
        price_guard_yen=Decimal(800) if status == "open" else None,
        sector="情報・通信業" if status == "open" else None,
        common_factors=("domestic-demand",) if status == "open" else None,
        reservation_id="synthetic-working-order",
        expires_at=expires,
        now=now,
    )
    assert draft is not None
    apply_draft(store, draft, human_confirmed=True)
    return store


@pytest.mark.parametrize("sufficient", [False, True])
def test_normal_hold_keeps_same_ticker_zero_cash_reservation_visible_without_blocking(
    tmp_path, sufficient
):
    case = helper.backend_case(tmp_path, holding=True, active_sell=True)
    raw = helper.reviewed_proposal(case, action="hold").payload()
    if sufficient:
        raw["items"][0].update(
            holding_basis="sufficient_remaining_reward",
            remaining_reward={"status": "sufficient", "reason": "合成の保有継続比較"},
        )
    draft = helper.bind_review(raw, {case.pair.assessment_id: case.pair.document})
    service = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: helper.PUBLISHED
    )
    ledger_before = LedgerStoreService(case.app).load_with_head()
    result = service.check(draft)
    assert result.valid
    assert result.blockers == ()
    saved = service.publish(draft)
    assert saved.warnings == (WARNING,)
    assert saved.inputs.active_reservations[0].reserved_yen == 0
    assert saved.items[0] == draft.items[0]
    assert saved.inputs.available_cash_yen == 210000
    assert LedgerStoreService(case.app).load_with_head() == ledger_before
    before = case.app.read_bytes()
    assert read_proposal_views(case.app).latest.proposal.warnings == [WARNING]
    assert helper.read_saved_proposal(proposal_id=saved.proposal_id, db_path=case.app) == saved
    assert service.publish(draft) == saved
    assert case.app.read_bytes() == before


@pytest.mark.parametrize(
    ("side", "ticker", "warns"), [("buy", "1234", True), ("buy", "5678", False)]
)
def test_warning_uses_matching_reservation_not_side_or_cash_amount(tmp_path, side, ticker, warns):
    case = helper.backend_case(tmp_path, holding=True)
    store = _order(case, side=side, ticker=ticker)
    draft = helper.reviewed_proposal(case, action="hold")
    ledger_before = store.load_with_head()
    service = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: helper.PUBLISHED
    )
    assert service.check(draft).valid
    saved = service.publish(draft)
    assert (WARNING in saved.warnings) is warns
    assert saved.inputs.available_cash_yen == 130000
    assert store.load_with_head() == ledger_before


def test_no_reservation_has_no_warning_and_duplicate_same_ticker_orders_warn_once(tmp_path):
    case = helper.backend_case(tmp_path, holding=True)
    draft = helper.reviewed_proposal(case, action="hold")
    assert WARNING not in draft.warnings
    _order(case)
    active = helper.reviewed_proposal(case, action="hold")
    reservation = active.inputs.active_reservations[0]
    inputs = active.inputs.model_copy(
        update={
            "active_reservations": (
                reservation,
                reservation.model_copy(update={"reservation_id": "second-synthetic"}),
            )
        }
    )
    result = build_proposal_warnings(inputs, active.items, active.liquidity_context, assessments={})
    assert result.warnings.count(WARNING) == 1
    assert result.terms_warnings == {}
    assert active.items == draft.items


def test_expiry_alone_does_not_clear_recorded_reservation_warning(tmp_path):
    case = helper.backend_case(tmp_path, holding=True)
    _order(case, expires=helper.PUBLISHED - timedelta(minutes=1))
    draft = helper.reviewed_proposal(case, action="hold")
    assert draft.inputs.active_reservations
    assert WARNING in draft.warnings
    service = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: helper.PUBLISHED
    )
    assert service.check(draft).valid


def test_reported_cancellation_changes_current_warning_not_saved_hold(tmp_path):
    case = helper.backend_case(tmp_path, holding=True)
    store = _order(case)
    draft = helper.reviewed_proposal(case, action="hold")
    service = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: helper.PUBLISHED
    )
    saved = service.publish(draft)
    assert WARNING in saved.warnings
    now = helper.PUBLISHED + timedelta(minutes=15)
    _order(case, status="cancelled", now=now)
    ledger_before = store.load_with_head()
    before = case.app.read_bytes()
    preview = preview_current_terms(
        proposal_id=saved.proposal_id, db_path=case.app, sqlite_path=case.market, now=now
    )
    assert preview.status == "changed"
    assert preview.current_inputs.active_reservations == ()
    assert WARNING not in preview.warnings
    assert preview.items[0]["saved_action"] == "hold"
    assert preview.items[0]["blockers"] == []
    assert service.publish(saved) == saved
    assert read_proposal_views(case.app).latest.proposal.warnings == [WARNING]
    assert store.load_with_head() == ledger_before
    assert case.app.read_bytes() == before


def test_later_order_is_current_warning_not_retroactive_saved_warning(tmp_path):
    case = helper.backend_case(tmp_path, holding=True)
    service = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: helper.PUBLISHED
    )
    saved = service.publish(helper.reviewed_proposal(case, action="hold"))
    assert saved.warnings == ()
    now = helper.PUBLISHED + timedelta(minutes=15)
    store = _order(case, now=now)
    ledger_before = store.load_with_head()
    before = case.app.read_bytes()
    preview = preview_current_terms(
        proposal_id=saved.proposal_id, db_path=case.app, sqlite_path=case.market, now=now
    )
    assert preview.status == "changed"
    assert WARNING in preview.warnings
    assert preview.items[0]["blockers"] == []
    assert preview.items[0]["current_quote_calculation"]["action"] == "hold"
    assert service.publish(saved) == saved
    assert read_proposal_views(case.app).latest.proposal.warnings == []
    assert store.load_with_head() == ledger_before
    assert case.app.read_bytes() == before


def test_legacy_warningless_hold_read_retry_are_immutable_but_new_draft_is_rechecked(
    tmp_path, monkeypatch
):
    case = helper.backend_case(tmp_path, holding=True, active_sell=True)
    service = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: helper.PUBLISHED
    )

    def legacy_warnings(*args, **kwargs):
        result = build_proposal_warnings(*args, **kwargs)
        return replace(
            result, warnings=tuple(value for value in result.warnings if value != WARNING)
        )

    # Simulate the old publisher, not an actual legacy App or a mutation of stored bytes.
    with monkeypatch.context() as old:
        old.setattr(helper, "build_proposal_warnings", legacy_warnings)
        old.setattr(service_module, "build_proposal_warnings", legacy_warnings)
        draft = helper.reviewed_proposal(case, action="hold")
        saved = service.publish(draft)
    before = case.app.read_bytes()
    assert saved.warnings == ()
    assert service.publish(draft) == saved
    assert read_proposal_views(case.app).latest.proposal.warnings == []
    preview = preview_current_terms(
        proposal_id=saved.proposal_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=helper.PUBLISHED,
    )
    assert WARNING in preview.warnings
    assert preview.status == "unchanged"
    assert preview.items[0]["blockers"] == []
    # Existing identity retry is exact, but a new author must bind the new warning to Review.
    with pytest.raises(ProposalConflictError, match="warning/exposure projection differs"):
        service.check(draft)
    assert case.app.read_bytes() == before
