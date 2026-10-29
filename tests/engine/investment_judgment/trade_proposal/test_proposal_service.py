from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, timedelta
from decimal import Decimal

import pytest
import yaml
from tests.helpers.proposal import (
    PREPARED,
    PROPOSAL_ID,
    PUBLISHED,
    backend_case,
    bind_review,
    reviewed_proposal,
)

from baibai_engine.cli.investment_judgment import main
from baibai_engine.investment_judgment.trade_proposal.contracts import (
    ProposalConflictError,
    ProposalError,
    TradeProposal,
    load_proposal_draft,
    proposal_draft_sha256,
)
from baibai_engine.investment_judgment.trade_proposal.query import (
    get_investment_proposal,
    preview_current_terms,
)
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.investment_judgment.workflow.operation.progress import progress_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationCompletionError
from baibai_engine.investment_judgment.workflow.selection import selection_sha256


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
        result="合成提案を発行した。",
        next="本人が採否と証券画面の条件を確認する。",
        completed_at=PUBLISHED,
    )
    assert completed.status == "completed"


@pytest.mark.parametrize("publication", ["before_read", "after_read", "changed_reason"])
def test_published_unbound_unfinished_proposal_cannot_be_skipped(case, monkeypatch, publication):
    operation = case.operations.record_unfinished(
        case.operation.operation_id,
        ticker="1234",
        reason="追加資料の確認が未完了",
        references=("synthetic-pending-source",),
        now=PREPARED,
    )
    proposal = reviewed_proposal(case, action="defer")
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    get = case.operations.get
    if publication == "after_read":

        def read_then_publish(operation_id):
            before = get(operation_id)
            service.publish(proposal)
            return before

        monkeypatch.setattr(case.operations, "get", read_then_publish)
    else:
        service.publish(proposal)
    if publication == "changed_reason":
        operation = case.operations.record_unfinished(
            operation.operation_id,
            ticker="1234",
            reason="公開後に追加の反証を確認し、別の問いが残った",
            references=("synthetic-new-counterevidence",),
            now=PUBLISHED,
        )
    assert progress_binding(operation.payload).proposal_ref is None
    with pytest.raises(OperationCompletionError, match="published Trade Proposal"):
        case.operations.complete(
            operation.operation_id,
            result="評価未完了のまま作業を終了",
            next="不足する資料を確認する",
            completed_at=PUBLISHED,
        )
    assert get(operation.operation_id) == operation
    assert get_investment_proposal(
        proposal_id=proposal.proposal_id, db_path=case.app
    ).payload() == {
        **proposal.payload(),
        "published_at": PUBLISHED.isoformat(),
    }
    if publication == "changed_reason":
        with pytest.raises(OperationCompletionError, match="fixed coverage"):
            case.operations.record_proposal(
                operation.operation_id, proposal.proposal_id, now=PUBLISHED
            )
        assert get(operation.operation_id) == operation
    else:
        monkeypatch.setattr(case.operations, "get", get)
        case.operations.record_proposal(operation.operation_id, proposal.proposal_id, now=PUBLISHED)
        completed = case.operations.complete(
            operation.operation_id,
            result="未完了を明示した提案を保存し作業を終了",
            next="不足する資料を確認する",
            completed_at=PUBLISHED,
        )
        assert completed.status == "completed"
        assert progress_binding(completed.payload).proposal_ref.proposal_id == proposal.proposal_id


def test_reviewed_defer_still_requires_a_bound_published_proposal(tmp_path):
    case = backend_case(tmp_path, valuation_unresolved=True)
    proposal = reviewed_proposal(case, action="defer")
    assert proposal.research_scope.outcomes[0].status == "reviewed"
    assert proposal.items[0].action == "defer"
    notes = {"result": "不確実な評価を保存", "next": "新しい開示を確認", "completed_at": PUBLISHED}
    with pytest.raises(OperationCompletionError, match="published Trade Proposal"):
        case.operations.complete(case.operation.operation_id, **notes)
    published = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(proposal)
    case.operations.record_proposal(
        case.operation.operation_id, published.proposal_id, now=PUBLISHED
    )
    assert case.operations.complete(case.operation.operation_id, **notes).status == "completed"


