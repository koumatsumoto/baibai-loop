"""保存された未拘束cashは提案比較の出発額で、現在残高や採用予算ではない。"""

from decimal import Decimal

import pytest
from tests.helpers.proposal import PREPARED, PUBLISHED, backend_case, reviewed_proposal

from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.portfolio.ledger.events import PortfolioLedgerDocument
from baibai_engine.portfolio.ledger.store import LedgerStoreService
from baibai_web.readmodel.builders.proposal import project_saved_proposal
from baibai_web.readmodel.proposal_models import SavedProposalView
from baibai_web.sources.proposal import DbProposalSource


def _publish(case, *, action="no_action"):
    draft = reviewed_proposal(case, action=action)
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    assert service.check(draft).valid
    return service, draft, service.publish(draft)


def _assert_cash(view, saved):
    assert view.model_dump(mode="json").get("saved_cash") == {
        "available_cash_yen": str(saved.inputs.available_cash_yen),
        "inputs_checked_at": saved.inputs.checked_at.isoformat(),
        "ledger_as_of": saved.inputs.ledger_as_of.isoformat(),
    }


@pytest.mark.parametrize("action", ["buy", "no_action", "defer", "hold"])
def test_normal_saved_cash_is_independent_of_action_and_never_refreshed(
    tmp_path, monkeypatch, action
):
    case = backend_case(tmp_path, holding=action == "hold", valuation_unresolved=action == "defer")
    service, draft, saved = _publish(case, action=action)
    before = case.app.read_bytes()
    view = DbProposalSource(case.app).latest()
    _assert_cash(view, saved)
    assert view.items[0].action == action
    assert view.comparison == saved.comparison
    assert view.forgone == saved.forgone
    assert view.warnings == list(saved.warnings)
    if action == "hold":
        assert saved.inputs.available_cash_yen != saved.inputs.total_capital_yen
    if action == "buy":
        assert view.items[0].saved_buy_comparison.cash_only_terminal_value_yen == str(
            saved.inputs.available_cash_yen
        )
        assert view.items[0].terms.notional_yen == str(saved.items[0].terms.notional_yen)
    assert service.publish(draft) == saved
    assert case.app.read_bytes() == before

    def forbidden(*_args, **_kwargs):
        pytest.fail("saved cash display must not collect current inputs")

    monkeypatch.setattr(
        "baibai_engine.investment_judgment.trade_proposal.inputs.collect_proposal_inputs", forbidden
    )
    case.market.rename(tmp_path / "unused-market.sqlite")
    assert DbProposalSource(case.app).latest() == view
    assert case.app.read_bytes() == before


def test_unreviewed_defer_retains_cash_without_inventing_value_or_buy(tmp_path):
    case = backend_case(tmp_path, valuation_unresolved=True)
    case.operation = case.operations.record_unfinished(
        case.operation.operation_id,
        ticker="1234",
        reason="合成: 普通株主への回収幅をまだ採択できない。",
        references=("fixture:cash-recovery-question",),
        now=PREPARED,
    )
    _service, _draft, saved = _publish(case, action="defer")
    view = DbProposalSource(case.app).latest()
    _assert_cash(view, saved)
    assert view.items[0].assessment is None
    assert view.items[0].action == "defer"
    assert "saved_buy_comparison" not in view.items[0].model_dump()


def test_unknown_nav_does_not_erase_known_cash_or_value_holdings_at_cost(tmp_path):
    case = backend_case(tmp_path, holding=True, valuation_unresolved=True)
    case.market.rename(tmp_path / "missing-market.sqlite")
    _service, _draft, saved = _publish(case, action="defer")
    view = DbProposalSource(case.app).latest()
    assert saved.inputs.total_capital_yen is None
    assert view.portfolio_exposure.total_capital_yen is None
    assert saved.inputs.available_cash_yen == Decimal(210000)
    _assert_cash(view, saved)


