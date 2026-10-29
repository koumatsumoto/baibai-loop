"""一回用cutoverのcopy境界と旧保存identityの保持。実storeは使わない。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime

import pytest
import tools.cutover_application_v25_to_v26 as tool

from baibai_engine.investment_judgment.company_research.document import (
    ResearchDocument,
    ResearchDraft,
)
from baibai_engine.investment_judgment.company_research.store import (
    ResearchStoreService,
    require_research_ref,
)
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_engine.storage.sqlite.application.schema import (
    COMPANY_RESEARCH_SCHEMA_SQL,
    SCHEMA_SQL,
)
from baibai_engine.storage.sqlite.application.write import initialize_database

_PAYLOAD = '{ "schema_version" : 1, "原本" : "保持", "n" : 1.00 }'
_AT = "2026-10-28T16:00:00+09:00"
_AS_OF = "2026-10-28"


def _schema25(connection):
    # Version 26 adds only this DDL. No migration-era parser reserializes old data.
    connection.executescript(SCHEMA_SQL.removesuffix(COMPANY_RESEARCH_SCHEMA_SQL))
    connection.execute("PRAGMA user_version = 25")


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
    connection.commit()


def _legacy_rows(connection):
    tables = connection.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' AND name <> 'company_research' ORDER BY name"
    ).fetchall()
    return {
        name: connection.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
        for (name,) in tables
    }


def test_cutover_copies_committed_wal_preserves_old_rows_and_accepts_new_research(tmp_path):
    source_path = tmp_path / "source.sqlite"
    destination_path = tmp_path / "destination.sqlite"
    with closing(sqlite3.connect(source_path)) as source:
        source.execute("PRAGMA foreign_keys = ON")
        source.execute("PRAGMA journal_mode = WAL")
        source.execute("PRAGMA wal_autocheckpoint = 0")
        _schema25(source)
        source.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        _seed_legacy_rows(source)
        wal_path = tmp_path / "source.sqlite-wal"
        assert wal_path.stat().st_size > 0
        before_files = (source_path.read_bytes(), wal_path.read_bytes())
        before_rows = _legacy_rows(source)
        before_schema = source.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name"
        ).fetchall()

        assert tool.cutover(source_path, destination_path) == destination_path

        assert (source_path.read_bytes(), wal_path.read_bytes()) == before_files
        assert source.execute("PRAGMA user_version").fetchone()[0] == 25
        assert (
            source.execute(
                "SELECT name FROM sqlite_schema WHERE name='company_research'"
            ).fetchall()
            == []
        )
        with closing(sqlite3.connect(destination_path)) as destination:
            assert _legacy_rows(destination) == before_rows
            assert (
                destination.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_schema "
                    "WHERE tbl_name <> 'company_research' ORDER BY type, name"
                ).fetchall()
                == before_schema
            )
            assert destination.execute("SELECT COUNT(*) FROM company_research").fetchone()[0] == 0
            assert destination.execute("PRAGMA user_version").fetchone()[0] == 26
            assert destination.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert destination.execute("PRAGMA foreign_key_check").fetchall() == []

    assert initialize_database(destination_path) == 26
    with pytest.raises(RuntimeError, match="explicit semantic cutover"):
        initialize_database(source_path)
    with pytest.raises(RuntimeError, match="obsolete application database schema"):
        connect_read_only(source_path)
    record = ResearchStoreService(
        destination_path, clock=lambda: datetime(2026, 10, 29, 8, tzinfo=UTC)
    ).save(
        ResearchDraft(
            research_id="research-one",
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


@pytest.mark.parametrize("version", [0, 24, 26])
def test_cutover_refuses_wrong_source_version_and_reapplication(tmp_path, version):
    source_path, destination_path = tmp_path / "source.sqlite", tmp_path / "destination.sqlite"
    with closing(sqlite3.connect(source_path)) as source:
        source.execute(f"PRAGMA user_version = {version}")
    before = source_path.read_bytes()
    with pytest.raises(ValueError, match="source must have user_version=25"):
        tool.cutover(source_path, destination_path)
    assert not destination_path.exists()
    assert source_path.read_bytes() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == ["source.sqlite"]


def test_cutover_refuses_same_source_and_existing_destination(tmp_path):
    source_path, destination_path = tmp_path / "source.sqlite", tmp_path / "destination.sqlite"
    with closing(sqlite3.connect(source_path)) as source:
        _schema25(source)
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
        _schema25(source)
        _seed_legacy_rows(source)
    before = source_path.read_bytes()
    monkeypatch.setattr(
        tool, "COMPANY_RESEARCH_SCHEMA_SQL", COMPANY_RESEARCH_SCHEMA_SQL + "INVALID;"
    )
    with pytest.raises(sqlite3.OperationalError):
        tool.cutover(source_path, destination_path)
    assert source_path.read_bytes() == before
    assert not destination_path.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["source.sqlite"]
