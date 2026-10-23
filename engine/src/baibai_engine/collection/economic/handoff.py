"""Export only new economic receipts for a source-scoped publication handoff."""

from __future__ import annotations

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
                        f"INSERT INTO {table} VALUES ({placeholders})",
                        rows,  # nosec B608
                    )
