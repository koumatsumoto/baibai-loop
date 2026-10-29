"""旧判断・後日証拠・実行報告を混同しない、有限の保存と再訪受入。"""

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta

import pytest
from tests.helpers.assessment_v1 import assessment_id, pair_payload
from tests.helpers.proposal import PUBLISHED, backend_case, reviewed_proposal

from baibai_engine.investment_judgment.company_research.document import ResearchDraft
from baibai_engine.investment_judgment.company_research.store import ResearchStoreService
from baibai_engine.investment_judgment.judgment_retrospective.document import RetrospectiveDraft
from baibai_engine.investment_judgment.judgment_retrospective.query import (
    get_retrospective,
    list_retrospectives,
    scaffold_retrospective,
)
from baibai_engine.investment_judgment.judgment_retrospective.store import (
    RetrospectiveConflictError,
    RetrospectiveStoreService,
)
from baibai_engine.investment_judgment.stock_assessment.store import AssessmentStoreService
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.investment_judgment.workflow.operation.progress import UnfinishedTarget
from baibai_engine.portfolio.ledger.events import ExecutionEvent, PortfolioLedgerDocument
from baibai_engine.portfolio.ledger.store import LedgerStoreService
from baibai_engine.storage.sqlite.application.read import connect_read_only

NOW = datetime.fromisoformat("2026-10-20T18:00:00+09:00")
AS_OF = datetime.fromisoformat("2026-10-16T18:00:00+09:00").date()
UNRESOLVED = {
    "original_claim": "年度内に主要契約の更新条件を確認できる",
    "observed": "確認する開示はまだない",
    "result": "unresolved",
    "reason": "対象期間が終わっておらず判定材料が不足している",
    "citations": [],
}
LESSON = {
    "next_time": "契約条件と実際の注文条件を分けて再確認する",
    "rationale": "実行価格の差だけで企業仮説が誤ったとは言えない",
}


def _assessment(path, *, number=1, ticker="1234", as_of="2026-10-14", published_at=PUBLISHED):
    document, review = pair_payload(ticker=ticker, as_of=as_of, review_number=number + 100)
    return AssessmentStoreService(path, clock=lambda: published_at).publish_reviewed_assessment(
        assessment_id(number), document, review
    )


def _draft(path, *, identity="retro-1", as_of=AS_OF, **origin):
    result = scaffold_retrospective(
        retrospective_id=identity,
        as_of=as_of,
        milestone="契約更新の節目",
        db_path=path,
        known_at=NOW,
        **origin,
    )
    raw = result["draft"]
    raw["document"]["comparisons"] = [deepcopy(UNRESOLVED)]
    return raw


def _research(
    path, *, identity="research-1", ticker="1234", as_of="2026-10-14", saved_at=NOW, prior=None
):
    # Old information acquired later is useful evidence, not an intraday PIT claim.
    return ResearchStoreService(path, clock=lambda: saved_at).save(
        ResearchDraft.model_validate(
            {
                "research_id": identity,
                "prior_research_ref": None if prior is None else prior.model_dump(mode="json"),
                "document": {
                    "ticker": ticker,
                    "as_of": as_of,
                    "investigated_scope": "契約更新資料の確認",
                    "sources": [
                        {
                            "source_id": "price",
                            "ticker": ticker,
                            "source_tier": "primary",
                            "ref": "https://example.test/contract",
                            "retrieved_at": saved_at.isoformat(),
                            "as_of": as_of,
                            "used_for": "価格ではなく契約更新の開示を確認",
                        }
                    ],
                    "facts": [
                        {
                            "fact_id": "close",
                            "fact_kind": "other",
                            "value": "契約更新の条件が公表された",
                            "unit": "text",
                            "as_of": as_of,
                            "source_ids": ["price"],
                        }
                    ],
                },
            }
        )
    )


def _save(path, raw, *, now=NOW):
    return RetrospectiveStoreService(path, clock=lambda: now).save(
        RetrospectiveDraft.model_validate(raw)
    )


def _publish(case, *, action="buy"):
    return TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(reviewed_proposal(case, action=action))


