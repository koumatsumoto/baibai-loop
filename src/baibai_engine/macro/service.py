"""Macro-context publication and revision queries."""

from __future__ import annotations

from contextlib import closing
from datetime import date
from pathlib import Path

from baibai_engine.appdb.json import canonical_json
from baibai_engine.appdb.write import connect_rw, initialize_database

from .models import MacroContextDocument


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
                row = connection.execute(
                    "SELECT context_id FROM macro_context_head WHERE singleton = 1"
                ).fetchone()
                current = None if row is None else str(row[0])
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
                        context_id, as_of, valid_until, published_at, supersedes_id, payload
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document.context_id,
                        document.as_of.isoformat(),
                        document.valid_until.isoformat(),
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
            row = connection.execute(
                "SELECT context_id FROM macro_context_head WHERE singleton = 1"
            ).fetchone()
        return None if row is None else str(row[0])

    def get(self, context_id: str) -> MacroContextDocument:
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            row = connection.execute(
                "SELECT payload FROM macro_context WHERE context_id = ?", (context_id,)
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
                WHERE as_of <= ?
                ORDER BY as_of DESC, published_at DESC, context_id DESC
                LIMIT 1
                """,
                (as_of.isoformat(),),
            ).fetchone()
        return None if row is None else MacroContextDocument.model_validate_json(str(row[0]))

    def get_for(self, context_id: str, *, as_of: date) -> MacroContextDocument:
        document = self.get(context_id)
        if document.as_of > as_of:
            raise MacroContextConflictError(
                f"future macro context is not eligible: {context_id} as_of={document.as_of}"
            )
        return document


__all__ = [
    "MacroContextConflictError",
    "MacroContextNotFoundError",
    "MacroContextService",
]
