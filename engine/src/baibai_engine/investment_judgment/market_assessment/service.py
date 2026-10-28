"""新しい判断だけを発行し、既存headとのCASと保存本文の不変性を守る。"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from baibai_engine.investment_judgment.market_assessment.models import MarketContextAssessment
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.sqlite.application.paths import database_path
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_engine.storage.sqlite.application.write import connect_rw, initialize_database


class AssessmentConflictError(ValueError):
    pass


class AssessmentService:
    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path

    def head_id(self) -> str | None:
        path = database_path(self._db_path)
        if not path.is_file():
            return None
        with closing(connect_read_only(path)) as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] == 0:
                return None
            row = connection.execute(
                "SELECT context_id FROM market_context_assessment_head WHERE singleton=1"
            ).fetchone()
            return None if row is None else str(row[0])

    def publish(
        self, document: MarketContextAssessment, *, expected_head: str | None
    ) -> MarketContextAssessment:
        initialize_database(self._db_path)
        payload = canonical_json(document.payload())
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT context_id FROM market_context_assessment_head WHERE singleton=1"
                ).fetchone()
                current = None if row is None else str(row[0])
                saved = connection.execute(
                    "SELECT payload, supersedes_id FROM market_context_assessment "
                    "WHERE context_id=?",
                    (document.context_id,),
                ).fetchone()
                if saved is not None:
                    if (
                        str(saved[0]) == payload
                        and saved[1] == expected_head
                        and current == document.context_id
                    ):
                        connection.rollback()
                        return document
                    raise AssessmentConflictError(
                        "context_id already has a different publication state"
                    )
                if current != expected_head:
                    raise AssessmentConflictError(
                        f"assessment head changed: expected={expected_head!r}, actual={current!r}"
                    )
                connection.execute(
                    (
                        "INSERT INTO market_context_assessment("
                        "context_id,schema_version,as_of,published_at,"
                        "supersedes_id,payload) VALUES(?,?,?,?,?,?)"
                    ),
                    (
                        document.context_id,
                        document.schema_version,
                        document.as_of.isoformat(),
                        document.published_at.isoformat(),
                        current,
                        payload,
                    ),
                )
                connection.execute(
                    (
                        "INSERT INTO market_context_assessment_head(singleton,context_id) "
                        "VALUES(1,?) "
                        "ON CONFLICT(singleton) DO UPDATE SET context_id=excluded.context_id"
                    ),
                    (document.context_id,),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return document