def test_buy_roundtrip_reuses_scoped_evidence_without_rewriting_old_records(tmp_path):
    case = backend_case(tmp_path)
    proposal = _publish(case)
    research = _research(case.app)
    raw = _draft(case.app, proposal_id=proposal.proposal_id, ticker="1234")
    raw["document"]["comparisons"] = [
        {
            **UNRESOLVED,
            "observed": "更新条件が開示され、以前の条件との差が分かった",
            "result": "supported",
            "reason": "元の対象期間内に条件を確認できた",
            "citations": [
                {
                    "reference": research.reference.model_dump(mode="json"),
                    "source_ids": ["price"],
                    "fact_ids": ["close"],
                },
                {
                    "reference": proposal.items[0].assessment_ref.model_dump(mode="json"),
                    "source_ids": ["price"],
                    "fact_ids": ["close"],
                },
            ],
        }
    ]
    raw["document"]["lesson"] = LESSON
    tables = (
        "stock_assessment",
        "stock_assessment_review",
        "company_research",
        "investment_proposal",
        "investment_proposal_item",
        "operation_session",
        "ledger_event",
        "ledger_meta",
    )
    with closing(connect_read_only(case.app)) as connection:
        before = {name: connection.execute(f"SELECT * FROM {name}").fetchall() for name in tables}
    saved = _save(case.app, raw)
    with closing(connect_read_only(case.app)) as connection:
        after = {name: connection.execute(f"SELECT * FROM {name}").fetchall() for name in tables}
        assert after == before
    document, review = pair_payload(as_of="2026-10-16", review_number=202)
    AssessmentStoreService(case.app, clock=lambda: NOW).publish_reviewed_assessment(
        assessment_id(2), document, review, supersedes_id=case.pair.assessment_id
    )
    _research(case.app, identity="research-latest", as_of="2026-10-20", prior=research.reference)
    result = get_retrospective(retrospective_id="retro-1", db_path=case.app)
    assert result["record"] == saved.model_dump(mode="json")
    origin = result["context"]["origin"]
    assert origin["proposal"]["item"] == proposal.items[0].model_dump(mode="json")
    assert origin["proposal"]["forgone"] == proposal.forgone
    assert origin["assessment"]["reference"]["assessment_id"] == case.pair.assessment_id
    citations = result["context"]["citations"]
    assert citations[0]["record"]["reference"] == research.reference.model_dump(mode="json")
    assert (
        citations[0]["record"]["document"]["facts"][0]["value"]
        != citations[1]["record"]["document"]["input_snapshot"]["facts"][0]["value"]
    )
    # No market/runs path or current cash eligibility is supplied to retrospective calls.
    assert (
        list_retrospectives(db_path=case.app, ticker="1234")["items"][0]["reference"]
        == result["record"]["reference"]
    )


def test_no_action_defer_without_assessment_and_assessment_only_keep_original_meaning(tmp_path):
    case = backend_case(tmp_path, tickers=("1234", "5678"))
    case.operation = case.operations.record_outcome(
        case.operation.operation_id,
        UnfinishedTarget(
            ticker="5678",
            purpose="new",
            unfinished_reason="一次資料の確認が未了",
            references=("https://example.test/pending",),
        ),
        now=PUBLISHED - timedelta(minutes=12),
    )
    proposal = _publish(case, action="no_action")
    origins = (
        ("no-action", {"proposal_id": proposal.proposal_id, "ticker": "1234"}),
        ("defer", {"proposal_id": proposal.proposal_id, "ticker": "5678"}),
        ("assessment", {"assessment_id": case.pair.assessment_id}),
    )
    contexts = {}
    for identity, origin in origins:
        _save(case.app, _draft(case.app, identity=identity, **origin))
        result = get_retrospective(retrospective_id=identity, db_path=case.app)
        contexts[identity] = result["context"]
    assert contexts["no-action"]["origin"]["proposal"]["item"]["action"] == "no_action"
    assert contexts["defer"]["origin"]["proposal"]["item"]["resume_conditions"]
    assert contexts["defer"]["origin"]["assessment"] is None
    assert contexts["assessment"]["origin"]["proposal"] is None
    assert contexts["assessment"]["ledger"] is None


