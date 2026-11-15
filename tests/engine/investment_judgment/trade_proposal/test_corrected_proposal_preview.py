"""明示的な誤記録訂正だけを現在適用から外し、旧判断/非購入報告は改変しない。"""

from contextlib import closing
from datetime import timedelta

import pytest
from tests.helpers.proposal import (
    PROPOSAL_ID,
    PUBLISHED,
    backend_case,
    bind_review,
    reviewed_proposal,
)

from baibai_engine.investment_judgment.trade_proposal.contracts import proposal_draft_sha256
from baibai_engine.investment_judgment.trade_proposal.preview import preview_current_terms
from baibai_engine.investment_judgment.trade_proposal.query import (
    load_investment_proposal,
    load_latest_investment_proposal,
)
from baibai_engine.investment_judgment.trade_proposal.record_contracts import (
    HumanProposalReportDraft,
    ProposalCorrectionDraft,
    ProposalReference,
)
from baibai_engine.investment_judgment.trade_proposal.records import ProposalRecordService
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.storage.sqlite.application.read import connect_read_only

WRONG_ID = "investment-proposal-00000000-0000-4000-8000-000000000004"
NOW = PUBLISHED + timedelta(minutes=2)


def _ref(proposal):
    return ProposalReference(
        proposal_id=proposal.proposal_id, draft_sha256=proposal_draft_sha256(proposal)
    )


def _preview(case, proposal, *, now=NOW):
    return preview_current_terms(
        proposal_id=proposal.proposal_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=now,
    )


def _draft(case, action, proposal_id=PROPOSAL_ID):
    draft = reviewed_proposal(case, action=action, proposal_id=proposal_id)
    if action != "sell":
        return draft
    raw = draft.payload()
    raw["items"][0].update(
        terms={
            "side": "sell",
            "limit_price_yen": "1100",
            "quantity": 100,
            "notional_yen": "110000",
            "target_session": "2026-10-15",
            "valuation_as_of": "2026-10-14",
            "maximum_entry_price_yen": None,
            "price_grid_rule_id": "tse-common-conservative-20261014",
            "conditions": ["実注文前に保有数量と既存注文の処理を確認する。"],
            "warnings": [],
        },
        terms_unavailable_reasons=[],
    )
    return bind_review(raw, {case.pair.assessment_id: case.pair.document})


@pytest.mark.parametrize("action", ["buy", "no_action", "defer", "hold", "sell"])
def test_corrected_exact_proposal_is_history_not_currently_applicable(tmp_path, action):
    case = backend_case(tmp_path, holding=action in {"hold", "sell"}, broken=action == "sell")
    original = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(_draft(case, action))
    wrong = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED + timedelta(minutes=1)
    ).publish(_draft(case, action, proposal_id=WRONG_ID))
    baseline = _preview(case, wrong)
    original_baseline = _preview(case, original)
    assert "proposal_corrected" not in {reason.code for reason in baseline.change_reasons}
    writer = ProposalRecordService(case.app, clock=lambda: NOW)
    draft = ProposalCorrectionDraft(
        correction_id="synthetic-wrong-proposal",
        erroneous_ref=_ref(wrong),
        replacement_ref=_ref(original),
        reason="誤って発行した保存提案を元の記録へ訂正する合成例。採否の新判断ではない。",
    )
    correction = writer.correct(draft, expected_latest_ref=_ref(wrong))
    assert writer.correct(draft, expected_latest_ref=_ref(wrong)) == correction
    before = case.app.read_bytes()
    preview = _preview(case, wrong)
    row = preview.items[0]
    assert preview.status == "blocked"
    assert "proposal_corrected" in {reason.code for reason in preview.change_reasons}
    assert "proposal_corrected" in {reason["code"] for reason in row["blockers"]}
    assert row["saved_action"] == action
    assert row["saved_order_readiness"] == baseline.items[0]["saved_order_readiness"]
    assert row["saved_terms"] == baseline.items[0]["saved_terms"]
    assert row["current_quote_calculation"] is None
    if row["saved_limit_revalidation"] is not None:
        assert "proposal_corrected" in {
            reason["code"] for reason in row["saved_limit_revalidation"]["hard_blockers"]
        }
    assert preview.records["corrections"] == [correction.model_dump(mode="json")]
    replacement = _preview(case, original)
    assert replacement.status == original_baseline.status
    assert replacement.items == original_baseline.items
    assert replacement.change_reasons == original_baseline.change_reasons
    assert replacement.records["corrections"] == preview.records["corrections"]
    with closing(connect_read_only(case.app)) as con:
        assert load_investment_proposal(con, wrong.proposal_id) == wrong
        assert load_investment_proposal(con, original.proposal_id) == original
        assert load_latest_investment_proposal(con) == original
    assert case.app.read_bytes() == before


def test_human_non_purchase_report_and_newer_proposal_do_not_invalidate_old_buy(tmp_path):
    case = backend_case(tmp_path)
    original = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(reviewed_proposal(case, action="buy"))
    baseline = _preview(case, original)
    assert baseline.status == "unchanged"
    TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED + timedelta(minutes=1)
    ).publish(reviewed_proposal(case, action="no_action", proposal_id=WRONG_ID))
    report = ProposalRecordService(case.app, clock=lambda: NOW).report(
        HumanProposalReportDraft(
            report_id="synthetic-not-purchased",
            proposal_ref=_ref(original),
            outcome="not_purchased",
            statement="今回は買わなかった。",
            reason_quote="予定支出のため現金を残した。",
            reported_at=NOW,
        )
    )
    before = case.app.read_bytes()
    preview = _preview(case, original)
    assert preview.status == baseline.status
    assert preview.items == baseline.items
    assert preview.change_reasons == baseline.change_reasons
    assert preview.records["corrections"] == []
    assert preview.records["human_reports"] == [report.model_dump(mode="json")]
    assert case.app.read_bytes() == before
