"""一回用cutoverのcopy境界と旧保存identityの保持。実storeは使わない。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime

import pytest
import tools.cutover_application_v26_to_v27 as tool
from tests.helpers.assessment_v1 import assessment_id, pair_payload

from baibai_engine.investment_judgment.company_research.document import (
    ResearchDocument,
    ResearchDraft,
    ResearchRecord,
    ResearchRef,
    research_sha256,
)
from baibai_engine.investment_judgment.company_research.store import (
    ResearchStoreService,
    require_research_ref,
)
from baibai_engine.investment_judgment.judgment_retrospective.document import RetrospectiveDraft
from baibai_engine.investment_judgment.judgment_retrospective.store import (
    RetrospectiveStoreService,
    load_retrospective,
)
from baibai_engine.investment_judgment.stock_assessment.document import AssessmentRef
from baibai_engine.investment_judgment.stock_assessment.store import publish_reviewed_assessment
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_engine.storage.sqlite.application.schema import (
    JUDGMENT_RETROSPECTIVE_SCHEMA_SQL,
    SCHEMA_SQL,
)
from baibai_engine.storage.sqlite.application.write import initialize_database

_PAYLOAD = '{ "schema_version" : 1, "原本" : "保持", "n" : 1.00 }'
_AT = "2026-10-28T16:00:00+09:00"
_AS_OF = "2026-10-28"


def _schema26(connection):
    # Version 27 adds only this explicit suffix; Research already belongs to 26.
    assert SCHEMA_SQL.endswith(JUDGMENT_RETROSPECTIVE_SCHEMA_SQL)
    connection.executescript(SCHEMA_SQL.removesuffix(JUDGMENT_RETROSPECTIVE_SCHEMA_SQL))
    connection.execute("PRAGMA user_version = 26")


def _seed_legacy_rows(connection):
    rows = {
        "task": ("task-one", "open", "other", "1234", _AS_OF, None, _AT, None, _PAYLOAD),
        "market_context_assessment": ("context-one", 1, _AS_OF, _AT, None, _PAYLOAD),
        "market_context_assessment_head": (1, "context-one"),
        "stock_assessment": (
            "assessment-one",
            "1234",
            _AS_OF,
            "supported",
            _AT,
            None,
            _PAYLOAD,
            "1" * 64,
        ),
        "stock_assessment_review": ("review-one", "assessment-one", _AT, _PAYLOAD),
        "investment_proposal": (
            "proposal-one",
            _AS_OF,
            _AT,
            _AT,
            "specified_stocks",
            _PAYLOAD,
            "2" * 64,
        ),
        "investment_proposal_item": ("proposal-one", 0, "1234", "buy", "assessment-one"),
        "position_decision_reference": (
            "decision-one",
            "1234",
            "buy",
            "proposal",
            "proposal-one",
            "source-one",
            "3" * 64,
        ),
        "operation_session": (
            "operation-one",
            "capital-allocation",
            "completed",
            _AS_OF,
            None,
            _AT,
            _AT,
            _PAYLOAD,
        ),
        "ledger_event": (1, "event-one", _AT, 0, "execution", "1234", "decision-one", _PAYLOAD),
        "ledger_meta": (1, 1, "repository_only", _AS_OF, None, None, _PAYLOAD),
        "portfolio_outcome": ("outcome-one", "1y", "2025-10-28", _AS_OF, "unresolved", _PAYLOAD),
    }
    for table, values in rows.items():
        placeholders = ",".join("?" for _ in values)
        connection.execute(f'INSERT INTO "{table}" VALUES ({placeholders})', values)
    research_records = []
    for day in (27, 28):
        document = ResearchDocument(
            ticker="1234",
            as_of=date(2026, 10, day),
            investigated_scope="旧Researchの原本・未確認点を保持",
            unresolved_questions=("主要事業の収益構造は変わったか",),
        )
        prior = research_records[-1].reference if research_records else None
        record = ResearchRecord(
            reference=ResearchRef(
                research_id=f"research-{day}", payload_sha256=research_sha256(document)
            ),
            recorded_at=datetime.fromisoformat(f"2026-10-{day}T16:00:00+09:00"),
            prior_research_ref=prior,
            document=document,
        )
        # Noncanonical whitespace proves cutover did not parse and rewrite the payload.
        payload = json.dumps(document.model_dump(mode="json"), ensure_ascii=False, indent=2)
        connection.execute(
            "INSERT INTO company_research VALUES (?,?,?,?,?,?,?)",
            (
                record.reference.research_id,
                document.ticker,
                document.as_of.isoformat(),
                record.recorded_at.isoformat(),
                None if prior is None else prior.research_id,
                payload,
                record.reference.payload_sha256,
            ),
        )
        research_records.append(record)
    document, review = pair_payload(ticker="5678", as_of=_AS_OF)
    pair = publish_reviewed_assessment(
        connection,
        assessment_id(),
        document,
        review,
        published_at=datetime.fromisoformat("2026-10-28T16:06:00+09:00"),
    )
    connection.commit()
    return research_records, AssessmentRef(
        assessment_id=pair.assessment_id,
        core_sha256=pair.core_sha256,
        review_id=pair.review.review_id,
    )


def _legacy_rows(connection):
    tables = connection.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' AND name <> 'judgment_retrospective' ORDER BY name"
    ).fetchall()
    return {
        name: connection.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
        for (name,) in tables
    }


def test_cutover_preserves_wal_and_research_chain_then_accepts_retrospective(tmp_path):
    source_path = tmp_path / "source.sqlite"
    destination_path = tmp_path / "destination.sqlite"
    with closing(sqlite3.connect(source_path)) as source:
        source.execute("PRAGMA foreign_keys = ON")
        source.execute("PRAGMA journal_mode = WAL")
        source.execute("PRAGMA wal_autocheckpoint = 0")
        _schema26(source)
        source.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        research_records, origin = _seed_legacy_rows(source)
        wal_path = tmp_path / "source.sqlite-wal"
        assert wal_path.stat().st_size > 0
        before_files = (source_path.read_bytes(), wal_path.read_bytes())
        before_rows = _legacy_rows(source)
        before_schema = source.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name"
        ).fetchall()

        assert tool.cutover(source_path, destination_path) == destination_path

        assert (source_path.read_bytes(), wal_path.read_bytes()) == before_files
        assert source.execute("PRAGMA user_version").fetchone()[0] == 26
        assert (
            source.execute(
                "SELECT name FROM sqlite_schema WHERE name='judgment_retrospective'"
            ).fetchall()
            == []
        )
        with closing(sqlite3.connect(destination_path)) as destination:
            assert _legacy_rows(destination) == before_rows
            assert (
                destination.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_schema "
                    "WHERE tbl_name <> 'judgment_retrospective' ORDER BY type, name"
                ).fetchall()
                == before_schema
            )
            assert (
                destination.execute("SELECT COUNT(*) FROM judgment_retrospective").fetchone()[0]
                == 0
            )
            assert destination.execute("PRAGMA user_version").fetchone()[0] == 27
            assert destination.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert destination.execute("PRAGMA foreign_key_check").fetchall() == []

    assert initialize_database(destination_path) == 27
    with pytest.raises(RuntimeError, match="explicit semantic cutover"):
        initialize_database(source_path)
    with pytest.raises(RuntimeError, match="obsolete application database schema"):
        connect_read_only(source_path)
    with closing(connect_read_only(destination_path)) as destination:
        for old_record in research_records:
            assert require_research_ref(destination, old_record.reference) == old_record
    record = ResearchStoreService(
        destination_path, clock=lambda: datetime(2026, 10, 29, 8, tzinfo=UTC)
    ).save(
        ResearchDraft(
            research_id="research-after-cutover",
            prior_research_ref=research_records[-1].reference,
            document=ResearchDocument(
                ticker="1234",
                as_of=date(2026, 10, 29),
                investigated_scope="決算説明資料の未確認点",
                unresolved_questions=("主要事業の収益構造は変わったか",),
            ),
        )
    )
    with closing(connect_read_only(destination_path)) as destination:
        assert require_research_ref(destination, record.reference) == record
    retrospective = RetrospectiveStoreService(
        destination_path, clock=lambda: datetime(2026, 10, 29, 8, tzinfo=UTC)
    ).save(
        RetrospectiveDraft.model_validate(
            {
                "retrospective_id": "retrospective-after-cutover",
                "document": {
                    "ticker": "5678",
                    "as_of": "2026-10-29",
                    "milestone": "決算説明会後",
                    "origin": origin.model_dump(mode="json"),
                    "comparisons": [
                        {
                            "original_claim": "年度末まで借換条件を維持できる",
                            "observed": "条件を確認できる後日開示はまだない",
                            "result": "unresolved",
                            "reason": "次回の金融機関合意の開示を待つ",
                            "citations": [],
                        }
                    ],
                },
            }
        )
    )
    with closing(connect_read_only(destination_path)) as destination:
        assert load_retrospective(destination, "retrospective-after-cutover") == retrospective
        for old_record in research_records:
            assert require_research_ref(destination, old_record.reference) == old_record


@pytest.mark.parametrize("version", [0, 25, 27])
def test_cutover_refuses_wrong_source_version_and_reapplication(tmp_path, version):
    source_path, destination_path = tmp_path / "source.sqlite", tmp_path / "destination.sqlite"
    with closing(sqlite3.connect(source_path)) as source:
        source.execute(f"PRAGMA user_version = {version}")
    before = source_path.read_bytes()
    existing_paths = set(tmp_path.iterdir())
    with pytest.raises(ValueError, match="source must have user_version=26"):
        tool.cutover(source_path, destination_path)
    assert not destination_path.exists()
    assert source_path.read_bytes() == before
    assert set(tmp_path.iterdir()) == existing_paths


def test_cutover_refuses_same_source_and_existing_destination(tmp_path):
    source_path, destination_path = tmp_path / "source.sqlite", tmp_path / "destination.sqlite"
    with closing(sqlite3.connect(source_path)) as source:
        _schema26(source)
    before = source_path.read_bytes()
    with pytest.raises(ValueError, match="must be different paths"):
        tool.cutover(source_path, source_path)
    destination_path.write_bytes(b"existing destination must not be replaced")
    with pytest.raises(FileExistsError, match="destination already exists"):
        tool.cutover(source_path, destination_path)
    assert source_path.read_bytes() == before
    assert destination_path.read_bytes() == b"existing destination must not be replaced"


def test_failed_additive_ddl_does_not_publish_or_modify_source(tmp_path, monkeypatch):
    source_path, destination_path = tmp_path / "source.sqlite", tmp_path / "destination.sqlite"
    with closing(sqlite3.connect(source_path)) as source:
        _schema26(source)
        _seed_legacy_rows(source)
    before = source_path.read_bytes()
    existing_paths = set(tmp_path.iterdir())
    monkeypatch.setattr(
        tool, "JUDGMENT_RETROSPECTIVE_SCHEMA_SQL", JUDGMENT_RETROSPECTIVE_SCHEMA_SQL + "INVALID;"
    )
    with pytest.raises(sqlite3.OperationalError):
        tool.cutover(source_path, destination_path)
    assert source_path.read_bytes() == before
    assert not destination_path.exists()
    assert set(tmp_path.iterdir()) == existing_paths
