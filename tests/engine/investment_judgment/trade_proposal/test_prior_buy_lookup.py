import sqlite3
from contextlib import closing
from datetime import UTC, timedelta

import pytest
from tests.helpers.proposal import PUBLISHED, backend_case, bind_review, reviewed_proposal

from baibai_engine.investment_judgment.trade_proposal.query import previous_buy_proposals
from baibai_engine.investment_judgment.trade_proposal.record_contracts import (
    HumanProposalReportDraft,
    ProposalCorrectionDraft,
    ProposalReference,
)
from baibai_engine.investment_judgment.trade_proposal.records import ProposalRecordService
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.storage.sqlite.application.read import connect_read_only


def publish(case, number, instant, *, action="buy"):
    draft = reviewed_proposal(
        case,
        action=action,
        proposal_id=f"investment-proposal-00000000-0000-4000-8000-{number:012d}",
    )
    return TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: instant).publish(
        draft
    )


def lookup(case, when, *, tickers=("1234",), exclude=None):
    before = case.app.read_bytes()
    with closing(connect_read_only(case.app)) as connection:
        connection.execute("BEGIN")
        result = previous_buy_proposals(
            connection, tickers=tickers, known_at=when, exclude_proposal_id=exclude
        )
    assert before == case.app.read_bytes()
    return result


def ref(proposal):
    return ProposalReference(
        proposal_id=proposal.proposal_id, draft_sha256=proposal.review.draft_sha256
    )


def test_selects_latest_exact_buy_by_publication_instant_not_id_offset_or_other_action(tmp_path):
    c = backend_case(tmp_path)
    micro = timedelta(microseconds=1)
    a = publish(c, 9, PUBLISHED)
    b = publish(c, 1, (PUBLISHED + micro).astimezone(UTC))
    z = publish(c, 8, PUBLISHED + micro)
    publish(c, 7, PUBLISHED + micro * 2, action="no_action")
    assert lookup(c, PUBLISHED - micro)["1234"].status == "not_found"
    assert lookup(c, PUBLISHED)["1234"].proposal == a
    row = lookup(c, PUBLISHED + micro * 2, tickers=("1234", "123A", "1234"))
    assert set(row) == {"1234", "123A"}
    assert row["123A"].status == "not_found"
    assert row["1234"].proposal == z
    assert row["1234"].item_index == 0
    assert lookup(c, PUBLISHED + micro, exclude=z.proposal_id)["1234"].proposal == b


@pytest.mark.parametrize("damage_future", [False, True])
def test_correction_is_seen_at_receipt_only_and_never_promotes_its_replacement(
    tmp_path, damage_future
):
    c = backend_case(tmp_path)
    a = publish(c, 1, PUBLISHED)
    b = publish(c, 2, PUBLISHED + timedelta(minutes=1))
    z = publish(c, 3, PUBLISHED + timedelta(minutes=2))
    received = PUBLISHED + timedelta(hours=1)
    service = ProposalRecordService(c.app, clock=lambda: received.astimezone(UTC))
    service.correct(
        ProposalCorrectionDraft(
            correction_id="synthetic",
            erroneous_ref=ref(z),
            replacement_ref=ref(a),
            reason="合成訂正",
        ),
        expected_latest_ref=ref(z),
    )
    if damage_future:
        with closing(sqlite3.connect(c.app)) as d, d:
            d.execute("DROP TRIGGER proposal_correction_no_update")
            d.execute("UPDATE proposal_correction SET payload='{}'")
    assert lookup(c, received - timedelta(microseconds=1))["1234"].proposal == z
    seen = lookup(c, received)["1234"]
    if damage_future:
        assert seen.status == "unavailable"
        assert seen.proposal is None
    else:
        assert seen.proposal == b
        assert seen.proposal != a


def test_only_known_report_receipts_are_returned_and_retain_proposal_scope(tmp_path):
    c = backend_case(tmp_path)
    saved = publish(c, 3, PUBLISHED)
    receipt = PUBLISHED + timedelta(minutes=30)
    said = PUBLISHED + timedelta(minutes=1)
    report = HumanProposalReportDraft(
        report_id="example",
        proposal_ref=ref(saved),
        outcome="not_purchased",
        statement="その時点での本人の報告。",
        reported_at=said,
    )
    first = ProposalRecordService(c.app, clock=lambda: receipt.astimezone(UTC)).report(report)
    later = receipt + timedelta(microseconds=1)
    ProposalRecordService(c.app, clock=lambda: later).report(
        report.model_copy(update={"report_id": "later"})
    )
    assert not lookup(c, said)["1234"].reports
    assert lookup(c, receipt)["1234"].reports == (first,)
    assert [r.report_id for r in lookup(c, later)["1234"].reports] == ["example", "later"]
    with closing(sqlite3.connect(c.app)) as d, d:
        d.execute("DROP TRIGGER proposal_human_report_no_update")
        d.execute("UPDATE proposal_human_report SET payload='{}' WHERE report_id='later'")
    assert lookup(c, receipt)["1234"].reports == (first,)
    broken = lookup(c, later)["1234"]
    assert broken.status == "unavailable"
    assert broken.proposal is None


