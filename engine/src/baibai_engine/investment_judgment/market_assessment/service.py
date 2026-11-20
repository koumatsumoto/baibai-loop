"""新しい判断だけを発行し、既存headとのCASと保存本文の不変性を守る。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from pathlib import Path

from baibai_engine.investment_judgment.market_assessment.models import (
    RECEIPTED_ENVELOPE_VERSION,
    MarketContextAssessment,
    require_aware,
)
from baibai_engine.investment_judgment.market_assessment.query import stored_assessment_from_row
from baibai_engine.primitives.json import canonical_json
from baibai_engine.primitives.time import JST
from baibai_engine.storage.sqlite.application.paths import database_path
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_engine.storage.sqlite.application.write import connect_rw, initialize_database


class AssessmentConflictError(ValueError):
    pass


class AssessmentService:
    def __init__(
        self, db_path: Path | None = None, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._db_path = db_path
        self._clock = clock if clock is not None else lambda: datetime.now(JST)

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
        # Typed copies can bypass nested validation; do not publish an unreadable head.
        document = MarketContextAssessment.model_validate(document.model_dump(mode="python"))
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
                    "SELECT context_id,schema_version,as_of,published_at,payload,supersedes_id "
                    "FROM market_context_assessment "
                    "WHERE context_id=?",
                    (document.context_id,),
                ).fetchone()
                if saved is not None:
                    if (
                        str(saved["payload"]) == payload
                        and saved["supersedes_id"] == expected_head
                        and current == document.context_id
                    ):
                        stored_assessment_from_row(saved)
                        connection.rollback()
                        return document
                    raise AssessmentConflictError(
                        "context_id already has a different publication state"
                    )
                if current != expected_head:
                    raise AssessmentConflictError(
                        f"assessment head changed: expected={expected_head!r}, actual={current!r}"
                    )
                # Sample under the writer lock, only after CAS/retry checks. This is
                # receipt by this transaction, not proof of external visibility at COMMIT.
                # Mixed old-writer rows supply no receipt. Follow their actual CAS
                # parents to the nearest verified receipt; never order by a claim.
                prior_receipt = connection.execute(
                    "WITH RECURSIVE lineage(id,parent,version,stamp) AS ("
                    "SELECT context_id,supersedes_id,schema_version,published_at FROM "
                    "market_context_assessment WHERE context_id=? UNION SELECT "
                    "m.context_id,m.supersedes_id,m.schema_version,m.published_at FROM "
                    "market_context_assessment m JOIN lineage p ON m.context_id=p.parent "
                    "WHERE p.version<>?) SELECT stamp FROM lineage WHERE version=?",
                    (current, RECEIPTED_ENVELOPE_VERSION, RECEIPTED_ENVELOPE_VERSION),
                ).fetchone()
                recorded_at = require_aware(self._clock())
                if document.published_at > recorded_at:
                    raise ValueError("declared market publication follows its actual receipt")
                if prior_receipt is not None and recorded_at <= require_aware(
                    datetime.fromisoformat(str(prior_receipt[0]))
                ):
                    raise AssessmentConflictError("market writer receipt did not advance")

                connection.execute(
                    (
                        "INSERT INTO market_context_assessment("
                        "context_id,schema_version,as_of,published_at,"
                        "supersedes_id,payload) VALUES(?,?,?,?,?,?)"
                    ),
                    (
                        document.context_id,
                        RECEIPTED_ENVELOPE_VERSION,
                        document.as_of.isoformat(),
                        recorded_at.isoformat(),
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
