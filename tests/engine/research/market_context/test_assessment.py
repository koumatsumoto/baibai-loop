from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime

import pytest
from tests.helpers.assessment_history import historical_context_payload as historical_payload
from tests.helpers.assessment_history import seed_history

from baibai_engine.investment_judgment.market_assessment.cli import main
from baibai_engine.investment_judgment.market_assessment.models import (
    MarketContextAssessment,
    StoredAssessment,
)
from baibai_engine.investment_judgment.market_assessment.projection import assessment_projection
from baibai_engine.investment_judgment.market_assessment.query import (
    assessment_by_id,
    assessment_history,
    latest_assessment,
    select_assessment,
)
from baibai_engine.investment_judgment.market_assessment.service import (
    AssessmentConflictError,
    AssessmentService,
)


def document(
    *,
    context_id="market-context-assessment-2026-07-17-small",
    published_at="2026-07-18T12:00:00+00:00",
    summary="重要な変化は小さい。",
):
    return MarketContextAssessment.model_validate(
        {
            "context_id": context_id,
            "as_of": "2026-07-17",
            "known_at": "2026-07-17T06:00:00+00:00",
            "published_at": published_at,
            "summary": summary,
            "inputs": [],
        }
    )


def stored(doc):
    return StoredAssessment.from_payload(doc.payload())


def test_small_judgment_does_not_require_chapters_scenarios_or_scorecard():
    doc = document()
    assert doc.changes == doc.counter_evidence == doc.uncertainties == doc.implications == ()
    assert not {"core", "scenarios", "scorecard", "synthesis"} & doc.payload().keys()
    with pytest.raises(ValueError, match="schema_version"):
        MarketContextAssessment.model_validate(historical_payload())


def test_writer_preserves_old_payload_and_head_across_schemas(tmp_path):
    db = tmp_path / "app.sqlite"
    old = historical_payload(context_id="macro-context-2026-07-16-history", as_of="2026-07-16")
    seed_history(db, old)
    with closing(sqlite3.connect(db)) as conn, conn:
        before = conn.execute("select payload from market_context_assessment").fetchone()[0]
    service = AssessmentService(db)
    assert service.head_id() == old["context_id"]
    new = document()
    service.publish(new, expected_head=old["context_id"])
    service.publish(new, expected_head=old["context_id"])
    with closing(sqlite3.connect(db)) as conn, conn:
        assert (
            conn.execute(
                "select payload from market_context_assessment where context_id=?",
                (old["context_id"],),
            ).fetchone()[0]
            == before
        )
        assert (
            conn.execute(
                "select supersedes_id from market_context_assessment where context_id=?",
                (new.context_id,),
            ).fetchone()[0]
            == old["context_id"]
        )
        assert conn.execute("select count(*) from market_context_assessment").fetchone()[0] == 2
    assert service.head_id() == new.context_id
    with pytest.raises(AssessmentConflictError):
        service.publish(document(summary="別の本文"), expected_head=old["context_id"])
    with pytest.raises(AssessmentConflictError):
        service.publish(
            document(context_id="market-context-assessment-2026-07-17-other"), expected_head=None
        )


def test_known_at_normal_historical_and_weekend_selection():
    friday = stored(
        document(
            context_id="market-context-assessment-2026-07-17-friday",
            published_at="2026-07-17T18:00:00+00:00",
        )
    )
    weekend = stored(document())
    records = [friday, weekend]
    assert (
        select_assessment(
            records, as_of=date(2026, 7, 17), known_at=datetime(2026, 7, 18, 13, tzinfo=UTC)
        )
        == weekend
    )
    assert (
        select_assessment(
            records, as_of=date(2026, 7, 17), known_at=datetime(2026, 7, 17, 19, tzinfo=UTC)
        )
        == friday
    )
    assert (
        select_assessment(
            records, as_of=date(2026, 7, 17), known_at=datetime(2026, 7, 17, 17, tzinfo=UTC)
        )
        is None
    )
    assert (
        select_assessment(
            records, as_of=date(2026, 7, 16), known_at=datetime(2026, 7, 20, tzinfo=UTC)
        )
        is None
    )


