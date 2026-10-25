from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, timedelta

import pytest
import yaml
from tests.helpers.proposal import PUBLISHED, backend_case, bind_review, reviewed_proposal

from baibai_engine.investment_judgment.selection import selection_sha256
from baibai_engine.investment_judgment.trade_proposal import ProposalConflictError, ProposalError
from baibai_engine.investment_judgment.trade_proposal_query import get_investment_proposal
from baibai_engine.investment_judgment.trade_proposal_service import TradeProposalService
from baibai_engine.investment_judgment.workspace_cli import main


@pytest.fixture
def case(tmp_path):
    return backend_case(tmp_path)


def _counts(path):
    with closing(sqlite3.connect(path)) as connection, connection:
        return {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "investment_proposal",
                "investment_proposal_item",
                "position_decision_reference",
                "ledger_event",
                "ledger_meta",
            )
        }


def _ledger_snapshot(path):
    with closing(sqlite3.connect(path)) as connection, connection:
        return {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()
            for table in ("ledger_event", "ledger_meta")
        }


def test_real_selection_pair_scope_publish_atomic_and_ledger_unchanged(case):
    proposal = reviewed_proposal(case)
    assert proposal.research_scope.selection_sha256 == selection_sha256(case.selected)
    assert proposal.research_scope.operation_id == case.operation.operation_id
    assert proposal.inputs.checked_at < PUBLISHED
    before = _counts(case.app)
    ledger_before = _ledger_snapshot(case.app)
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    assert service.check(proposal).valid
    published = service.publish(proposal)
    after = _counts(case.app)
    assert after["ledger_event"] == before["ledger_event"]
    assert after["ledger_meta"] == before["ledger_meta"]
    assert _ledger_snapshot(case.app) == ledger_before
    assert (
        after["investment_proposal"]
        == after["investment_proposal_item"]
        == after["position_decision_reference"]
        == 1
    )
    with closing(sqlite3.connect(case.app)) as connection, connection:
        binding = connection.execute(
            "SELECT ticker,origin,source_digest FROM position_decision_reference"
        ).fetchone()
    assert binding == ("1234", "proposal", published.review.draft_sha256)
    assert get_investment_proposal(proposal_id=published.proposal_id, db_path=case.app) == published
    recorded = case.operations.record_proposal(
        case.operation.operation_id, published.proposal_id, now=PUBLISHED
    )
    completed = case.operations.complete(
        recorded.operation_id,
        recorded.payload.model_copy(
            update={
                "result": "合成提案を発行した。",
                "next": "本人が採否と証券画面の条件を確認する。",
            }
        ),
        completed_at=PUBLISHED,
    )
    assert completed.status == "completed"


def test_cli_yaml_output_failure_preserves_successful_publication(
    case, tmp_path, monkeypatch, capsys
):
    proposal = reviewed_proposal(case)
    draft = tmp_path / "proposal.yaml"
    draft.write_text(yaml.safe_dump(proposal.payload()))

    def fail_output(*_args, **_kwargs):
        raise yaml.YAMLError("synthetic publication output failure")

    monkeypatch.setattr(
        "baibai_engine.investment_judgment.workspace_cli.yaml.safe_dump", fail_output
    )
    with pytest.raises(yaml.YAMLError, match="synthetic publication output failure"):
        main(
            [
                "trade-proposal-publish",
                str(draft),
                "--db",
                str(case.app),
                "--sqlite-path",
                str(case.market),
            ],
            now=PUBLISHED,
        )
    assert capsys.readouterr().out == ""
    stored = get_investment_proposal(proposal_id=proposal.proposal_id, db_path=case.app)
    assert stored == proposal.model_copy(update={"published_at": PUBLISHED})


def test_same_id_retry_reads_immutable_before_current_clock_latest_or_quote_gates(case):
    proposal = reviewed_proposal(case)
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    published = service.publish(proposal)
    with closing(sqlite3.connect(case.market)) as connection, connection:
        connection.execute("UPDATE jquants_daily_bars SET close=900")
    later = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED + timedelta(days=20)
    )
    assert later.publish(proposal) == published
    raw = proposal.payload()
    raw["headline"] = "同identityの別内容"
    changed = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    with pytest.raises(ProposalConflictError, match="immutable"):
        later.publish(changed)


def test_same_id_retry_requires_the_exact_signed_typed_payload(case):
    proposal = reviewed_proposal(case)
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    service.publish(proposal)
    raw = proposal.payload()
    raw["inputs"]["quotes"][0]["price_yen"] = "1100.000"
    from baibai_engine.investment_judgment.trade_proposal import TradeProposal

    with pytest.raises(ProposalConflictError, match="immutable"):
        service.publish(TradeProposal.model_validate(raw))


