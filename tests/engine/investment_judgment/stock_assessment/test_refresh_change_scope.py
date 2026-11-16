"""A new refresh must not call changed reviewed material unchanged."""

import sqlite3
from contextlib import closing
from copy import deepcopy
from datetime import datetime

import pytest
from tests.helpers.assessment_v1 import (
    NOW,
    assessment_id,
    pair_payload,
    review_id,
    update_review_hash,
)

from baibai_engine.investment_judgment.stock_assessment.document import (
    StockAssessmentDocument,
    assessment_core_hash,
)
from baibai_engine.investment_judgment.stock_assessment.store import (
    ResearchValidationError,
    load_reviewed_assessment,
    publish_reviewed_assessment,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.sqlite.application.schema import SCHEMA_SQL

LATER = datetime.fromisoformat("2026-10-14T16:16:00+09:00")


@pytest.fixture
def case():
    with closing(sqlite3.connect(":memory:")) as connection, connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(SCHEMA_SQL)
        connection.execute("BEGIN IMMEDIATE")
        document, review = pair_payload()
        prior = publish_reviewed_assessment(
            connection, assessment_id(), document, review, published_at=NOW
        )
        draft, new_review = pair_payload(review_number=4)
        draft["conclusion"]["assessed_at"] = "2026-10-14T16:10:00+09:00"
        draft["refresh"] = {
            "prior_assessment_ref": {
                "assessment_id": prior.assessment_id,
                "core_sha256": prior.core_sha256,
                "review_id": prior.review.review_id,
            },
            "checked_at": "2026-10-14T16:08:00+09:00",
            "material_information_check": "合成例: 旧資料と今回の材料を確認した。",
            "changed_sections": [],
            "unchanged_assumptions_reason": "合成例: 元の事業・資金の仮定を維持する。",
            "horizon_reason": "同日起点の同じ期間を確認した。",
            "distribution_reason": "同じ期間内分配を確認した。",
            "corporate_action_reason": "同じ起点一株の仮定で比較する。",
        }
        new_review["reviewed_at"] = "2026-10-14T16:15:00+09:00"
        yield connection, prior, draft, new_review


def _change(draft, section):
    if section == "valuation":
        draft[section]["required_annual_return_pct"] = "5"
    elif section == "findings":
        draft[section]["hypotheses"] = [
            {
                "statement": "産業の需要増がこの会社の利益へ届く。",
                "origin": "research",
                "result": "refuted",
                "reason": "合成反例: 数量増より調達費用増が大きい。",
                "source_ids": ["annual"],
            }
        ]
    elif section == "investment_case":
        draft[section]["invalidation_conditions"].append("主要顧客の契約を失う。")
    elif section == "risks":
        draft["permanent_loss_risks"][0]["assessment"] = "adverse"
        draft["permanent_loss_risks"][0]["summary"] = "合成反例: 返済時の資金余力が低下した。"
    elif section == "sources":
        draft["input_snapshot"]["sources"][0]["used_for"] += " 訂正後の対象期間を確認。"
    else:
        assert section == "model_perspectives"
        draft[section] = [
            {
                "model": "industry_trends",
                "assessment": "adverse",
                "summary": "需要増より費用増が大きく、利益仮定を反証する。",
                "source_ids": ["annual"],
            }
        ]


def _publish(connection, prior, draft, review, *, number=3):
    update_review_hash(draft, review)
    return publish_reviewed_assessment(
        connection,
        assessment_id(number),
        draft,
        review,
        published_at=LATER,
        supersedes_id=prior.assessment_id,
    )


@pytest.mark.parametrize(
    "section",
    ["valuation", "findings", "investment_case", "risks", "sources", "model_perspectives"],
)
def test_new_refresh_rejects_omitted_changed_section_and_accepts_declaration(case, section):
    connection, prior, draft, review = case
    original_payload = connection.execute(
        "SELECT payload, core_sha256 FROM stock_assessment WHERE assessment_id=?",
        (prior.assessment_id,),
    ).fetchone()
    _change(draft, section)
    # The new review checks the new hash. It must not hide an inaccurate change summary.
    with pytest.raises(ResearchValidationError, match=f"changed_sections omits: {section}"):
        _publish(connection, prior, draft, review)
    assert connection.execute("SELECT count(*) FROM stock_assessment").fetchone()[0] == 1
    draft["refresh"]["changed_sections"] = [section]
    saved = _publish(connection, prior, draft, review)
    assert saved.document.refresh.changed_sections == (section,)
    assert load_reviewed_assessment(connection, saved.assessment_id) == saved
    assert (
        connection.execute(
            "SELECT payload, core_sha256 FROM stock_assessment WHERE assessment_id=?",
            (prior.assessment_id,),
        ).fetchone()
        == original_payload
    )


@pytest.mark.parametrize("declared", [[], ["valuation", "risks"]])
def test_unchanged_material_and_conservative_review_scope_remain_valid(case, declared):
    connection, prior, draft, review = case
    draft["refresh"]["changed_sections"] = declared
    saved = _publish(connection, prior, draft, review)
    assert saved.document.valuation == prior.document.valuation
    assert saved.document.refresh.changed_sections == tuple(declared)


def test_changed_numeric_spelling_alone_does_not_require_section(case):
    connection, prior, draft, review = case
    draft["valuation"]["required_annual_return_pct"] += ".000"
    saved = _publish(connection, prior, draft, review)
    assert saved.document.valuation == prior.document.valuation
    assert saved.document.refresh.changed_sections == ()


def test_all_omitted_sections_are_reported_together(case):
    connection, prior, draft, review = case
    _change(draft, "findings")
    _change(draft, "investment_case")
    with pytest.raises(
        ResearchValidationError, match="changed_sections omits: findings, investment_case"
    ):
        _publish(connection, prior, draft, review)


def test_old_exact_and_same_id_retry_preserve_historical_omission(case):
    connection, prior, draft, review = case
    _change(draft, "valuation")
    update_review_hash(draft, review)
    document = StockAssessmentDocument.model_validate(draft)
    # Seed a pre-fix publication with correct immutable identity, not through the new writer.
    connection.execute(
        "INSERT INTO stock_assessment(assessment_id,ticker,as_of,stance,published_at,"
        "supersedes_id,payload,core_sha256) VALUES (?,?,?,?,?,?,?,?)",
        (
            assessment_id(3),
            "1234",
            document.input_snapshot.as_of.isoformat(),
            document.conclusion.stance,
            LATER.isoformat(),
            prior.assessment_id,
            canonical_json(document.model_dump(mode="json")),
            assessment_core_hash(document),
        ),
    )
    connection.execute(
        "INSERT INTO stock_assessment_review(review_id,assessment_id,reviewed_at,payload) "
        "VALUES (?,?,?,?)",
        (review["review_id"], assessment_id(3), review["reviewed_at"], canonical_json(review)),
    )
    old_rows = connection.execute(
        "SELECT payload, core_sha256 FROM stock_assessment ORDER BY assessment_id"
    ).fetchall()
    exact = load_reviewed_assessment(connection, assessment_id(3))
    retry = _publish(connection, prior, draft, review)
    assert retry == exact
    assert exact.document.refresh.changed_sections == ()
    assert (
        connection.execute(
            "SELECT payload, core_sha256 FROM stock_assessment ORDER BY assessment_id"
        ).fetchall()
        == old_rows
    )
    copied = deepcopy(draft)
    copied["valuation"]["required_annual_return_pct"] = "4"
    review["review_id"] = review_id(6)
    with pytest.raises(ResearchValidationError, match="changed_sections omits: valuation"):
        _publish(connection, exact, copied, review, number=5)
