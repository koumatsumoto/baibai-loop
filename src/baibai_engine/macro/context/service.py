"""Macro-context publication and revision queries.

Revisions published under an earlier contract stay in the table as an immutable log,
so every read filters on the current ``schema_version``: a report that the current
contract cannot express is not a report the current consumers may read.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

from baibai_engine.appdb.json import canonical_json
from baibai_engine.appdb.write import connect_rw, initialize_database

from .models import MACRO_CONTEXT_SCHEMA_VERSION, MacroContextDocument


class MacroContextConflictError(ValueError):
    pass


class MacroContextNotFoundError(ValueError):
    pass


class MacroContextService:
    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path

    def publish(
        self,
        document: MacroContextDocument,
        *,
        expected_head: str | None,
    ) -> MacroContextDocument:
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                # The head means "the operative revision under the current contract", so
                # the compare-and-swap reads it through the same filter as `head_id`. A
                # head left behind by an earlier contract reads as absent, and the first
                # revision of the new contract starts a fresh chain.
                current = _current_head_id(connection)
                if current != expected_head:
                    raise MacroContextConflictError(
                        "macro context head changed: "
                        f"expected={expected_head!r}, actual={current!r}"
                    )
                if connection.execute(
                    "SELECT 1 FROM macro_context WHERE context_id = ?", (document.context_id,)
                ).fetchone():
                    raise MacroContextConflictError(
                        f"context_id already exists: {document.context_id}"
                    )
                connection.execute(
                    """
                    INSERT INTO macro_context (
                        context_id, schema_version, as_of, published_at, supersedes_id, payload
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document.context_id,
                        document.schema_version,
                        document.as_of.isoformat(),
                        document.published_at.isoformat(),
                        current,
                        canonical_json(document.payload()),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO macro_context_head(singleton, context_id) VALUES (1, ?)
                    ON CONFLICT(singleton) DO UPDATE SET context_id = excluded.context_id
                    """,
                    (document.context_id,),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return document

    def head_id(self) -> str | None:
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            return _current_head_id(connection)

    def get(self, context_id: str) -> MacroContextDocument:
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            row = connection.execute(
                "SELECT payload FROM macro_context WHERE context_id = ? AND schema_version = ?",
                (context_id, MACRO_CONTEXT_SCHEMA_VERSION),
            ).fetchone()
        if row is None:
            raise MacroContextNotFoundError(f"unknown context_id: {context_id}")
        return MacroContextDocument.model_validate_json(str(row[0]))

    def latest_for(self, as_of: date) -> MacroContextDocument | None:
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            row = connection.execute(
                """
                SELECT payload FROM macro_context
                WHERE as_of <= ? AND schema_version = ?
                ORDER BY published_at DESC, as_of DESC, context_id DESC
                LIMIT 1
                """,
                (as_of.isoformat(), MACRO_CONTEXT_SCHEMA_VERSION),
            ).fetchone()
        return None if row is None else MacroContextDocument.model_validate_json(str(row[0]))

    def get_for(self, context_id: str, *, as_of: date) -> MacroContextDocument:
        document = self.get(context_id)
        if document.as_of > as_of:
            raise MacroContextConflictError(
                f"future macro context is not eligible: {context_id} as_of={document.as_of}"
            )
        return document


def _current_head_id(connection: sqlite3.Connection) -> str | None:
    row = connection.execute(
        """
        SELECT head.context_id FROM macro_context_head AS head
        JOIN macro_context AS context ON context.context_id = head.context_id
        WHERE head.singleton = 1 AND context.schema_version = ?
        """,
        (MACRO_CONTEXT_SCHEMA_VERSION,),
    ).fetchone()
    return None if row is None else str(row[0])


__all__ = [
    "MacroContextConflictError",
    "MacroContextNotFoundError",
    "MacroContextService",
]
