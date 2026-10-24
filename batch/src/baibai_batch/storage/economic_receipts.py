"""Select change-only canonical receipts from actual source recheck handoffs."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from baibai_batch.storage.merge_economic_store import FACT_KEYS
from baibai_batch.storage.store_merge import MergeError
from baibai_engine.batch_api import export_economic_receipts, validate_economic_schema


def accept_rechecks(source: Path, canonical: Path, output: Path) -> dict[str, object]:
    """Compare real received payloads in order against a read-only fresh canonical.

    Canonical remains append-only and change-only. Exact identities already there
    are included for idempotent readback. An unintegrated older receipt behind a
    later canonical vintage is retained in staging and reported for explicit
    recovery; it cannot silently roll the current value back.
    """
    export_economic_receipts(source, output, source_ids=(), since=datetime.now(UTC))
    late: list[list[str]] = []
    accepted = 0
    unchanged = 0
    with (
        closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as raw,
        closing(sqlite3.connect(canonical.resolve().as_uri() + "?mode=ro", uri=True)) as current,
        closing(sqlite3.connect(output)) as selected,
        selected,
    ):
        validate_economic_schema(raw)
        validate_economic_schema(current)
        current.execute("BEGIN")
        for table, keys in FACT_KEYS.items():
            latest: dict[tuple[str, str], tuple[object, ...] | None] = {}
            order = "started_at, run_id" if table == "economic_source_runs" else "fetched_at, source_id"
            names = [item[1] for item in raw.execute(f"PRAGMA table_info({table})")]  # nosec B608
            for row in raw.execute(f"SELECT * FROM {table} ORDER BY {order}"):  # nosec B608
                where = " AND ".join(f"{key} = ?" for key in keys)
                identity = tuple(row[names.index(key)] for key in keys)
                prior = current.execute(f"SELECT * FROM {table} WHERE {where}", identity).fetchone()  # nosec B608
                if prior is not None and tuple(prior) != tuple(row):
                    raise MergeError("conflicting economic receipt identity")
                if prior is None and table != "economic_source_runs":
                    key = (str(row[0]), str(row[1]))
                    if key not in latest:
                        latest[key] = current.execute(
                            f"SELECT * FROM {table} WHERE {keys[0]} = ? AND {keys[1]} = ? "  # nosec B608
                            "ORDER BY fetched_at DESC LIMIT 1", key,
                        ).fetchone()
                    previous = latest[key]
                    if previous is not None and str(row[2]) < str(previous[2]):
                        late.append([str(item) for item in row[:3]])
                        continue
                    if previous is not None and tuple(row[3:]) == tuple(previous[3:]):
                        unchanged += 1
                        continue
                    latest[key] = tuple(row)
                placeholders = ",".join("?" for _ in row)
                selected.execute(f"INSERT INTO {table} VALUES ({placeholders})", row)  # nosec B608
                accepted += 1
    return {"accepted_receipts": accepted, "unchanged_rechecks": unchanged, "late_recovery_required": late}
