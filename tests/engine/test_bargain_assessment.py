from __future__ import annotations

import copy
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from baibai_engine.research.assessment import (
    AssessmentConflictError,
    BargainAssessment,
    BargainAssessmentService,
    assessment_draft_sha256,
)
from baibai_engine.research.assessment_scaffold import (
    UNREVIEWED_DRAFT_SHA256,
    scaffold_assessment,
)
from baibai_engine.screening.shortlist import (
    SelectionBinding,
    Shortlist,
    ShortlistService,
)

JST = ZoneInfo("Asia/Tokyo")
PUBLISHED_AT = datetime(2026, 7, 22, 15, 0, tzinfo=JST)
THESIS_ID = "thesis-20260714-2331-r1"


def _narrative() -> dict[str, object]:
    return {
        "ploss": "中低",
        "why": "受注端境",
        "temporary": "翌期に戻る",
        "structural": "毀損はない",
        "survive": "net cashで耐える",
        "unlock": "還元強化",
        "upside": "正常化でPER12倍相当",
        "downside": "簿価が床",
        "rr": "下値が資産で支えられる",
        "catalyst": "2Q決算",
        "catalyst_date": None,
        "macro": "sizing cautionは該当なし",
        "counter": "構造鈍化",
        "research": "受注残を確認",
        "value": "FV乖離が大きい",
        "prov": "深掘り最優先",
    }


def _publish_shortlist(db_path: Path, *, shortlist_id: str = "shortlist-20260721-test") -> str:
    shortlist = Shortlist.model_validate(
        {
            "schema_version": 4,
            "kind": "shortlist",
            "shortlist_id": shortlist_id,
            "selection_id": "selection-test",
            "run_revision_id": "runrev-test",
            "as_of": "2026-07-21",
            "published_at": "2026-07-21T15:00:00+09:00",
            "profile": "value",
            "macro_context_id": "macro-context-2026-07-21-test",
            "entries": [
                {
                    "ticker": "2331",
                    "decision": "selected",
                    "rank": 1,
                    "reason": "一次IRへ進める",
                    "narrative": _narrative(),
                },
                {
                    "ticker": "0001",
                    "decision": "rejected",
                    "reason": "根拠が弱い",
                    "reject_class": "other",
                },
            ],
        }
    )
    ShortlistService(db_path).publish(
        shortlist,
        selection=SelectionBinding(
            selection_id=shortlist.selection_id,
            run_revision_id=shortlist.run_revision_id,
            as_of=shortlist.as_of,
            profile=shortlist.profile,
            macro_context_id=shortlist.macro_context_id,
            candidate_tickers=frozenset({"2331", "0001"}),
            candidate_er={"2331": 0.12, "0001": 0.04},
        ),
    )
    return shortlist_id


def _draft(db_path: Path, shortlist_id: str) -> dict[str, Any]:
    draft = scaffold_assessment(
        db_path=db_path,
        assessment_id="bargain-assessment-20260722-test",
        as_of=date(2026, 7, 22),
        shortlist_id=shortlist_id,
        thesis_ids=[THESIS_ID],
        proposal_id=None,
        published_at=PUBLISHED_AT,
    )
    lane = draft["lanes"][0]
    lane["disposition"] = "reject"
    lane["disposition_reason"] = "5年期待値が要求利回りに届かない"
    lane["reject_class"] = "price_already_converged"
    for field in (
        "business_model",
        "value_capture",
        "growth_quality",
        "financial_resilience",
        "strongest_countercase",
        "catalyst",
    ):
        lane[field] = f"{field} の判断"
    draft["headline"] = "現時点で買うに値する候補はない"
    draft["comparison"] = "唯一の深掘り候補が要求利回りを満たさなかった"
    draft["forgone"] = "2331 は決算後に再評価する"
    lane["research_questions"] = [
        {"question": "受注残を確認", "answer": "翌期の受注残は横ばい", "status": "answered"}
    ]
    draft["review"]["reviewer_identity"] = "independent-reviewer"
    return draft


def _bound(payload: dict[str, Any]) -> BargainAssessment:
    """review を draft 内容へ束縛する。内容を書き換えたあと最後に呼ぶ。"""
    payload["review"]["draft_sha256"] = assessment_draft_sha256(
        BargainAssessment.model_validate(payload)
    )
    return BargainAssessment.model_validate(payload)


