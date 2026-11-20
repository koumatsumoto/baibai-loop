"""受領時計のある保存版と、実時計を復元できない旧判断の読取対照。"""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from tests.helpers.assessment_history import seed_history

from baibai_engine.investment_judgment.market_assessment.models import MarketContextAssessment
from baibai_engine.investment_judgment.market_assessment.projection import assessment_projection
from baibai_engine.investment_judgment.market_assessment.query import (
    assessment_by_id,
    assessment_history,
    latest_assessment,
    latest_assessment_from_connection,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_engine.storage.sqlite.application.write import initialize_database

C = datetime.fromisoformat("2026-10-14T18:00:00+09:00")
TICK = timedelta(microseconds=1)


def authored(suffix, declared):
    return MarketContextAssessment(
        context_id=f"market-context-assessment-{C.date()}-{suffix}",
        as_of=C.date(),
        known_at=C - timedelta(minutes=2),
        published_at=declared,
        summary=suffix,
        inputs=(),
    )


def envelope(db, document, receipt, *, supersedes=None):
    # Independent fixture: writer is deliberately unchanged in the reader-only stage.
    initialize_database(db)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(
            "INSERT INTO market_context_assessment VALUES(?,?,?,?,?,?)",
            (
                document.context_id,
                7,
                document.as_of.isoformat(),
                receipt.isoformat(),
                supersedes,
                canonical_json(document.payload()),
            ),
        )


def strict(db, at=C):
    with closing(connect_read_only(db)) as conn:
        conn.execute("BEGIN")
        return latest_assessment_from_connection(
            conn, as_of=C.date(), known_at=at, require_receipt=True
        )


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_writer_receipt_is_distinct_from_unchanged_authored_publication(tmp_path, delta):
    db = tmp_path / "app"
    prior = authored("prior", C - timedelta(minutes=1))
    later = authored("later", C - 2 * TICK)
    receipt = C.astimezone(UTC) + delta * TICK
    envelope(db, prior, prior.published_at)
    envelope(db, later, receipt, supersedes=prior.context_id)
    saved = assessment_by_id(db, context_id=later.context_id)
    assert saved.document == later.payload()
    assert saved.published_at == later.published_at
    assert saved.recorded_at == receipt
    assert assessment_projection(saved, as_of=C.date())["recorded_at"] == receipt.isoformat()
    expected = later if delta <= 0 else prior
    assert strict(db).context_id == expected.context_id
    assert latest_assessment(db, as_of=C.date(), known_at=C).context_id == expected.context_id
    assert assessment_history(db)[0].document == later.payload()
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute(
            "SELECT payload FROM market_context_assessment WHERE context_id=?", (later.context_id,)
        ).fetchone()[0] == canonical_json(later.payload())


def test_legacy_receipt_is_unknown_not_absence_or_retroactive_fact(tmp_path):
    db = tmp_path / "app"
    initialize_database(db)
    assert strict(db) is None
    old = authored("old", C - 3 * TICK)
    seed_history(db, old.payload())
    assert assessment_by_id(db, context_id=old.context_id).recorded_at is None
    assert latest_assessment(db, as_of=C.date(), known_at=C).document == old.payload()
    with pytest.raises(ValueError, match="receipt unknown"):
        strict(db)
    new = authored("new", C - 2 * TICK)
    envelope(db, new, C, supersedes=old.context_id)
    assert strict(db).document == new.payload()
    with pytest.raises(ValueError, match="receipt unknown"):
        strict(db, C - TICK)
    # Later old writer or disconnected import: lower *declared* time cannot prove
    # it preceded a verified candidate. Don't silently choose the visible known row.
    other = authored("unclocked", C - 4 * TICK)
    seed_history(db, other.payload(), supersedes=new.context_id)
    with pytest.raises(ValueError, match="unclocked"):
        strict(db)
    assert assessment_by_id(db, context_id=old.context_id).document == old.payload()


def test_damaged_writer_receipt_fails_without_fallback(tmp_path):
    db = tmp_path / "app"
    item = authored("bad", C)
    envelope(db, item, C - TICK)
    with pytest.raises(ValueError, match="receipt differs"):
        strict(db)