def test_unresolved_retry_correction_and_recent_list_are_independent_records(tmp_path):
    path = tmp_path / "app.sqlite"
    pair = _assessment(path)
    raw = _draft(path, assessment_id=pair.assessment_id)
    first = _save(path, raw)
    assert _save(path, raw, now=NOW + timedelta(days=30)) == first
    changed = deepcopy(raw)
    changed["document"]["milestone"] = "内容変更"
    with pytest.raises(RetrospectiveConflictError, match="immutable"):
        _save(path, changed)
    later = deepcopy(raw)
    later["retrospective_id"] = "later-milestone"
    later["document"]["as_of"] = "2026-10-17"
    _save(path, later, now=NOW + timedelta(seconds=1))
    correction = deepcopy(raw)
    correction["retrospective_id"] = "correction"
    correction["document"]["corrects_ref"] = first.reference.model_dump(mode="json")
    correction["document"]["lesson"] = {
        "next_time": "対象期間を直す",
        "rationale": "前記録の期間が誤記だった",
    }
    corrected = _save(path, correction, now=NOW + timedelta(seconds=2))
    pair2 = _assessment(path, number=2, ticker="5678")
    _save(path, _draft(path, identity="other", assessment_id=pair2.assessment_id))
    recent = list_retrospectives(db_path=path, ticker="1234", limit=1)
    assert recent["has_more"] is True
    assert recent["items"][0]["reference"] == corrected.reference.model_dump(mode="json")
    assert recent["items"][0]["corrects_ref"] == first.reference.model_dump(mode="json")
    assert len(list_retrospectives(db_path=path)["items"]) == 4
    original = get_retrospective(retrospective_id="retro-1", db_path=path)
    assert original["record"] == first.model_dump(mode="json")
    document, review = pair_payload(review_number=303)
    different_judgment = AssessmentStoreService(
        path, clock=lambda: PUBLISHED + timedelta(minutes=1)
    ).publish_reviewed_assessment(
        assessment_id(3), document, review, supersedes_id=pair.assessment_id
    )
    wrong_origin = _draft(
        path, identity="wrong-correction", assessment_id=different_judgment.assessment_id
    )
    wrong_origin["document"].update(
        corrects_ref=first.reference.model_dump(mode="json"), lesson=LESSON
    )
    with pytest.raises(ValueError, match="same ticker and origin"):
        _save(path, wrong_origin)
    with pytest.raises(ValueError, match="between 1 and 100"):
        list_retrospectives(db_path=path, limit=0)


def test_content_citation_and_publication_boundaries(tmp_path):
    path = tmp_path / "app.sqlite"
    pair = _assessment(path, published_at=PUBLISHED + timedelta(days=1))
    with pytest.raises(ValueError, match="origin publication"):
        _draft(path, as_of=PUBLISHED.date(), assessment_id=pair.assessment_id)
    raw = _draft(
        path, as_of=(PUBLISHED + timedelta(days=1)).date(), assessment_id=pair.assessment_id
    )
    # Same-day origin publication, and old documents acquired after as_of, are accepted.
    research = _research(path)
    citation = {
        "reference": research.reference.model_dump(mode="json"),
        "source_ids": ["price"],
        "fact_ids": ["close"],
    }
    raw["document"]["comparisons"][0].update(result="supported", citations=[citation])
    _save(path, raw)
    invalid = []
    empty = deepcopy(raw)
    empty["document"]["comparisons"] = []
    invalid.append((empty, "requires a comparison or lesson"))
    unsupported = deepcopy(raw)
    unsupported["document"]["comparisons"][0]["citations"] = []
    invalid.append((unsupported, "requires a citation"))
    unresolved = deepcopy(raw)
    unresolved["document"]["comparisons"] = [{**UNRESOLVED, "reason": ""}]
    invalid.append((unresolved, "at least 1 character"))
    unknown_source = deepcopy(raw)
    unknown_source["document"]["comparisons"][0]["citations"][0]["source_ids"] = ["annual"]
    invalid.append((unknown_source, "unknown source"))
    wrong_digest = deepcopy(raw)
    wrong_digest["document"]["comparisons"][0]["citations"][0]["reference"]["payload_sha256"] = (
        "a" * 64
    )
    invalid.append((wrong_digest, "reference differs"))
    research_origin = deepcopy(raw)
    research_origin["document"]["origin"] = research.reference.model_dump(mode="json")
    invalid.append((research_origin, "validation errors"))
    arbitrary_events = deepcopy(raw)
    arbitrary_events["event_ids"] = []
    invalid.append((arbitrary_events, "Extra inputs"))
    for index, (candidate, error) in enumerate(invalid):
        candidate["retrospective_id"] = f"bad-{index}"
        with pytest.raises(ValueError, match=error):
            _save(path, candidate)
    future = _research(path, identity="future", as_of="2026-10-16", prior=research.reference)
    raw["retrospective_id"] = "future-citation"
    raw["document"]["comparisons"][0]["citations"][0]["reference"] = future.reference.model_dump(
        mode="json"
    )
    with pytest.raises(ValueError, match="information/recording time"):
        _save(path, raw)
    raw["document"]["as_of"] = "2026-10-16"
    with pytest.raises(ValueError, match="information/recording time"):
        _save(path, raw, now=NOW - timedelta(seconds=1))
    wrong_ticker = _research(path, identity="other-ticker", ticker="5678")
    raw["document"]["comparisons"][0]["citations"][0]["reference"] = (
        wrong_ticker.reference.model_dump(mode="json")
    )
    with pytest.raises(ValueError, match="ticker mismatch"):
        _save(path, raw)
    assert len(list_retrospectives(db_path=path)["items"]) == 1