def test_scaffold_fills_machine_values_from_the_thesis_and_leaves_judgment_blank(
    app_method_root: Path,
) -> None:
    db_path = app_method_root / "stores/application/baibai.sqlite"
    shortlist_id = _publish_shortlist(db_path)

    draft = scaffold_assessment(
        db_path=db_path,
        assessment_id="bargain-assessment-20260722-test",
        as_of=date(2026, 7, 22),
        shortlist_id=shortlist_id,
        thesis_ids=[THESIS_ID],
        proposal_id=None,
        published_at=PUBLISHED_AT,
    )

    lane = draft["lanes"][0]
    assert lane["ticker"] == "2331"
    assert lane["machine"]["five_year_base_cagr_pct"] == pytest.approx(9.57)
    assert lane["machine"]["required_return_pct"] == pytest.approx(8.5)
    assert lane["machine"]["fair_value_yen"] == pytest.approx(1300.0)
    assert lane["business_model"] == "TODO"
    assert draft["macro_context_id"] == "macro-context-2026-07-21-test"
    assert draft["result"] == "no_actionable_bargain"


def test_publish_stores_a_no_actionable_bargain_round_and_repeats_without_change(
    app_method_root: Path,
) -> None:
    db_path = app_method_root / "stores/application/baibai.sqlite"
    shortlist_id = _publish_shortlist(db_path)
    assessment = _bound(_draft(db_path, shortlist_id))
    service = BargainAssessmentService(db_path)

    service.publish(assessment)
    service.publish(assessment)

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("SELECT result, shortlist_id FROM bargain_assessment").fetchall()
    assert rows == [("no_actionable_bargain", shortlist_id)]


def test_check_verifies_the_bindings_without_writing(app_method_root: Path) -> None:
    db_path = app_method_root / "stores/application/baibai.sqlite"
    shortlist_id = _publish_shortlist(db_path)
    assessment = _bound(_draft(db_path, shortlist_id))

    BargainAssessmentService(db_path).check(assessment)

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM bargain_assessment").fetchone()[0] == 0


@pytest.mark.parametrize("disposition", ["reject", "defer"])
def test_reject_or_defer_lane_requires_a_known_reject_class(
    app_method_root: Path, disposition: str
) -> None:
    db_path = app_method_root / "stores/application/baibai.sqlite"
    shortlist_id = _publish_shortlist(db_path)
    payload = _draft(db_path, shortlist_id)
    payload["lanes"][0]["disposition"] = disposition
    del payload["lanes"][0]["reject_class"]
    with pytest.raises(ValidationError, match="must include a reject_class"):
        BargainAssessment.model_validate(payload)

    payload["lanes"][0]["reject_class"] = "future_guess"
    with pytest.raises(ValidationError, match="Input should be"):
        BargainAssessment.model_validate(payload)


def test_selected_lane_forbids_reject_class(app_method_root: Path) -> None:
    db_path = app_method_root / "stores/application/baibai.sqlite"
    shortlist_id = _publish_shortlist(db_path)
    payload = _draft(db_path, shortlist_id)
    payload["result"] = "proposal"
    payload["lanes"][0]["disposition"] = "selected"
    payload["entry_timing"] = "決算前に買う理由"
    payload["purchase"] = _purchase_payload(db_path, _seed_proposal(db_path))

    with pytest.raises(ValidationError, match="must not include a reject_class"):
        BargainAssessment.model_validate(payload)


def test_publish_rejects_a_lane_that_the_shortlist_did_not_select(
    app_method_root: Path,
) -> None:
    db_path = app_method_root / "stores/application/baibai.sqlite"
    shortlist_id = _publish_shortlist(db_path)
    payload = _draft(db_path, shortlist_id)
    payload["lanes"][0]["ticker"] = "0001"
    assessment = _bound(payload)

    with pytest.raises(AssessmentConflictError, match="not a selected candidate"):
        BargainAssessmentService(db_path).publish(assessment)


@pytest.mark.parametrize(
    "field",
    ["five_year_base_cagr_pct", "fair_value_yen", "break_even_terminal_multiple"],
)
def test_publish_rejects_machine_values_edited_after_the_scaffold(
    app_method_root: Path, field: str
) -> None:
    db_path = app_method_root / "stores/application/baibai.sqlite"
    shortlist_id = _publish_shortlist(db_path)
    payload = _draft(db_path, shortlist_id)
    payload["lanes"][0]["machine"][field] = 99.0
    assessment = _bound(payload)

    with pytest.raises(AssessmentConflictError, match=field):
        BargainAssessmentService(db_path).publish(assessment)


def test_publish_rejects_a_thesis_hash_that_moved_since_the_draft(
    app_method_root: Path,
) -> None:
    db_path = app_method_root / "stores/application/baibai.sqlite"
    shortlist_id = _publish_shortlist(db_path)
    payload = _draft(db_path, shortlist_id)
    payload["lanes"][0]["thesis_core_sha256"] = "0" * 64
    assessment = _bound(payload)

    with pytest.raises(AssessmentConflictError, match="has moved"):
        BargainAssessmentService(db_path).publish(assessment)


