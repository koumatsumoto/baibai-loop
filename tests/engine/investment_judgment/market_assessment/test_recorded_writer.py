"""Strong receipt, independent of an already checked draft's nominal publication date."""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, timedelta

import pytest
from tests.engine.investment_judgment.market_assessment.test_recorded_envelope import (
    TICK,
    C,
    authored,
    strict,
)
from tests.helpers.assessment_history import seed_history

from baibai_engine.cli.investment_judgment import main
from baibai_engine.investment_judgment.market_assessment.query import assessment_by_id
from baibai_engine.investment_judgment.market_assessment.service import AssessmentService
from baibai_engine.primitives.json import canonical_json


@pytest.mark.parametrize("delta", [-1, 0, 1, 120_000_000])
def test_prechecked_draft_receipt_comes_from_actual_publish_and_never_changes(tmp_path, delta):
    db = tmp_path / "app"
    prior = authored("known", C - timedelta(minutes=1))
    late = authored("prechecked", C - 2 * TICK)
    writer = AssessmentService(db, clock=lambda: prior.published_at)
    writer.publish(prior, expected_head=None)
    p = tmp_path / "draft.json"
    p.write_text(json.dumps(late.payload()))
    command = ["market-context", "--db", str(db), "publish", str(p)]
    before = db.read_bytes()
    assert main([*command, "--check"], now=C - 2 * TICK) == 0
    assert db.read_bytes() == before
    assert strict(db).context_id == prior.context_id
    actual = C.astimezone(UTC) + delta * TICK
    assert main([*command, "--expected-head", prior.context_id], now=actual) == 0
    selected = strict(db)
    assert selected.context_id == (late.context_id if delta <= 0 else prior.context_id)
    saved = assessment_by_id(db, context_id=late.context_id)
    assert saved.published_at == late.published_at
    assert saved.recorded_at == actual
    assert saved.document == late.payload()

    def no_clock():
        pytest.fail("exact resume must not resample a receipt")

    before = db.read_bytes()
    assert (
        AssessmentService(db, clock=no_clock).publish(late, expected_head=prior.context_id) == late
    )
    assert db.read_bytes() == before
    with closing(sqlite3.connect(db)) as reader:
        shape = reader.execute(
            "SELECT schema_version,published_at,payload FROM market_context_assessment WHERE context_id=?",
            (late.context_id,),
        ).fetchone()
    assert shape == (7, actual.isoformat(), canonical_json(late.payload()))


def test_unrecorded_legacy_retry_is_not_a_backfill(tmp_path):
    db = tmp_path / "app"
    old = authored("archived", C - TICK)
    seed_history(db, old.payload())
    before = db.read_bytes()
    assert AssessmentService(db, clock=lambda: C).publish(old, expected_head=None) == old
    assert db.read_bytes() == before
    assert assessment_by_id(db, context_id=old.context_id).recorded_at is None
    with pytest.raises(ValueError, match="receipt unknown"):
        strict(db)


def test_future_authored_stamp_is_not_a_receipt_or_new_head(tmp_path):
    db = tmp_path / "app"
    prior = authored("earlier", C - TICK)
    writer = AssessmentService(db, clock=lambda: C)
    writer.publish(prior, expected_head=None)
    before = db.read_bytes()
    with pytest.raises(ValueError, match="follows its actual receipt"):
        writer.publish(authored("future", C + TICK), expected_head=prior.context_id)
    assert db.read_bytes() == before
    assert writer.head_id() == prior.context_id


@pytest.mark.parametrize("delta", [-1, 0, 1])
@pytest.mark.parametrize("legacy_bridge", [False, True])
def test_distinct_cas_must_advance_verified_receipt_even_through_old_writer(
    tmp_path, delta, legacy_bridge
):
    db = tmp_path / "app"
    first = authored("first", C - timedelta(minutes=2))
    AssessmentService(db, clock=lambda: C).publish(first, expected_head=None)
    previous = first.context_id
    if legacy_bridge:
        legacy = authored("bridge", C - timedelta(minutes=1))
        seed_history(db, legacy.payload(), supersedes=previous)
        previous = legacy.context_id
    second = authored("second", C - timedelta(seconds=3))
    writer = AssessmentService(db, clock=lambda: C.astimezone(UTC) + delta * TICK)
    before = db.read_bytes()
    if delta <= 0:
        with pytest.raises(ValueError, match="receipt did not advance"):
            writer.publish(second, expected_head=previous)
        assert db.read_bytes() == before
        assert writer.head_id() == previous
    else:
        assert writer.publish(second, expected_head=previous) == second
        assert strict(db, C + TICK).context_id == second.context_id
    assert assessment_by_id(db, context_id=first.context_id).recorded_at == C