@pytest.mark.parametrize("change", ["head", "meta", "quote", "source"])
def test_actual_input_fact_change_requires_new_snapshot_and_review(case, change):
    proposal = reviewed_proposal(case)
    if change == "head":
        from baibai_engine.position.drafts import apply_draft, build_event_draft
        from baibai_engine.position.ledger import ContributionEvent
        from baibai_engine.position.store import LedgerStoreService

        event = ContributionEvent(
            type="contribution",
            event_id="new-confirmed-fact",
            occurred_at=PUBLISHED - timedelta(minutes=1),
            amount_yen=1,
        )
        store = LedgerStoreService(case.app)
        apply_draft(store, build_event_draft(store, event), human_confirmed=True)
    elif change == "meta":
        from baibai_engine.position.store import LedgerStoreService

        store = LedgerStoreService(case.app)
        ledger, head = store.load_with_head()
        store.apply_document(
            expected_head=head,
            expected_document=ledger,
            replacement=ledger.model_copy(update={"as_of": ledger.as_of + timedelta(minutes=1)}),
        )
    elif change == "quote":
        with closing(sqlite3.connect(case.market)) as connection, connection:
            connection.execute("UPDATE jquants_daily_bars SET close=1099")
    else:
        raw = proposal.payload()
        raw["inputs"]["quotes"][0]["source_ref"] = "other-real-source"
        proposal = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    with pytest.raises(ProposalConflictError, match="input facts changed"):
        service.publish(proposal)
    assert _counts(case.app)["investment_proposal"] == 0


def test_same_instant_utc_and_decimal_equivalent_facts_publish(case):
    proposal = reviewed_proposal(case)
    raw = proposal.payload()
    raw["inputs"]["ledger_as_of"] = "2026-10-14T06:00:00Z"
    raw["inputs"]["quotes"][0]["price_yen"] = "1100.00"
    raw["inputs"]["available_cash_yen"] = "3E+5"
    proposal = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    assert (
        TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
            proposal
        )
        == proposal
    )


@pytest.mark.parametrize(
    ("checked_at", "blocker_code", "message"),
    [
        ("2026-10-14T16:19:00+09:00", "proposal_inputs_after_review", "独立Review以前"),
        (
            "2026-10-14T15:20:00+09:00",
            "quote_session_after_inputs",
            "入力収集時点で利用できる取引日以前",
        ),
    ],
)
def test_saved_input_and_quote_times_must_precede_their_reviewed_cutoffs(
    case, checked_at, blocker_code, message
):
    raw = reviewed_proposal(case).payload()
    raw["inputs"]["checked_at"] = checked_at
    proposal = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    validation = service.check(proposal)
    assert not validation.valid
    assert blocker_code in {reason.code for reason in validation.blockers}
    before = case.app.read_bytes()
    with pytest.raises(ProposalError, match=message):
        service.publish(proposal)
    assert case.app.read_bytes() == before


@pytest.mark.parametrize("checked_at", ["2026-10-14T07:15:00Z", "2026-10-14T06:30:00Z"])
def test_saved_input_review_and_quote_boundaries_accept_the_same_utc_instant(case, checked_at):
    raw = reviewed_proposal(case).payload()
    raw["inputs"]["checked_at"] = checked_at
    proposal = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    service = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED.astimezone(UTC)
    )
    assert proposal.inputs.checked_at <= proposal.review.reviewed_at < PUBLISHED
    assert service.check(proposal).valid
    assert service.publish(proposal).review == proposal.review


def test_review_binds_conditions_cash_scope_and_projection(case):
    proposal = reviewed_proposal(case)
    raw = proposal.payload()
    raw["items"][0]["terms"]["conditions"].append("別の実前提")
    from baibai_engine.investment_judgment.trade_proposal import TradeProposal

    changed = TradeProposal.model_validate(raw)
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    assert service.check(changed, require_review=False).valid
    with pytest.raises(ProposalConflictError, match="content review"):
        service.publish(changed)


@pytest.mark.parametrize("change", ["warning", "exposure", "terms_warning"])
def test_warning_or_exposure_tampering_not_accepted_by_new_review(case, change):
    proposal = reviewed_proposal(case)
    raw = proposal.payload()
    if change == "warning":
        raw["warnings"] = ["made-up-warning"]
    elif change == "exposure":
        raw["portfolio_exposure"]["ticker"]["prospective_pct"] = "99"
    else:
        assert raw["items"][0]["terms"]["warnings"]
        raw["items"][0]["terms"]["warnings"] = []
    from baibai_engine.investment_judgment.trade_proposal import (
        TradeProposal,
        proposal_draft_sha256,
    )

    changed = TradeProposal.model_validate(raw)
    raw["review"]["draft_sha256"] = proposal_draft_sha256(changed)
    changed = TradeProposal.model_validate(raw)
    assert changed.review.draft_sha256 == proposal_draft_sha256(changed)
    before = _counts(case.app)
    message = "terms warnings" if change == "terms_warning" else "warning/exposure"
    with pytest.raises(ProposalConflictError, match=message):
        TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
            changed
        )
    assert _counts(case.app) == before