def test_selector_orders_real_instants_not_iso_strings():
    early = stored(
        document(
            context_id="market-context-assessment-2026-07-17-early",
            published_at="2026-07-17T16:00:00+09:00",
        )
    )
    late = stored(
        document(
            context_id="market-context-assessment-2026-07-17-late",
            published_at="2026-07-17T08:00:00+00:00",
        )
    )
    assert (
        select_assessment(
            [early, late], as_of=date(2026, 7, 17), known_at=datetime(2026, 7, 17, 9, tzinfo=UTC)
        )
        == late
    )
    with pytest.raises(ValueError, match="timezone"):
        select_assessment(
            [], as_of=date(2026, 7, 17), known_at=datetime.fromisoformat("2026-07-17T00:00:00")
        )


def test_history_id_access_has_no_analytical_cutoff(tmp_path):
    db = tmp_path / "app.sqlite"
    payload = {
        "context_id": "macro-context-2026-07-17-v3",
        "schema_version": 3,
        "kind": "macro-context",
        "as_of": "2026-07-17",
        "published_at": "2026-07-20T12:00:00+09:00",
        "summary": "当時の本文",
        "sections": [{"summary": "保存済み節"}],
        "inputs": {},
    }
    seed_history(db, payload)
    assert assessment_by_id(db, context_id=payload["context_id"]).document == payload
    assert len(assessment_history(db)) == 1
    assert (
        latest_assessment(db, as_of=date(2026, 7, 17), known_at=datetime(2026, 7, 18, tzinfo=UTC))
        is None
    )


def test_input_time_and_evidence_resolution():
    payload = document().payload()
    payload["inputs"] = [
        {
            "input_id": "reading",
            "reference": "market_context.reading/fixed",
            "layer": "L2",
            "available_at": "2026-07-17T07:00:00+00:00",
        }
    ]
    with pytest.raises(ValueError, match="unavailable"):
        MarketContextAssessment.model_validate(payload)
    payload["inputs"][0]["available_at"] = "2026-07-17T05:00:00+00:00"
    payload["counter_evidence"] = [{"summary": "相反する材料", "source_ids": ["reading"]}]
    payload["uncertainties"] = [{"summary": "観測が限られる", "source_ids": ["reading"]}]
    projection = assessment_projection(
        stored(MarketContextAssessment.model_validate(payload)), as_of=date(2026, 7, 17)
    )
    assert projection["counter_evidence"] == payload["counter_evidence"]
    assert projection["uncertainties"] == payload["uncertainties"]
    payload["changes"] = [{"summary": "変化", "source_ids": ["unknown"]}]
    with pytest.raises(ValueError, match="unknown input"):
        MarketContextAssessment.model_validate(payload)


def test_missing_and_stale_are_distinct_and_do_not_create_a_store(tmp_path):
    db = tmp_path / "absent.sqlite"
    assert (
        latest_assessment(db, as_of=date(2026, 7, 17), known_at=datetime(2026, 7, 18, tzinfo=UTC))
        is None
    )
    assert not db.exists()
    assert assessment_projection(None, as_of=date(2026, 7, 17))["status"] == "missing"
    assert assessment_projection(stored(document()), as_of=date(2026, 10, 1))["status"] == "stale"


def test_cli_new_writer_check_publish_and_history(tmp_path, capsys):
    db = tmp_path / "app.sqlite"
    draft = tmp_path / "draft.json"
    draft.write_text(json.dumps(document().payload()))
    prefix = ["--db", str(db)]
    assert main([*prefix, "publish", str(draft), "--check"]) == 0
    assert not db.exists()
    assert main([*prefix, "publish", str(draft)]) == 0
    assert main([*prefix, "show", "--context-id", document().context_id]) == 0
    capsys.readouterr()
    assert (
        main([*prefix, "latest", "--asof", "2026-07-17", "--known-at", "2026-07-17T19:00:00+00:00"])
        == 0
    )
    assert capsys.readouterr().out.strip() == "null"


@pytest.mark.parametrize("version", [5, 6])
def test_compact_meaning_survives_history_projection(version):
    payload = document().payload()
    if version == 5:
        payload.update(schema_version=5, context_id="macro-context-2026-07-17-saved")
    record = StoredAssessment.from_payload(payload)
    projected = assessment_projection(record, as_of=date(2026, 7, 19))
    assert projected["status"] == "current"
    assert projected["uncertainties"] == payload["uncertainties"]
    assert record.document == payload
    assert (
        select_assessment(
            [record],
            as_of=date(2026, 7, 17),
            known_at=datetime.fromisoformat(payload["published_at"]),
        )
        == record
    )


