"""Source-owned annual XBRL acquisition scope, independent of extracted facts.

The scope is operational state, not a provider receipt or a successful extraction.
A selected family remains requested even when its ZIP is unavailable. Stale copies
can add scope, never remove requested identities or narrow its global start date.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class FactsScope:
    since: date | None = None
    doc_ids: frozenset[str] = frozenset()
    legacy_origins: frozenset[str] = frozenset()


def read_scope(connection: sqlite3.Connection, *, schema: str = "main") -> FactsScope:
    if schema not in {"main", "source"}:
        raise ValueError("unsupported scope schema")
    query = (
        "SELECT enabled_since, doc_ids_json, legacy_origins_json "
        "FROM main.edinet_facts_scope WHERE singleton=1"
        if schema == "main"
        else "SELECT enabled_since, doc_ids_json, legacy_origins_json "
        "FROM source.edinet_facts_scope WHERE singleton=1"
    )
    row = connection.execute(query).fetchone()
    if row is None:
        return FactsScope()
    since = date.fromisoformat(row[0]) if row[0] is not None else None
    if since is not None and since.isoformat() != row[0]:
        raise ValueError("invalid EDINET acquisition since")
    ids, origins = json.loads(row[1]), json.loads(row[2])
    if not isinstance(ids, list) or any(not isinstance(key, str) or not key for key in ids):
        raise ValueError("invalid EDINET acquisition identities")
    if not isinstance(origins, list) or any(
        not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key) for key in origins
    ):
        raise ValueError("invalid EDINET legacy scope origin")
    return FactsScope(since, frozenset(ids), frozenset(origins))


def merge_scope(connection: sqlite3.Connection, incoming: FactsScope) -> FactsScope:
    """Join requested scopes in the caller's transaction; NULL means not enabled."""
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    current = read_scope(connection)
    starts = [value for value in (current.since, incoming.since) if value is not None]
    merged = FactsScope(
        min(starts) if starts else None,
        current.doc_ids | incoming.doc_ids,
        current.legacy_origins | incoming.legacy_origins,
    )
    if merged != current:
        connection.execute(
            "INSERT INTO edinet_facts_scope VALUES (1,?,?,?) "
            "ON CONFLICT(singleton) DO UPDATE SET enabled_since=excluded.enabled_since, "
            "doc_ids_json=excluded.doc_ids_json, legacy_origins_json=excluded.legacy_origins_json",
            (
                merged.since.isoformat() if merged.since else None,
                json.dumps(sorted(merged.doc_ids)),
                json.dumps(sorted(merged.legacy_origins)),
            ),
        )
    return merged
