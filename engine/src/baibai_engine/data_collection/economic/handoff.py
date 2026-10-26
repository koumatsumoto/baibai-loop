"""Export source receipts and select their change-only canonical handoff."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from . import store

_RECEIPT_CLOCKS = {
    "economic_source_records": "fetched_at",
    "economic_source_documents": "fetched_at",
    "economic_source_runs": "started_at",
}


def export_receipts(
    source: Path, output: Path, *, source_ids: Sequence[str], since: datetime
) -> None:
    """Copy original receipt identities, including partial and no-change outcomes.

    Create a new SQLite file rather than deleting unrelated rows from a backup:
    deleted pages must not carry other source history into the handoff artifact.
    """
    if output.exists():
        raise FileExistsError(output)
    if since.tzinfo is None:
        raise ValueError("handoff cutoff requires a timezone")
    with closing(store.open_read_only_connection(source)) as received:
        store.validate_current_schema(received)
        with closing(store.initialize_database(output)) as target, target:
            for table, clock in _RECEIPT_CLOCKS.items():
                for source_id in dict.fromkeys(source_ids):
                    # Table/clock are fixed identifiers in this module, values are bound.
                    rows = received.execute(
                        f"SELECT * FROM {table} WHERE source_id = ? AND {clock} >= ?",  # nosec B608
                        (source_id, since.astimezone(UTC).isoformat(timespec="microseconds")),
                    )
                    placeholders = ",".join("?" for _ in rows.description or ())
                    target.executemany(
                        f"INSERT INTO {table} VALUES ({placeholders})",  # nosec B608
                        rows,
                    )


class EconomicReceiptConflict(ValueError):
    """The same source receipt identity carries different original bytes."""


def accept_rechecks(source: Path, canonical: Path, output: Path) -> dict[str, object]:
    """Compare real received payloads in order against a read-only fresh canonical.

    Canonical remains append-only and change-only. Exact identities already there
    are included for idempotent readback. An unintegrated older receipt behind a
    later canonical vintage is retained in staging and reported for explicit
    recovery; it cannot silently roll the current value back.
    """
    export_receipts(source, output, source_ids=(), since=datetime.now(UTC))
    late: list[list[str]] = []
    accepted = 0
    unchanged = 0
    with (
        closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as raw,
        closing(sqlite3.connect(canonical.resolve().as_uri() + "?mode=ro", uri=True)) as current,
        closing(sqlite3.connect(output)) as selected,
        selected,
    ):
        store.validate_current_schema(raw)
        store.validate_current_schema(current)
        current.execute("BEGIN")
        for table, keys in store.RECEIPT_KEYS.items():
            latest: dict[tuple[str, str], tuple[object, ...] | None] = {}
            order = (
                "started_at, run_id" if table == "economic_source_runs" else "fetched_at, source_id"
            )
            names = [item[1] for item in raw.execute(f"PRAGMA table_info({table})")]  # nosec B608
            for row in raw.execute(f"SELECT * FROM {table} ORDER BY {order}"):  # nosec B608
                where = " AND ".join(f"{key} = ?" for key in keys)
                identity = tuple(row[names.index(key)] for key in keys)
                prior = current.execute(f"SELECT * FROM {table} WHERE {where}", identity).fetchone()  # nosec B608
                if prior is not None and tuple(prior) != tuple(row):
                    raise EconomicReceiptConflict("conflicting economic receipt identity")
                if prior is None and table != "economic_source_runs":
                    key = (str(row[0]), str(row[1]))
                    if key not in latest:
                        latest[key] = current.execute(
                            f"SELECT * FROM {table} WHERE {keys[0]} = ? AND {keys[1]} = ? "  # nosec B608
                            "ORDER BY fetched_at DESC LIMIT 1",
                            key,
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
    return {
        "accepted_receipts": accepted,
        "unchanged_rechecks": unchanged,
        "late_recovery_required": late,
    }