def test_publish_rejects_an_unknown_shortlist(app_method_root: Path) -> None:
    db_path = app_method_root / "stores/application/baibai.sqlite"
    shortlist_id = _publish_shortlist(db_path)
    payload = _draft(db_path, shortlist_id)
    payload["shortlist_id"] = "shortlist-20260721-absent"
    assessment = _bound(payload)

    with pytest.raises(AssessmentConflictError, match="shortlist is unavailable"):
        BargainAssessmentService(db_path).publish(assessment)


def test_publish_conflicts_when_the_same_id_carries_different_content(
    app_method_root: Path,
) -> None:
    db_path = app_method_root / "stores/application/baibai.sqlite"
    shortlist_id = _publish_shortlist(db_path)
    payload = _draft(db_path, shortlist_id)
    service = BargainAssessmentService(db_path)
    service.publish(_bound(payload))

    changed = copy.deepcopy(payload)
    changed["headline"] = "書き換えた結論"

    with pytest.raises(AssessmentConflictError, match="differs from existing"):
        service.publish(_bound(changed))


def test_a_proposal_result_requires_a_purchase_plan_and_entry_timing(
    app_method_root: Path,
) -> None:
    db_path = app_method_root / "stores/application/baibai.sqlite"
    shortlist_id = _publish_shortlist(db_path)
    payload = _draft(db_path, shortlist_id)
    payload["result"] = "proposal"
    payload["lanes"][0]["disposition"] = "selected"
    payload["lanes"][0]["reject_class"] = None

    with pytest.raises(ValidationError, match="requires a purchase plan"):
        BargainAssessment.model_validate(payload)


def test_a_selected_lane_cannot_appear_without_a_proposal_result(
    app_method_root: Path,
) -> None:
    db_path = app_method_root / "stores/application/baibai.sqlite"
    shortlist_id = _publish_shortlist(db_path)
    payload = _draft(db_path, shortlist_id)
    payload["lanes"][0]["disposition"] = "selected"
    payload["lanes"][0]["reject_class"] = None

    with pytest.raises(ValidationError, match="only a proposal result"):
        BargainAssessment.model_validate(payload)


def test_publish_rejects_a_purchase_plan_that_does_not_match_the_proposal(
    app_method_root: Path,
) -> None:
    db_path = app_method_root / "stores/application/baibai.sqlite"
    shortlist_id = _publish_shortlist(db_path)
    proposal_id = _seed_proposal(db_path)
    payload = _draft(db_path, shortlist_id)
    payload["result"] = "proposal"
    payload["lanes"][0]["disposition"] = "selected"
    payload["lanes"][0]["reject_class"] = None
    payload["entry_timing"] = "決算前に買う理由"
    payload["purchase"] = _purchase_payload(db_path, proposal_id)
    payload["purchase"]["limit_price_yen"] = 1.0
    assessment = _bound(payload)

    with pytest.raises(AssessmentConflictError, match="limit_price_yen"):
        BargainAssessmentService(db_path).publish(assessment)


def test_publish_accepts_a_proposal_round_bound_to_the_stored_proposal(
    app_method_root: Path,
) -> None:
    db_path = app_method_root / "stores/application/baibai.sqlite"
    shortlist_id = _publish_shortlist(db_path)
    proposal_id = _seed_proposal(db_path)
    payload = _draft(db_path, shortlist_id)
    payload["result"] = "proposal"
    payload["lanes"][0]["disposition"] = "selected"
    payload["lanes"][0]["reject_class"] = None
    payload["lanes"][0]["disposition_reason"] = "要求利回りを上回る"
    payload["entry_timing"] = "決算前に買う理由"
    payload["purchase"] = _purchase_payload(db_path, proposal_id)

    published = BargainAssessmentService(db_path).publish(_bound(payload))

    assert published.purchase is not None
    assert published.purchase.quantity == 100


def test_scaffold_carries_the_permanent_loss_verdict_and_the_shortlist_question(
    app_method_root: Path,
) -> None:
    db_path = app_method_root / "stores/application/baibai.sqlite"
    shortlist_id = _publish_shortlist(db_path)

    draft = scaffold_assessment(
        db_path=db_path,
        assessment_id="bargain-assessment-20260722-test",
        as_of=date(2026, 7, 22),
        shortlist_id=shortlist_id,
        thesis_ids=[THESIS_ID],
        proposal_id=None,
        published_at=PUBLISHED_AT,
    )

    lane = draft["lanes"][0]
    assert lane["machine"]["permanent_loss_conclusion"] in {"acceptable", "elevated", "unknown"}
    assert lane["research_questions"] == [
        {"question": "受注残を確認", "answer": "TODO", "status": "unresolved"}
    ]
    assert draft["review"]["draft_sha256"] == UNREVIEWED_DRAFT_SHA256


