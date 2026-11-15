"""Mapping内のtyped子modelも、正式readerで読めるpairとしてだけ保存する。"""

from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from tests.helpers.assessment_v1 import assessment_id, pair_payload

from baibai_engine.investment_judgment.independent_review.contracts import AssessmentReview
from baibai_engine.investment_judgment.stock_assessment.document import (
    StockAssessmentDocument,
    assessment_core_hash,
)
from baibai_engine.investment_judgment.stock_assessment.store import (
    AssessmentStoreService,
    load_reviewed_assessment,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_engine.storage.sqlite.application.write import initialize_database

NOW = datetime.fromisoformat("2026-10-14T18:00:00+09:00")


def _pair(*, invalid=None, review_number=1):
    document, review = pair_payload(review_number=review_number)
    assessment = StockAssessmentDocument.model_validate(document)
    checked = AssessmentReview.model_validate(review)
    if invalid == "conclusion_copy":
        conclusion = assessment.conclusion.model_copy(update={"strongest_countercase": ""})
        assessment = assessment.model_copy(update={"conclusion": conclusion})
    elif invalid == "conclusion_construct":
        raw = assessment.conclusion.model_dump(mode="python")
        del raw["strongest_countercase"]
        conclusion = type(assessment.conclusion).model_construct(**raw)
        assessment = assessment.model_copy(update={"conclusion": conclusion})
    elif invalid == "source":
        snapshot = assessment.input_snapshot
        source = snapshot.sources[0].model_copy(update={"ref": "javascript:alert(1)"})
        snapshot = snapshot.model_copy(update={"sources": (source, *snapshot.sources[1:])})
        assessment = assessment.model_copy(update={"input_snapshot": snapshot})
    elif invalid == "projection":
        base = assessment.valuation.base.model_copy(update={"calculation": ""})
        assessment = assessment.model_copy(
            update={"valuation": assessment.valuation.model_copy(update={"base": base})}
        )
    elif invalid == "review_projection":
        # Zero terminal is valid; a tiny negative Review value fits the numeric
        # comparison tolerance but still violates the stored Review's own type.
        base = assessment.valuation.base.model_copy(
            update={"terminal_value_per_share_yen": Decimal(0)}
        )
        assessment = assessment.model_copy(
            update={"valuation": assessment.valuation.model_copy(update={"base": base})}
        )
        projections = tuple(
            item.model_copy(update={"terminal_value_per_share_yen": Decimal("-0.00001")})
            if item.name == "base"
            else item
            for item in checked.recalculated_projections
        )
        checked = checked.model_copy(update={"recalculated_projections": projections})
    checked = checked.model_copy(
        update={"reviewed_assessment_sha256": assessment_core_hash(assessment)}
    )
    return assessment, checked


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize(
    ("invalid", "field"),
    [
        ("conclusion_copy", "strongest_countercase"),
        ("conclusion_construct", "strongest_countercase"),
        ("source", r"HTTP\(S\)"),
        ("projection", "calculation"),
        ("review_projection", "terminal_value_per_share_yen"),
    ],
)
def test_invalid_nested_mapping_cannot_publish_unreadable_pair(tmp_path, existing, invalid, field):
    db = tmp_path / "app.sqlite"
    initialize_database(db)
    service = AssessmentStoreService(db, clock=lambda: NOW)
    previous = None
    if existing:
        document, review = pair_payload()
        previous = service.publish_reviewed_assessment(assessment_id(), document, review)
    before = db.read_bytes()
    document, review = _pair(invalid=invalid, review_number=3)
    with pytest.raises(ValueError, match=field):
        service.publish_reviewed_assessment(
            assessment_id(2),
            dict(document),
            dict(review),
            supersedes_id=assessment_id() if existing else None,
        )
    assert db.read_bytes() == before
    with closing(connect_read_only(db)) as connection:
        for table in ("stock_assessment", "stock_assessment_review"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == int(
                existing
            )
        if previous is not None:
            assert load_reviewed_assessment(connection, previous.assessment_id) == previous


def test_valid_nested_mapping_preserves_canonical_pair_and_retry(tmp_path):
    db = tmp_path / "app.sqlite"
    document, review = _pair()
    saved = AssessmentStoreService(db, clock=lambda: NOW).publish_reviewed_assessment(
        assessment_id(), dict(document), dict(review)
    )
    assert saved.document == document
    assert saved.review == review
    with closing(connect_read_only(db)) as connection:
        assert load_reviewed_assessment(connection, assessment_id()) == saved
        assert connection.execute("SELECT payload FROM stock_assessment").fetchone()[
            0
        ] == canonical_json(document.model_dump(mode="json"))
        assert connection.execute("SELECT payload FROM stock_assessment_review").fetchone()[
            0
        ] == canonical_json(review.model_dump(mode="json"))
    before = db.read_bytes()
    assert (
        AssessmentStoreService(
            db, clock=lambda: NOW + timedelta(days=1)
        ).publish_reviewed_assessment(assessment_id(), dict(document), dict(review))
        == saved
    )
    assert db.read_bytes() == before