def test_historical_synthesis_keeps_the_claim_counter_evidence_and_interactions():
    original = historical_payload()
    projection = assessment_projection(
        StoredAssessment.from_payload(original), as_of=date(2026, 7, 19)
    )
    synthesis = projection["synthesis"]
    assert synthesis["interactions"] == original["synthesis"]["interactions"]
    assert {force["force_id"] for force in synthesis["dominant_forces"]} == {
        "rates-repricing",
        "fx-extreme",
    }
    for quoted, source in zip(
        synthesis["dominant_forces"], original["synthesis"]["dominant_forces"], strict=True
    ):
        for field in ("summary", "transmission", "confidence", "counter_evidence", "source_ids"):
            assert quoted[field] == source[field]
        assert any(
            item["summary"] == source["counter_evidence"] for item in projection["counter_evidence"]
        )


@pytest.mark.parametrize("unused", ["older", "future_publication", "future_asof", "unsupported"])
def test_latest_does_not_decode_unselected_bodies(tmp_path, unused):
    db = tmp_path / "app.sqlite"
    chosen = document()
    AssessmentService(db).publish(chosen, expected_head=AssessmentService(db).head_id())
    other = chosen.payload()
    other["context_id"] = "market-context-assessment-2026-07-17-other"
    other["published_at"] = "2026-07-17T18:00:00+00:00"
    if unused == "future_publication":
        other["published_at"] = "2026-07-18T13:00:00+00:00"
    elif unused == "future_asof":
        other["as_of"] = "2026-07-19"
    elif unused == "unsupported":
        other["schema_version"] = 99
    seed_history(db, other)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(
            "UPDATE market_context_assessment SET payload='[]' WHERE context_id=?",
            (other["context_id"],),
        )
    result = latest_assessment(
        db, as_of=date(2026, 7, 17), known_at=datetime(2026, 7, 18, 12, tzinfo=UTC)
    )
    assert result.document == chosen.payload()
    with pytest.raises(ValueError, match="assessment payload must be an object"):
        assessment_by_id(db, context_id=other["context_id"])


def test_latest_broken_selected_body_does_not_fall_back(tmp_path):
    db = tmp_path / "app.sqlite"
    older = document(
        context_id="market-context-assessment-2026-07-17-older",
        published_at="2026-07-17T18:00:00+00:00",
    )
    AssessmentService(db).publish(older, expected_head=None)
    chosen = document()
    AssessmentService(db).publish(chosen, expected_head=AssessmentService(db).head_id())
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(
            "UPDATE market_context_assessment SET payload='[]' WHERE context_id=?",
            (chosen.context_id,),
        )
    with pytest.raises(ValueError, match="assessment payload must be an object"):
        latest_assessment(
            db, as_of=date(2026, 7, 17), known_at=datetime(2026, 7, 18, 12, tzinfo=UTC)
        )


def test_latest_orders_offset_and_microsecond_publications_like_pure_selector(tmp_path):
    db = tmp_path / "app.sqlite"
    documents = [
        document(
            context_id="market-context-assessment-2026-07-17-offset",
            published_at="2026-07-17T16:00:00+09:00",
        ),
        document(
            context_id="market-context-assessment-2026-07-17-first",
            published_at="2026-07-17T08:00:00.000001+00:00",
        ),
        document(
            context_id="market-context-assessment-2026-07-17-second",
            published_at="2026-07-17T08:00:00.000002+00:00",
        ),
    ]
    for item in documents:
        AssessmentService(db).publish(item, expected_head=AssessmentService(db).head_id())
    for cutoff in ("2026-07-17T08:00:00.000001+00:00", "2026-07-17T08:00:00.000002+00:00"):
        known_at = datetime.fromisoformat(cutoff)
        expected = select_assessment(
            [stored(item) for item in documents], as_of=date(2026, 7, 17), known_at=known_at
        )
        assert latest_assessment(db, as_of=date(2026, 7, 17), known_at=known_at) == expected