def test_unfinished_completion_prevents_later_new_proposal_publication(case):
    case.operations.record_unfinished(
        case.operation.operation_id,
        ticker="1234",
        reason="資料待ちで評価未完了",
        references=("synthetic-pending-source",),
        now=PREPARED,
    )
    proposal = reviewed_proposal(case, action="defer")
    completed = case.operations.complete(
        case.operation.operation_id,
        result="今回の作業を終了",
        next="不足する資料を確認する",
        completed_at=PUBLISHED,
    )
    before = _counts(case.app)
    with pytest.raises(ProposalConflictError, match="active Research Operation"):
        TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
            proposal
        )
    assert _counts(case.app) == before
    assert case.operations.get(completed.operation_id) == completed


def test_cli_yaml_output_failure_preserves_successful_publication(
    case, tmp_path, monkeypatch, capsys
):
    proposal = reviewed_proposal(case)
    draft = tmp_path / "proposal.yaml"
    draft.write_text(yaml.safe_dump(proposal.payload()))

    def fail_output(*_args, **_kwargs):
        raise yaml.YAMLError("synthetic publication output failure")

    monkeypatch.setattr("baibai_engine.cli.investment_judgment.yaml.safe_dump", fail_output)
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
    assert stored.model_dump(mode="json", exclude={"published_at"}) == proposal.payload()
    assert stored.published_at == PUBLISHED


@pytest.mark.parametrize("legacy_publication_time", [False, True])
def test_cli_scaffold_later_review_check_publish_and_retry_own_the_publication_clock(
    case, tmp_path, capsys, legacy_publication_time
):
    draft = tmp_path / "proposal.yaml"
    common = ["--db", str(case.app), "--sqlite-path", str(case.market)]
    assert (
        main(
            [
                "trade-proposal-scaffold",
                "--operation-id",
                case.operation.operation_id,
                "--proposal-id",
                PROPOSAL_ID,
                "--out",
                str(draft),
                *common,
            ],
            now=PREPARED,
        )
        == 0
    )
    capsys.readouterr()
    raw = yaml.safe_load(draft.read_text())
    assert "published_at" not in raw
    raw.update(headline="合成見送り", comparison="現金維持との比較", forgone="買付を見送る")
    raw["items"][0]["action"] = "no_action"
    raw["items"][0].pop("blockers")
    raw["items"][0].pop("resume_conditions")
    if legacy_publication_time:
        raw["published_at"] = PREPARED.isoformat()
    draft.write_text(yaml.safe_dump(raw))
    assert main(["trade-proposal-check", str(draft), *common], now=PREPARED) == 0
    checked = yaml.safe_load(capsys.readouterr().out)
    assert checked["review_binding"] == "stale"

    reviewed_at = PREPARED + timedelta(minutes=5)
    raw["review"].update(
        reviewer_identity="synthetic-independent-reviewer",
        reviewed_at=reviewed_at.isoformat(),
        draft_sha256=checked["draft_sha256"],
    )
    draft.write_text(yaml.safe_dump(raw))
    draft_bytes = draft.read_bytes()
    assert main(["trade-proposal-check", str(draft), *common], now=PUBLISHED) == 0
    checked_again = yaml.safe_load(capsys.readouterr().out)
    assert checked_again["valid"] is True
    assert checked_again["review_binding"] == "match"
    assert checked_again["draft_sha256"] == checked["draft_sha256"]

    published_at = PUBLISHED + timedelta(minutes=1)
    assert main(["trade-proposal-publish", str(draft), *common], now=published_at) == 0
    published = TradeProposal.model_validate(yaml.safe_load(capsys.readouterr().out))
    assert published.prepared_at == PREPARED
    assert published.review.reviewed_at == reviewed_at
    assert published.published_at == published_at
    assert proposal_draft_sha256(published) == checked["draft_sha256"]
    assert draft.read_bytes() == draft_bytes

    stored_bytes = case.app.read_bytes()
    # Exact retries are historical reads, even when the caller's clock predates Review.
    assert main(["trade-proposal-publish", str(draft), *common], now=PREPARED) == 0
    assert TradeProposal.model_validate(yaml.safe_load(capsys.readouterr().out)) == published
    assert case.app.read_bytes() == stored_bytes
    assert draft.read_bytes() == draft_bytes


def test_future_review_is_rejected_even_with_a_later_caller_publication_time(case):
    raw = reviewed_proposal(case).payload()
    raw["review"]["reviewed_at"] = (PUBLISHED + timedelta(minutes=1)).isoformat()
    raw["published_at"] = (PUBLISHED + timedelta(minutes=2)).isoformat()
    proposal = load_proposal_draft(raw)
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    before = case.app.read_bytes()
    validation = service.check(proposal)
    assert not validation.valid
    assert "proposal_time_after_publish" in {reason.code for reason in validation.blockers}
    with pytest.raises(ProposalError, match="現在発行時刻"):
        service.publish(proposal)
    assert case.app.read_bytes() == before