def test_publish_rejects_a_permanent_loss_verdict_edited_after_the_scaffold(
    app_method_root: Path,
) -> None:
    db_path = app_method_root / "stores/application/baibai.sqlite"
    shortlist_id = _publish_shortlist(db_path)
    payload = _draft(db_path, shortlist_id)
    payload["lanes"][0]["machine"]["permanent_loss_conclusion"] = "acceptable"
    payload["lanes"][0]["machine"]["adverse_risk_axes"] = []
    assessment = _bound(payload)

    with pytest.raises(AssessmentConflictError, match="permanent_loss_conclusion"):
        BargainAssessmentService(db_path).publish(assessment)


def test_publish_rejects_a_draft_edited_after_the_content_review(
    app_method_root: Path,
) -> None:
    db_path = app_method_root / "stores/application/baibai.sqlite"
    shortlist_id = _publish_shortlist(db_path)
    payload = _draft(db_path, shortlist_id)
    reviewed = _bound(payload).model_dump(mode="json")
    reviewed["comparison"] = "review 後に書き足した結論"

    with pytest.raises(AssessmentConflictError, match="binds a different draft"):
        BargainAssessmentService(db_path).publish(BargainAssessment.model_validate(reviewed))


def test_publish_rejects_an_unreviewed_draft(app_method_root: Path) -> None:
    db_path = app_method_root / "stores/application/baibai.sqlite"
    shortlist_id = _publish_shortlist(db_path)
    payload = _draft(db_path, shortlist_id)

    with pytest.raises(AssessmentConflictError, match="binds a different draft"):
        BargainAssessmentService(db_path).publish(BargainAssessment.model_validate(payload))


def test_check_passes_an_unreviewed_draft_so_the_expected_hash_can_be_read(
    app_method_root: Path,
) -> None:
    db_path = app_method_root / "stores/application/baibai.sqlite"
    shortlist_id = _publish_shortlist(db_path)
    assessment = BargainAssessment.model_validate(_draft(db_path, shortlist_id))

    BargainAssessmentService(db_path).check(assessment)


def test_publish_rejects_an_assessment_dated_before_its_shortlist(
    app_method_root: Path,
) -> None:
    db_path = app_method_root / "stores/application/baibai.sqlite"
    shortlist_id = _publish_shortlist(db_path)
    payload = _draft(db_path, shortlist_id)
    payload["as_of"] = "2026-07-20"
    assessment = _bound(payload)

    with pytest.raises(AssessmentConflictError, match="precedes shortlist"):
        BargainAssessmentService(db_path).publish(assessment)


def test_a_lane_cannot_drop_the_question_that_earned_it_a_research_slot(
    app_method_root: Path,
) -> None:
    db_path = app_method_root / "stores/application/baibai.sqlite"
    shortlist_id = _publish_shortlist(db_path)
    payload = _draft(db_path, shortlist_id)
    payload["lanes"][0]["research_questions"] = []

    with pytest.raises(ValidationError, match="research_questions"):
        BargainAssessment.model_validate(payload)


def _purchase_payload(db_path: Path, proposal_id: str) -> dict[str, Any]:
    draft = scaffold_assessment(
        db_path=db_path,
        assessment_id="bargain-assessment-20260722-purchase",
        as_of=date(2026, 7, 22),
        shortlist_id="shortlist-20260721-test",
        thesis_ids=[THESIS_ID],
        proposal_id=proposal_id,
        published_at=PUBLISHED_AT,
    )
    purchase = draft["purchase"]
    assert isinstance(purchase, dict)
    return purchase


def _seed_proposal(db_path: Path) -> str:
    proposal_id = "proposal-20260722-2331"
    payload = {
        "planned_limit": {
            "limit_price_yen": 1030.0,
            "quantity": 100,
            "notional_yen": 103000.0,
            "max_acceptable_price_yen": 1300.0,
            "close_yen": 1030.0,
            "price_as_of": "2026-07-21",
            "expires_at": "2026-07-23T15:30:00+09:00",
            "warnings": [],
        }
    }
    with sqlite3.connect(db_path) as connection:
        review_id = connection.execute(
            "SELECT review_id FROM thesis_review WHERE thesis_id = ?", (THESIS_ID,)
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO proposal(
                proposal_id, ticker, thesis_id, review_id, created_at, status, payload
            ) VALUES (?, '2331', ?, ?, '2026-07-22T14:00:00+09:00', 'pending', ?)
            """,
            (proposal_id, THESIS_ID, review_id, json.dumps(payload, sort_keys=True)),
        )
    return proposal_id
