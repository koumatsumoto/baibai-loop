"""Explicit original-page acquisition; commits receipts before decoding or paging."""

from __future__ import annotations

import gzip
import json
import sqlite3
import uuid
from collections.abc import Mapping

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsAcquisitionError,
    JQuantsSource,
)
from baibai_engine.data_collection.providers.jquants.http import JQuantsHTTP


def acquire_request(
    conn: sqlite3.Connection,
    client: JQuantsHTTP,
    source: JQuantsSource,
    params: Mapping[str, str],
) -> str:
    """Receive one explicit query; no normalized table is read or written here."""
    if conn.in_transaction:
        raise ValueError("acquisition requires a connection without a pending transaction")
    request_id = uuid.uuid4().hex
    try:
        for index, page in enumerate(client.pages(source, params)):
            with conn:
                conn.execute(
                    "INSERT INTO jquants_source_pages VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        request_id,
                        index,
                        page.source,
                        json.dumps(dict(page.query), sort_keys=True, separators=(",", ":")),
                        page.received_at.isoformat(timespec="microseconds"),
                        page.received_at.date().isoformat(),
                        gzip.compress(page.body, mtime=0),
                    ),
                )
    except JQuantsAcquisitionError as error:
        error.request_id = request_id
        raise
    return request_id