def test_place_limit_publishes_and_previews_separate_quote_and_ceiling(case):
    raw = reviewed_proposal(case).payload()
    with closing(sqlite3.connect(case.market)) as connection, connection:
        connection.execute("UPDATE jquants_daily_bars SET close=875")
    raw["inputs"]["quotes"][0]["price_yen"] = "875"
    raw["forgone"] = "観測875円を保持し、支払上限880円で指値を提案する。"
    item = raw["items"][0]
    item["timing"] = "place_limit"
    item["terms"].update(limit_price_yen="880", notional_yen="264000")
    proposal = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    ledger_before = _ledger_snapshot(case.app)
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    assert service.check(proposal).valid
    published = service.publish(proposal)
    assert get_investment_proposal(proposal_id=published.proposal_id, db_path=case.app) == published
    assert published.inputs.quotes[0].price_yen == Decimal(875)
    assert published.items[0].terms.notional_yen == Decimal(264000)
    preview = preview_current_terms(
        proposal_id=published.proposal_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PUBLISHED + timedelta(minutes=5),
    )
    assert preview.status == "unchanged"
    row = preview.items[0]
    assert Decimal(row["current_quote_calculation"]["calculation_price_yen"]) == Decimal(875)
    saved_limit = row["saved_limit_revalidation"]
    assert saved_limit["hard_blockers"] == []
    assert saved_limit["quantity"] == 300
    assert saved_limit["current_price_projection"]["price_yen"] == 875
    assert saved_limit["limit_price_projection"]["price_yen"] == 880
    assert saved_limit["limit_price_projection"]["base"]["total_return_pct"] == 25
    assert _ledger_snapshot(case.app) == ledger_before
    changed = published.payload()
    changed["items"][0]["terms"].update(limit_price_yen="885", notional_yen="265500")
    with pytest.raises(ProposalConflictError, match="content review"):
        service.check(load_proposal_draft(changed))
    assert get_investment_proposal(proposal_id=published.proposal_id, db_path=case.app) == published


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
    with pytest.raises(ProposalConflictError, match="immutable"):
        service.publish(load_proposal_draft(raw))


@pytest.mark.parametrize("change", ["head", "meta", "quote", "source"])
def test_actual_input_fact_change_requires_new_snapshot_and_review(case, change):
    proposal = reviewed_proposal(case)
    if change == "head":
        from baibai_engine.portfolio.ledger.drafts import apply_draft, build_event_draft
        from baibai_engine.portfolio.ledger.events import ContributionEvent
        from baibai_engine.portfolio.ledger.store import LedgerStoreService

        event = ContributionEvent(
            type="contribution",
            event_id="new-confirmed-fact",
            occurred_at=PUBLISHED - timedelta(minutes=1),
            amount_yen=1,
        )
        store = LedgerStoreService(case.app)
        apply_draft(store, build_event_draft(store, event), human_confirmed=True)
    elif change == "meta":
        from baibai_engine.portfolio.ledger.store import LedgerStoreService

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
    published = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(proposal)
    assert published.model_dump(mode="json", exclude={"published_at"}) == proposal.payload()


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
    changed = load_proposal_draft(raw)
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
    changed = load_proposal_draft(raw)
    raw["review"]["draft_sha256"] = proposal_draft_sha256(changed)
    changed = load_proposal_draft(raw)
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
    import baibai_engine.investment_judgment.trade_proposal.service as owner

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

    from baibai_engine.investment_judgment.stock_assessment.store import AssessmentStoreService

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

    from baibai_engine.investment_judgment.stock_assessment.store import AssessmentStoreService
    from baibai_engine.investment_judgment.workflow.operation.progress import UnfinishedTarget

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
    published = service.publish(proposal)
    document, review = pair_payload(ticker="5678", review_number=32)
    pair = AssessmentStoreService(
        case.app, clock=lambda: PUBLISHED + timedelta(minutes=1)
    ).publish_reviewed_assessment(assessment_id(31), document, review)
    case.operations.record_assessment(
        case.operation.operation_id, pair.assessment_id, now=PUBLISHED + timedelta(minutes=2)
    )
    stored = get_investment_proposal(proposal_id=proposal.proposal_id, db_path=case.app)
    assert stored == published
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
