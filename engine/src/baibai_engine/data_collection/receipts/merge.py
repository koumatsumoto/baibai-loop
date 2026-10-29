"""Copy explicitly selected original receipts, never market projections or coverage."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

_KEYS = {
    "jpx_source_bundles": ("bundle_id",),
    "jpx_source_responses": ("bundle_id", "resource_name"),
    "edinet_list_responses": ("receipt_id",),
    "edinet_source_archives": ("doc_id", "source_type"),
}


def copy_originals(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    table: str,
    identities: Sequence[tuple[object, ...]],
    *,
    selection: str,
) -> int:
    """Union exact raw identities, refusing conflicting metadata or bytes.

    Both the selected table and selection column must be from this fixed original
    receipt allowlist. The caller owns the transaction; no derived rows cross it.
    """
    keys = _KEYS[table]
    if selection not in keys:
        raise ValueError("unsupported original receipt selection")
    added = 0
    for identity in identities:
        selected_keys = keys if len(identity) == len(keys) else (selection,)
        if len(identity) != len(selected_keys):
            raise ValueError("original receipt selection is incomplete")
        where = " AND ".join(f"{key} = ?" for key in selected_keys)
        rows = source.execute(f"SELECT * FROM {table} WHERE {where}", identity)  # nosec B608
        names = tuple(item[0] for item in rows.description or ())
        positions = tuple(names.index(key) for key in keys)
        exact = " AND ".join(f"{key} = ?" for key in keys)
        for row in rows:
            existing = target.execute(
                f"SELECT * FROM {table} WHERE {exact}",  # nosec B608
                tuple(row[index] for index in positions),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != tuple(row):
                    raise ValueError("original receipt identity has conflicting content")
                continue
            placeholders = ",".join("?" for _ in names)
            target.execute(f"INSERT INTO {table} VALUES ({placeholders})", row)  # nosec B608
            added += 1
    return added
