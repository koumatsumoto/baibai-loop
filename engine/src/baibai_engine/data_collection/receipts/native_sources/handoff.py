"""Export source receipts and select their change-only canonical handoff."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from baibai_engine.data_collection.receipts.native_sources import store
from baibai_engine.data_collection.receipts.native_sources.confirmations import save_confirmation

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
            for source_id in dict.fromkeys(source_ids):
                for row in received.execute(
                    "SELECT * FROM economic_source_record_confirmations "
                    "WHERE source_id=? AND checked_at>=?",
                    (source_id, since.astimezone(UTC).isoformat(timespec="microseconds")),
                ):
                    # A no-change confirmation references an older change receipt.
                    # Carry that exact content, without inventing an earlier HTTP request.
                    anchor = received.execute(
                        "SELECT * FROM economic_source_records "
                        "WHERE source_id=? AND observed_at=? AND fetched_at=?",
                        (*row[:2], row[3]),
                    ).fetchone()
                    if anchor is None:
                        raise ValueError("native confirmation has no adopted source receipt")
                    target.execute(
                        "INSERT OR IGNORE INTO economic_source_records VALUES (?,?,?,?,?)", anchor
                    )
                    save_confirmation(target, tuple(row))


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
        confirmations = {
            (str(row[0]), str(row[1])): tuple(row)
            for row in raw.execute("SELECT * FROM economic_source_record_confirmations")
        }
        confirmed_accepted, confirmed_unchanged = _accept_confirmations(
            raw, current, selected, confirmations, late
        )
        accepted += confirmed_accepted
        unchanged += confirmed_unchanged
        for table, keys in store.RECEIPT_KEYS.items():
            latest: dict[tuple[str, str], tuple[object, ...] | None] = {}
            order = (
                "started_at, run_id" if table == "economic_source_runs" else "fetched_at, source_id"
            )
            names = [item[1] for item in raw.execute(f"PRAGMA table_info({table})")]  # nosec B608
            for row in raw.execute(f"SELECT * FROM {table} ORDER BY {order}"):  # nosec B608
                if table == "economic_source_records" and tuple(row[:2]) in confirmations:
                    continue
                where = " AND ".join(f"{key} = ?" for key in keys)
                identity = tuple(row[names.index(key)] for key in keys)
                prior = current.execute(f"SELECT * FROM {table} WHERE {where}", identity).fetchone()  # nosec B608
                if prior is not None and tuple(prior) != tuple(row):
                    raise EconomicReceiptConflict("conflicting economic receipt identity")
                if prior is None and table != "economic_source_runs":
                    key = (str(row[0]), str(row[1]))
                    if (
                        table == "economic_source_records"
                        and current.execute(
                            "SELECT 1 FROM economic_source_record_confirmations "
                            "WHERE source_id=? AND observed_at=?",
                            key,
                        ).fetchone()
                    ):
                        # An unconfirmed legacy/scoped row cannot bypass the newer
                        # period's confirmation merely by omitting its state.
                        late.append([*key, str(row[2])])
                        continue
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


def _accept_confirmations(
    raw: sqlite3.Connection,
    current: sqlite3.Connection,
    selected: sqlite3.Connection,
    confirmations: dict[tuple[str, str], tuple[str, ...]],
    late: list[list[str]],
) -> tuple[int, int]:
    accepted = unchanged = 0
    for key, confirmation in sorted(confirmations.items()):
        rows = {
            str(row[2]): tuple(row)
            for row in raw.execute(
                "SELECT * FROM economic_source_records WHERE source_id=? AND observed_at=?",
                key,
            )
        }
        anchor = rows.get(confirmation[3])
        if (
            anchor is None
            or confirmation[2] < confirmation[3]
            or hashlib.sha256(anchor[4]).hexdigest() != confirmation[4]
        ):
            raise EconomicReceiptConflict("native confirmation differs from its source receipt")
        if confirmation[3] < confirmation[2]:
            # Keep an actual changed receipt at its original clock. The later
            # confirmation is another observation of the same content, not a
            # replacement identity for that earlier change.
            rows[confirmation[2]] = (*anchor[:2], confirmation[2], *anchor[3:])
        prior_check = current.execute(
            "SELECT * FROM economic_source_record_confirmations "
            "WHERE source_id=? AND observed_at=?",
            key,
        ).fetchone()
        previous = None
        if prior_check is not None:
            previous = current.execute(
                "SELECT * FROM economic_source_records "
                "WHERE source_id=? AND observed_at=? AND fetched_at=?",
                (*key, prior_check[3]),
            ).fetchone()
            if previous is None or hashlib.sha256(previous[4]).hexdigest() != prior_check[4]:
                raise EconomicReceiptConflict("canonical confirmation lost its source receipt")
        elif current.execute(
            "SELECT 1 FROM economic_source_records WHERE source_id=? AND observed_at=? LIMIT 1", key
        ).fetchone():
            # State loss cannot be repaired from change clocks: an omitted equal
            # confirmation may have been newer. Recheck the source explicitly.
            late.append([*key, "confirmation_missing"])
            continue
        checked_at = str(prior_check[2]) if prior_check is not None else ""
        adopted = tuple(previous) if previous is not None else None
        changed_check = False
        for stamp, row in sorted(rows.items()):
            exact = current.execute(
                "SELECT * FROM economic_source_records "
                "WHERE source_id=? AND observed_at=? AND fetched_at=?",
                (*key, stamp),
            ).fetchone()
            if exact is not None and tuple(exact) != row:
                raise EconomicReceiptConflict("conflicting economic receipt identity")
            if stamp < checked_at:
                if exact is None and not (
                    stamp == confirmation[3] and confirmation[2] > checked_at
                ):
                    late.append([*key, stamp])
                # A carried anchor can precede the canonical collection state.
                # It only supplies the later real confirmation's content.
                continue
            equal = adopted is not None and tuple(row[3:]) == tuple(adopted[3:])
            if stamp == checked_at and not equal:
                raise EconomicReceiptConflict(
                    "conflicting native confirmation at the same received time"
                )
            if equal:
                unchanged += 1
            else:
                adopted = row
                selected.execute(
                    "INSERT OR IGNORE INTO economic_source_records VALUES (?,?,?,?,?)", row
                )
                accepted += int(exact is None)
            checked_at = stamp
            changed_check = True
        if changed_check:
            assert adopted is not None
            selected.execute(
                "INSERT OR IGNORE INTO economic_source_records VALUES (?,?,?,?,?)", adopted
            )
            save_confirmation(
                selected,
                (*key, checked_at, str(adopted[2]), hashlib.sha256(adopted[4]).hexdigest()),
            )
    return accepted, unchanged
