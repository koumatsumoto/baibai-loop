"""Finite schema49 -> 50 copy: retain source scope without importing L2 results."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date
from pathlib import Path

from baibai_engine.data_collection.providers.edinet.facts_scope import FactsScope, merge_scope
from baibai_engine.data_collection.providers.edinet.storage_contracts import EDINET_FACTS_SCOPE_DDL
from baibai_engine.storage.sqlite.source_cache.schema import (
    _SCHEMA_SQL,
    _expected_schema_shape,
    _index_info,
    _table_info,
    validate_current_schema,
)

SCHEMA49_SQL = _SCHEMA_SQL.replace(EDINET_FACTS_SCOPE_DDL, "")


def validate_schema49(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA user_version").fetchone()[0] != 49:
        raise ValueError("EDINET scope upgrade accepts only schema 49")
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name='edinet_facts_scope'").fetchone():
        raise ValueError("schema 49 already contains new scope state")
    tables, indexes = _expected_schema_shape(SCHEMA49_SQL)
    for name, shape in tables.items():
        if _table_info(connection, name) != shape:
            raise ValueError(f"schema 49 table differs: {name}")
    for name, index_shape in indexes.items():
        if _index_info(connection, index_shape[0], name) != index_shape:
            raise ValueError(f"schema 49 index differs: {name}")


def legacy_scope(connection: sqlite3.Connection, *, stage: bool = False) -> FactsScope:
    rows = connection.execute(
        "SELECT coverage_key,status,error FROM source_coverage "
        "WHERE source='edinet_research_facts' ORDER BY coverage_key"
    ).fetchall()
    ids = frozenset(key for key, _, _ in rows if key != "initialized")
    # A retained original proves its family was requested even when extraction
    # stopped before writing a status. Never infer targets from the whole list.
    ids |= frozenset(
        row[0]
        for row in connection.execute(
            "SELECT doc_id FROM edinet_source_archives WHERE source_type=1"
        )
    )
    if any(not isinstance(key, str) or not key for key in ids):
        raise ValueError("invalid legacy EDINET acquisition identity; scope cannot be inferred")
    baseline = next(((status, value) for key, status, value in rows if key == "initialized"), None)
    since = None
    if baseline is not None:
        status, value = baseline
        parsed = json.loads(value or "null")
        if (
            status != "ok"
            or not isinstance(parsed, dict)
            or not isinstance(parsed.get("since"), str)
        ):
            raise ValueError(
                "legacy initialized requires a valid since; never infer a full history scope"
            )
        since = date.fromisoformat(parsed["since"])
        if since.isoformat() != parsed["since"]:
            raise ValueError("legacy initialized requires an ISO date")
    # Digest only the legacy fields that determine scope, not extraction results.
    origin = hashlib.sha256(
        json.dumps(
            {"initialized": baseline, "doc_ids": sorted(ids)},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    return FactsScope(None if stage else since, ids, frozenset({origin}))


def upgrade_facts_scope_working_copy(source: Path, output: Path, *, stage: bool = False) -> None:
    if source.resolve() == output.resolve():
        raise ValueError("scope migration requires a separate copy")
    old = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        old.execute("BEGIN")
        validate_schema49(old)
        scope = legacy_scope(old, stage=stage)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("xb"):
            pass
        target = sqlite3.connect(output)
        try:
            old.backup(target)
            target.executescript(EDINET_FACTS_SCOPE_DDL)
            with target:
                merge_scope(target, scope)
                target.execute("PRAGMA user_version=50")
            validate_current_schema(target)
        finally:
            target.close()
    finally:
        old.close()
