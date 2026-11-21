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
from baibai_engine.investment_judgment.market_assessment.service import (
    AssessmentConflictError,
    AssessmentService,
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
    # Build headers independently to exercise how readers handle mixed or damaged history.
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


@pytest.mark.parametrize("shape", ["only", "successor", "independent", "future_stamp"])
def test_strict_refuses_unknown_envelope_without_borrowing_its_clock(tmp_path, shape):
    db = tmp_path / "app"
    prior = authored("verified", C - TICK)
    if shape != "only":
        envelope(db, prior, prior.published_at)
    unknown = authored("unknown-edition", C - TICK).payload()
    unknown["schema_version"] = 98
    if shape == "future_stamp":
        unknown["published_at"] = (C + TICK).isoformat()
    seed_history(db, unknown, supersedes=prior.context_id if shape == "successor" else None)
    with pytest.raises(
        ValueError, match=r"unsupported market publication envelope 98: .*unknown-edition"
    ):
        strict(db)
    ordinary = latest_assessment(db, as_of=C.date(), known_at=C)
    assert (ordinary.context_id if ordinary else None) == (
        None if shape == "only" else prior.context_id
    )
    assert assessment_by_id(db, context_id=unknown["context_id"]).document == unknown


def test_strict_does_not_inspect_unknown_edition_for_a_future_business_date(tmp_path):
    db = tmp_path / "app"
    future = authored("future-edition", C).payload()
    future.update(schema_version=98, as_of=(C.date() + timedelta(days=1)).isoformat())
    seed_history(db, future)
    assert strict(db) is None
    assert assessment_by_id(db, context_id=future["context_id"]).document == future


def test_default_writer_samples_wall_clock_and_retry_never_retimes(tmp_path):
    db = tmp_path / "app"
    old_time = datetime(2001, 1, 1, 12, tzinfo=UTC)
    doc = MarketContextAssessment(
        context_id="market-context-assessment-2001-01-01-measured",
        as_of=old_time.date(),
        known_at=old_time,
        published_at=old_time,
        summary="合成",
        inputs=(),
    )
    before = datetime.now(UTC)
    AssessmentService(db).publish(doc, expected_head=None)
    after = datetime.now(UTC)
    saved = assessment_by_id(db, context_id=doc.context_id)
    assert before <= saved.recorded_at <= after
    assert saved.document == doc.payload()
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT payload FROM market_context_assessment").fetchone()[0] == (
            canonical_json(doc.payload())
        )

    def no_clock():
        pytest.fail("idempotence and CAS must be decided before sampling receipt")

    writer = AssessmentService(db, clock=no_clock)
    intact = db.read_bytes()
    assert writer.publish(doc, expected_head=None) == doc
    with pytest.raises(AssessmentConflictError, match="head changed"):
        writer.publish(authored("other", C), expected_head=None)
    assert db.read_bytes() == intact


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_new_receipt_must_follow_the_last_receipted_ancestor(tmp_path, delta):
    db = tmp_path / "app"
    first, legacy, fresh = (authored(x, C - 2 * TICK) for x in ("first", "legacy", "fresh"))
    AssessmentService(db, clock=lambda: C).publish(first, expected_head=None)
    seed_history(db, legacy.payload(), supersedes=first.context_id)
    before = db.read_bytes()
    writer = AssessmentService(db, clock=lambda: C.astimezone(UTC) + delta * TICK)
    assert writer.publish(legacy, expected_head=first.context_id) == legacy
    assert assessment_by_id(db, context_id=legacy.context_id).recorded_at is None
    assert db.read_bytes() == before
    if delta > 0:
        writer.publish(fresh, expected_head=legacy.context_id)
        assert assessment_by_id(db, context_id=fresh.context_id).recorded_at == C + TICK
    else:
        with pytest.raises(AssessmentConflictError, match="receipt did not advance"):
            writer.publish(fresh, expected_head=legacy.context_id)
        assert db.read_bytes() == before


def test_future_author_stamp_does_not_displace_head(tmp_path):
    db = tmp_path / "app"
    prior = authored("prior", C - 2 * TICK)
    writer = AssessmentService(db, clock=lambda: C)
    writer.publish(prior, expected_head=None)
    before = db.read_bytes()
    with pytest.raises(ValueError, match="follows its actual receipt"):
        writer.publish(authored("future", C + 2 * TICK), expected_head=prior.context_id)
    assert writer.head_id() == prior.context_id
    assert db.read_bytes() == before
