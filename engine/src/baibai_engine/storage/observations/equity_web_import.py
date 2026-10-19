"""Import canonical public HTML originals into the existing market SQLite store."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from typing import cast

from baibai_engine.collection.equity_web.models import EquityWebSourceResponse
from baibai_engine.collection.equity_web.receipts import (
    EquityWebReceiptError,
    insert_response,
    read_responses,
    validate_response,
)
from baibai_engine.storage.observations.schema import validate_current_schema


def import_responses(sqlite_path: Path, responses: Sequence[EquityWebSourceResponse]) -> int:
    """Atomically retain one bounded bundle's canonical IDs in the current store."""
    if not isinstance(cast(object, responses), Sequence) or isinstance(
        cast(object, responses), (str, bytes)
    ):
        raise EquityWebReceiptError("equity web originals must be a sequence")
    ordered = tuple(responses)
    for response in ordered:
        validate_response(response)
    # A supplied iframe can precede its page; FK/identity checks still run inside
    # the same BEGIN IMMEDIATE transaction after stable page-first ordering.
    ordered = tuple(sorted(ordered, key=lambda response: response.role == "iframe"))
    uri = sqlite_path.resolve().as_uri() + "?mode=rw"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            validate_current_schema(conn)
            inserted = 0
            for response in ordered:
                stored = conn.execute(
                    "SELECT receipt_id FROM equity_web_source_responses WHERE receipt_id=?",
                    (response.receipt_id,),
                ).fetchone()
                if stored is not None:
                    if read_responses(conn, (response.receipt_id,))[0] != response:
                        raise EquityWebReceiptError("conflicting immutable equity web receipt")
                    continue
                insert_response(conn, response)
                inserted += 1
            conn.commit()
            return inserted
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise EquityWebReceiptError("conflicting equity web receipt identity") from exc
        except BaseException:
            conn.rollback()
            raise