def test_service_derived_ledger_preserves_partial_fills_release_cutoff_and_late_reports(tmp_path):
    case = backend_case(tmp_path)
    proposal = _publish(case)
    ledger_service = LedgerStoreService(case.app)
    initial, head = ledger_service.load_with_head()
    order = {
        "type": "reservation",
        "event_id": "order",
        "reservation_id": "reservation",
        "order_id": "broker-order",
        "occurred_at": "2026-10-15T09:00:00+09:00",
        "ticker": "1234",
        "side": "buy",
        "sector": "情報・通信業",
        "decision_reference": proposal.proposal_id,
        "quantity": 400,
        "price_guard_yen": "1000",
    }
    fill = {
        "type": "execution",
        "event_id": "fill-1",
        "execution_id": "broker-fill-1",
        "reservation_id": "reservation",
        "occurred_at": "2026-10-15T10:00:00+09:00",
        "occurred_on": "2026-10-15",
        "ticker": "1234",
        "side": "buy",
        "quantity": 100,
        "price_yen": "950",
    }
    release = {
        "type": "release",
        "event_id": "release",
        "reservation_id": "reservation",
        "occurred_at": "2026-10-16T10:00:00+09:00",
        "reason": "cancelled",
    }
    events = [
        *[event.model_dump(mode="json") for event in initial.events],
        {
            "type": "contribution",
            "event_id": "later-capital",
            "occurred_at": "2026-10-15T08:00:00+09:00",
            "amount_yen": 500000,
        },
        order,
        fill,
        {
            **fill,
            "event_id": "fill-2",
            "execution_id": "broker-fill-2",
            "decision_reference": proposal.proposal_id,
        },
        release,
        {
            **order,
            "event_id": "other-order",
            "reservation_id": "other",
            "order_id": "other",
            "occurred_at": "2026-10-16T11:00:00+09:00",
            "decision_reference": "other-proposal",
            "quantity": 100,
        },
        {
            **release,
            "event_id": "other-release",
            "reservation_id": "other",
            "occurred_at": "2026-10-16T12:00:00+09:00",
            "decision_reference": proposal.proposal_id,
        },
        {
            **fill,
            "event_id": "manual-fill",
            "execution_id": "manual-fill",
            "reservation_id": None,
            "occurred_at": "2026-10-16T13:00:00+09:00",
            "occurred_on": None,
            "decision_reference": "manual-fact-other",
            "quantity": 100,
            "sector": "情報・通信業",
        },
        {
            **order,
            "event_id": "future-order",
            "reservation_id": "future",
            "order_id": "future",
            "occurred_at": "2026-10-17T09:00:00+09:00",
            "quantity": 100,
        },
    ]
    replacement = PortfolioLedgerDocument.model_validate(
        {
            **initial.model_dump(mode="json"),
            "as_of": "2026-10-17T15:00:00+09:00",
            "events": events,
        }
    )
    ledger_service.apply_document(
        expected_head=head, expected_document=initial, replacement=replacement
    )
    raw = _draft(case.app, proposal_id=proposal.proposal_id, ticker="1234")
    raw["document"].update(comparisons=[], lesson=LESSON)
    old = _save(case.app, raw)
    assert old.ledger.event_ids == ("order", "fill-1", "fill-2", "release")
    context = get_retrospective(retrospective_id="retro-1", db_path=case.app)["context"]
    assert context["ledger"]["events"][1]["occurred_on"] == "2026-10-15"
    current, head = ledger_service.load_with_head()
    delayed = ExecutionEvent.model_validate(
        {
            **fill,
            "event_id": "late-fill",
            "execution_id": "late-fill",
            "occurred_at": "2026-10-15T12:00:00+09:00",
            "quantity": 100,
        }
    )
    updated = current.model_copy(
        update={
            "events": tuple(sorted((*current.events, delayed), key=lambda event: event.occurred_at))
        }
    )
    ledger_service.apply_document(
        expected_head=head, expected_document=current, replacement=updated
    )
    assert _save(case.app, raw) == old
    assert get_retrospective(retrospective_id="retro-1", db_path=case.app)["context"] == context
    fresh = _draft(case.app, identity="new-head", proposal_id=proposal.proposal_id, ticker="1234")
    assert _save(case.app, fresh).ledger.event_ids == (
        "order",
        "fill-1",
        "fill-2",
        "late-fill",
        "release",
    )
    # Neither explicit unread/absent nor a read empty prefix is silently upgraded to latest.
    raw["retrospective_id"], raw["ledger_append_head"] = "unread", None
    assert _save(case.app, raw).ledger is None
    raw["retrospective_id"], raw["ledger_append_head"] = "empty-prefix", 0
    assert _save(case.app, raw).ledger.event_ids == ()
    raw["retrospective_id"], raw["ledger_append_head"] = "future-head", head + 100
    with pytest.raises(ValueError, match="outside the recorded prefix"):
        _save(case.app, raw)