@pytest.mark.parametrize("cash", [0, 300000])
def test_saved_zero_cash_and_positive_cash_are_not_missing(tmp_path, cash):
    case = backend_case(tmp_path, cash_yen=300000)
    if cash == 0:
        ledger = LedgerStoreService(case.app)
        document, head = ledger.load_with_head()
        payload = document.model_dump(mode="json")
        payload["events"].append(
            dict(
                type="withdrawal",
                event_id="synthetic-withdrawal",
                occurred_at="2026-10-14T10:00:00+09:00",
                amount_yen=300000,
            )
        )
        ledger.apply_document(
            expected_head=head,
            expected_document=document,
            replacement=PortfolioLedgerDocument.model_validate(payload),
        )
    _service, _draft, saved = _publish(case)
    _assert_cash(DbProposalSource(case.app).latest(), saved)
    assert saved.inputs.available_cash_yen == Decimal(cash)


def test_active_commitment_is_already_deducted_and_later_deposit_does_not_rewrite_cash(tmp_path):
    case = backend_case(tmp_path)
    ledger = LedgerStoreService(case.app)
    document, head = ledger.load_with_head()
    payload = document.model_dump(mode="json")
    payload["events"].append(
        dict(
            type="reservation",
            event_id="synthetic-other-order",
            reservation_id="synthetic-other-reservation",
            order_id="synthetic-broker-order",
            occurred_at="2026-10-14T10:00:00+09:00",
            ticker="5678",
            side="buy",
            sector="情報・通信業",
            common_factors=["domestic-demand"],
            decision_reference="synthetic-other-reference",
            quantity=100,
            price_guard_yen="1000",
            expires_at="2026-10-15T15:30:00+09:00",
        )
    )
    ledger.apply_document(
        expected_head=head,
        expected_document=document,
        replacement=PortfolioLedgerDocument.model_validate(payload),
    )
    service, draft, saved = _publish(case)
    view = DbProposalSource(case.app).latest()
    _assert_cash(view, saved)
    assert saved.inputs.available_cash_yen == Decimal(200000)
    assert saved.inputs.active_reservations[0].reserved_yen == Decimal(100000)
    assert view.portfolio_exposure.total_capital_yen == "300000"
    document, head = ledger.load_with_head()
    payload = document.model_dump(mode="json")
    payload["as_of"] = "2026-10-14T16:30:00+09:00"
    payload["events"].append(
        dict(
            type="contribution",
            event_id="synthetic-later-deposit",
            occurred_at=payload["as_of"],
            amount_yen=50000,
        )
    )
    ledger.apply_document(
        expected_head=head,
        expected_document=document,
        replacement=PortfolioLedgerDocument.model_validate(payload),
    )
    after_deposit = case.app.read_bytes()
    assert DbProposalSource(case.app).latest() == view
    assert service.publish(draft) == saved
    assert case.app.read_bytes() == after_deposit


@pytest.mark.parametrize("mode", ["absent", "null"])
def test_old_cash_display_is_unrecorded_not_zero_or_nav(tmp_path, mode):
    case = backend_case(tmp_path)
    _publish(case)
    raw = DbProposalSource(case.app).latest().model_dump(mode="json")
    raw["saved_cash"] = None
    if mode == "absent":
        raw.pop("saved_cash")
    assert SavedProposalView.model_validate(raw).saved_cash is None


def test_historical_cash_string_is_preserved_without_rounding(tmp_path):
    case = backend_case(tmp_path)
    _service, _draft, saved = _publish(case)
    # Read-only historical projection, not a ledger write or current cash claim.
    value = saved.model_copy(
        update={
            "inputs": saved.inputs.model_copy(
                update={"available_cash_yen": Decimal("300000.0000000000000001")}
            )
        }
    )
    before = value.model_dump_json()
    view = project_saved_proposal(value, {case.pair.assessment_id: case.pair})
    _assert_cash(view, value)
    assert value.model_dump_json() == before
