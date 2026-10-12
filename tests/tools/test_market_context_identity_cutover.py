from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from hashlib import sha256

import pytest
from pydantic import ValidationError
from tests.engine.market_context.assessment.test_assessment import document
from tests.engine.test_capital_allocation import _payload as allocation_payload
from tests.helpers.research_triage import research_triage_payload
from tools.ops.migrate_application_20_to_21 import TABLE_NAMES, migrate_copy

from baibai_engine.market_context.assessment.models import StoredAssessment
from baibai_engine.market_context.assessment.projection import assessment_projection
from baibai_engine.market_context.assessment.query import assessment_by_id, select_assessment
from baibai_engine.market_context.indicators.db import open_connection
from baibai_engine.research.capital_allocation import (
    CapitalAllocationAssessment,
    StoredCapitalAllocationAssessment,
    capital_allocation_draft_sha256,
)
from baibai_engine.research.triage.contracts import ResearchTriage, StoredResearchTriage
from baibai_engine.research.triage.query import (
    current_research_triage,
    latest_research_triage_payload,
    research_triage_payload_hash,
)
from baibai_engine.storage.application.json import canonical_json
from baibai_engine.storage.application.write import initialize_database
from baibai_web.readmodel.assessment import build_assessment_publication


def legacy_assessment():
    payload = document().payload()
    payload.update(schema_version=5, context_id="macro-context-2026-07-17-saved")
    return payload


def legacy_triage():
    payload = research_triage_payload(schema_version=3, triage_contract_id="research-triage-v3")
    payload["macro_context_id"] = "macro-context-2026-07-17-saved"
    payload.pop("market_context_assessment_id")
    return payload


def test_copy_renames_schema_without_reissuing_any_saved_fact(tmp_path):
    source, output = tmp_path / "old.sqlite", tmp_path / "new.sqlite"
    initialize_database(source)
    assessment = legacy_assessment()
    triage = legacy_triage()
    raw = json.dumps(assessment, ensure_ascii=False, indent=3)
    triage_raw = json.dumps(triage, ensure_ascii=False)
    with sqlite3.connect(source) as con:
        con.execute(
            "INSERT INTO market_context_assessment VALUES(?,?,?,?,?,?)",
            (
                assessment["context_id"],
                5,
                assessment["as_of"],
                assessment["published_at"],
                None,
                raw,
            ),
        )
        con.execute(
            "INSERT INTO market_context_assessment_head VALUES(1,?)", (assessment["context_id"],)
        )
        con.execute(
            "INSERT INTO research_triage(research_triage_id,review_set_id,run_revision_id,as_of,published_at,payload) VALUES(?,?,?,?,?,?)",
            (
                triage["research_triage_id"],
                triage["review_set_id"],
                triage["run_revision_id"],
                triage["as_of"],
                triage["published_at"],
                triage_raw,
            ),
        )
        for old, new in reversed(tuple(TABLE_NAMES.items())):
            con.execute(f'ALTER TABLE "{new}" RENAME TO "{old}"')
        con.execute("DROP INDEX market_context_assessment_asof_idx")
        con.execute(
            "CREATE INDEX macro_context_asof_idx ON macro_context(as_of,published_at,context_id)"
        )
        con.execute("PRAGMA user_version=20")
    before = source.read_bytes()
    report = migrate_copy(source, output)
    assert source.read_bytes() == before
    assert len(report) == 13
    with sqlite3.connect(output) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 21
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not con.execute("PRAGMA foreign_key_check").fetchall()
        assert con.execute("SELECT payload FROM market_context_assessment").fetchone()[0] == raw
        assert con.execute("SELECT payload FROM research_triage").fetchone()[0] == triage_raw
        assert (
            con.execute("SELECT context_id FROM market_context_assessment_head").fetchone()[0]
            == assessment["context_id"]
        )
    assert assessment_by_id(output, context_id=assessment["context_id"]).document == assessment
    assert latest_research_triage_payload(output) == triage
    stored = current_research_triage(output, triage["research_triage_id"])
    assert stored is not None
    assert stored.market_context_assessment_id == assessment["context_id"]
    assert (
        research_triage_payload_hash(stored) == sha256(canonical_json(triage).encode()).hexdigest()
    )
    with pytest.raises(FileExistsError):
        migrate_copy(source, output)


@pytest.mark.parametrize("version", [5, 6])
def test_compact_meaning_survives_history_projection_and_web(version):
    payload = legacy_assessment() if version == 5 else document().payload()
    record = StoredAssessment.from_payload(payload)
    view = build_assessment_publication(record.document, as_of=date(2026, 7, 19), series_names={})
    assert view.assessment is not None
    assert view.assessment.known_at == document().known_at
    assert view.core == []
    assert view.historical_payload is None
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


def test_legacy_triage_and_allocation_are_reads_not_second_writers(tmp_path):
    triage = legacy_triage()
    saved = StoredResearchTriage.from_payload(triage)
    assert saved.schema_version == 3
    assert saved.publication_payload() == triage
    assert saved.admissible_research_tickers() == ("2331",)
    with pytest.raises(ValidationError):
        ResearchTriage.model_validate(triage)
    current = CapitalAllocationAssessment.model_validate(allocation_payload()).payload()
    old = {**current, "schema_version": 1, "macro_context_id": "macro-context-2026-07-17-saved"}
    old.pop("market_context_assessment_id")
    allocation = StoredCapitalAllocationAssessment.from_payload(old)
    assert allocation.schema_version == 1
    assert allocation.market_context_assessment_id == old["macro_context_id"]
    assert allocation.payload() == old
    reviewed = {k: v for k, v in old.items() if k not in {"review", "published_at"}}
    assert (
        capital_allocation_draft_sha256(allocation)
        == sha256(canonical_json(reviewed).encode()).hexdigest()
    )
    with pytest.raises(ValidationError):
        CapitalAllocationAssessment.model_validate(old)

    from tests.helpers.research_triage import published_review_set

    from baibai_engine.research.capital_allocation_service import CapitalAllocationAssessmentService
    from baibai_engine.research.triage.service import ResearchTriageService
    from baibai_engine.screening.discovery.contracts import PublishedReviewSet

    db = tmp_path / "not-created.sqlite"
    with pytest.raises(ValueError, match="read-only"):
        ResearchTriageService(db).publish(
            saved, review_set=PublishedReviewSet.model_validate(published_review_set())
        )
    with pytest.raises(ValueError, match="read-only"):
        CapitalAllocationAssessmentService(db).publish(allocation)
    assert not db.exists()


def test_missing_canonical_indicator_path_cannot_create_an_empty_store(tmp_path, monkeypatch):
    from baibai_engine.foundation.repository_layout import INDICATORS_DB_PATH

    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError, match="explicit provisioning"):
        open_connection()
    assert not INDICATORS_DB_PATH.exists()


def test_wrong_schema_copy_is_refused_before_creating_output(tmp_path):
    source, output = tmp_path / "source.sqlite", tmp_path / "output.sqlite"
    initialize_database(source)
    with pytest.raises(ValueError, match="schema 20"):
        migrate_copy(source, output)
    assert not output.exists()