def test_unverified_latest_does_not_promote_older_buy_or_poison_another_symbol(tmp_path):
    c = backend_case(tmp_path)
    publish(c, 1, PUBLISHED)
    later = PUBLISHED + timedelta(minutes=1)
    new = publish(c, 2, later)
    with closing(sqlite3.connect(c.app)) as d, d:
        d.execute("DROP TRIGGER investment_proposal_immutable")
        d.execute(
            "UPDATE investment_proposal SET draft_sha256=? WHERE proposal_id=?",
            ("0" * 64, new.proposal_id),
        )
        # Corrupt an otherwise unrelated test index. A broken row is not a negative history.
        d.execute("INSERT INTO investment_proposal_item VALUES('missing',0,'123A','buy',NULL)")
    a = lookup(c, PUBLISHED, tickers=("1234", "123A", "123B"))
    assert a["1234"].status == "available"
    assert a["123A"].status == "unavailable"
    assert a["123B"].status == "not_found"
    b = lookup(c, later)["1234"]
    assert b.status == "unavailable"
    assert b.reason == "prior_buy_record_unreadable"
    assert b.proposal is None


def test_incomplete_buy_is_returned_with_its_limits_still_unconfirmed(tmp_path):
    c = backend_case(tmp_path)
    proposal = reviewed_proposal(c)
    with closing(sqlite3.connect(c.market)) as d, d:
        d.execute("DELETE FROM jquants_master_snapshots")
    raw = proposal.model_dump(mode="json")
    raw["inputs"]["security_classification"] = []
    raw["items"][0].update(
        terms=None,
        proposed_limit_yen="1000",
        terms_unavailable_reasons=[
            {"code": "security_classification_unavailable", "message": "商品分類なし"}
        ],
    )
    draft = bind_review(raw, {c.pair.assessment_id: c.pair.document})
    saved = TradeProposalService(c.app, sqlite_path=c.market, clock=lambda: PUBLISHED).publish(
        draft
    )
    result = lookup(c, PUBLISHED)["1234"]
    assert result.proposal == saved
    assert result.item_index == 0
    historical = result.proposal.items[result.item_index]
    assert historical.terms is None
    assert historical.terms_unavailable_reasons[0].code == "security_classification_unavailable"


@pytest.mark.parametrize("damage", ["action", "ticker", "delete"])
def test_concrete_buy_binding_detects_newer_damaged_item_index_without_fallback(tmp_path, damage):
    c = backend_case(tmp_path)
    publish(c, 1, PUBLISHED)
    later = PUBLISHED + timedelta(minutes=1)
    recent = publish(c, 2, later)
    change = {
        "action": "UPDATE investment_proposal_item SET action='defer' WHERE proposal_id=?",
        "ticker": "UPDATE investment_proposal_item SET ticker='123A' WHERE proposal_id=?",
        "delete": "DELETE FROM investment_proposal_item WHERE proposal_id=?",
    }[damage]
    with closing(sqlite3.connect(c.app)) as connection, connection:
        connection.execute(change, (recent.proposal_id,))
    assert lookup(c, PUBLISHED)["1234"].status == "available"
    result = lookup(c, later, tickers=("1234", "123B"))
    assert result["1234"].status == "unavailable"
    assert result["1234"].proposal is None
    assert result["123B"].status == "not_found"


def test_newer_non_buy_is_not_a_reason_to_read_its_assessment_or_reports(tmp_path):
    c = backend_case(tmp_path)
    earlier = publish(c, 1, PUBLISHED)
    later = PUBLISHED + timedelta(minutes=1)
    non_buy = publish(c, 2, later, action="no_action")
    with closing(sqlite3.connect(c.app)) as connection, connection:
        connection.execute(
            "INSERT INTO proposal_human_report VALUES('broken',?,?,'{}')",
            (non_buy.proposal_id, later.isoformat()),
        )
    result = lookup(c, later)["1234"]
    assert result.status == "available"
    assert result.proposal == earlier
    assert result.reports == ()


def test_nonobject_in_an_indexed_nonbuy_does_not_abort_other_histories(tmp_path):
    c = backend_case(tmp_path)
    original = publish(c, 1, PUBLISHED)
    with closing(sqlite3.connect(c.app)) as connection, connection:
        connection.execute(
            "INSERT INTO investment_proposal "
            "SELECT 'damaged',as_of,prepared_at,published_at,request_scope,"
            "json_set(payload,'$.items',json(?)),draft_sha256 FROM investment_proposal "
            "WHERE proposal_id=?",
            ('["not-an-object"]', original.proposal_id),
        )
        connection.execute(
            "INSERT INTO investment_proposal_item VALUES('damaged',0,'123A','defer',NULL)"
        )
    result = lookup(c, PUBLISHED, tickers=("123A", "1234"))
    assert result["123A"].status == "unavailable"
    assert result["1234"].status == "available"
    assert result["1234"].proposal == original