def test_injected_failure_after_binding_rolls_back_proposal_items_and_binding(case, monkeypatch):
    proposal = reviewed_proposal(case)
    import baibai_engine.investment_judgment.trade_proposal_service as owner

    def fail(*_):
        raise RuntimeError("injected readback failure")

    monkeypatch.setattr(owner, "load_investment_proposal", fail)
    with pytest.raises(RuntimeError, match="injected"):
        owner.TradeProposalService(
            case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
        ).publish(proposal)
    counts = _counts(case.app)
    assert (
        counts["investment_proposal"]
        == counts["investment_proposal_item"]
        == counts["position_decision_reference"]
        == 0
    )
    assert counts["ledger_event"] == 1


def test_nonbuy_has_no_binding_and_date_clock_guard_even_with_same_facts(case):
    proposal = reviewed_proposal(case, action="no_action")
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    assert service.publish(proposal).items[0].action == "no_action"
    assert _counts(case.app)["position_decision_reference"] == 0
    raw = proposal.payload()
    raw["proposal_id"] = "investment-proposal-00000000-0000-4000-8000-000000000099"
    proposal = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    tomorrow = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED + timedelta(days=1)
    )
    with pytest.raises(ProposalError, match="基準日は本日"):
        tomorrow.publish(proposal)


def test_live_latest_gate_applies_to_buy_but_not_no_action(case):
    buy = reviewed_proposal(case)
    no_action = reviewed_proposal(case, action="no_action")
    from tests.helpers.assessment_v1 import assessment_id, pair_payload

    from baibai_engine.investment_judgment.stock_assessment_store import AssessmentStoreService

    document, review = pair_payload(review_number=22)
    AssessmentStoreService(
        case.app, clock=lambda: PUBLISHED + timedelta(minutes=1)
    ).publish_reviewed_assessment(
        assessment_id(21), document, review, supersedes_id=case.pair.assessment_id
    )
    service = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED + timedelta(minutes=5)
    )
    validation = service.check(buy)
    assert not validation.valid
    assert any(reason.code == "latest_assessment_required" for reason in validation.blockers)
    assert service.check(no_action).valid
    assert service.publish(no_action).items[0].action == "no_action"


def test_late_outcome_does_not_rewrite_saved_scope_but_rejects_stale_new_publication(tmp_path):
    case = backend_case(tmp_path, tickers=("1234", "5678"))
    from tests.helpers.assessment_v1 import assessment_id, pair_payload

    from baibai_engine.investment_judgment.operation.progress import UnfinishedTarget
    from baibai_engine.investment_judgment.stock_assessment_store import AssessmentStoreService

    case.operation = case.operations.record_outcome(
        case.operation.operation_id,
        UnfinishedTarget(
            ticker="5678",
            purpose="new",
            unfinished_reason="確認する資料が残る。",
            references=("https://example.invalid/pending",),
        ),
        now=PUBLISHED - timedelta(minutes=12),
    )
    proposal = reviewed_proposal(case, action="no_action")
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    service.publish(proposal)
    document, review = pair_payload(ticker="5678", review_number=32)
    pair = AssessmentStoreService(
        case.app, clock=lambda: PUBLISHED + timedelta(minutes=1)
    ).publish_reviewed_assessment(assessment_id(31), document, review)
    case.operations.record_assessment(
        case.operation.operation_id, pair.assessment_id, now=PUBLISHED + timedelta(minutes=2)
    )
    stored = get_investment_proposal(proposal_id=proposal.proposal_id, db_path=case.app)
    assert stored == proposal
    assert stored.research_scope.outcomes[1].status == "unfinished"
    raw = proposal.payload()
    raw["proposal_id"] = "investment-proposal-00000000-0000-4000-8000-000000000099"
    new = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    with pytest.raises(ProposalConflictError, match="fixed coverage"):
        TradeProposalService(
            case.app, sqlite_path=case.market, clock=lambda: PUBLISHED + timedelta(minutes=3)
        ).publish(new)


def test_same_session_adjustment_does_not_change_native_quote_or_comparison(case):
    proposal = reviewed_proposal(case)
    with closing(sqlite3.connect(case.market)) as connection, connection:
        connection.execute("UPDATE jquants_daily_bars SET adjustment_factor=0.5, ex_right_type='1'")
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    assert service.check(proposal).valid
    assert service.publish(proposal).proposal_id == proposal.proposal_id
