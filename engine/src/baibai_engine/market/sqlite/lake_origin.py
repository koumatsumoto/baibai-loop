"""Bind a hydrated market store to the exact L1 release it contains."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class LakeStoreOriginError(RuntimeError):
    """The market store cannot prove or advance its L1 origin."""


@dataclass(frozen=True, slots=True)
class LakeStoreOrigin:
    release_id: str
    release_manifest_sha256: str

    def __post_init__(self) -> None:
        if _RELEASE_ID.fullmatch(self.release_id) is None:
            raise ValueError("invalid lake store origin release_id")
        if _SHA256.fullmatch(self.release_manifest_sha256) is None:
            raise ValueError("invalid lake store origin manifest digest")


def read_lake_store_origin(path: Path) -> LakeStoreOrigin | None:
    """Read the identity carried by this SQLite file, never by a sidecar."""

    if not path.is_file():
        raise LakeStoreOriginError(f"market store does not exist: {path}")
    try:
        with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as connection:
            return read_lake_store_origin_from_connection(connection)
    except sqlite3.Error as exc:
        raise LakeStoreOriginError("market store origin cannot be read") from exc


def read_lake_store_origin_from_connection(
    connection: sqlite3.Connection,
) -> LakeStoreOrigin | None:
    rows = connection.execute(
        "SELECT release_id, release_manifest_sha256 FROM lake_store_origin WHERE singleton = 1"
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise LakeStoreOriginError("market store has more than one lake origin")
    try:
        return LakeStoreOrigin(release_id=str(rows[0][0]), release_manifest_sha256=str(rows[0][1]))
    except ValueError as exc:
        raise LakeStoreOriginError(str(exc)) from exc


def write_lake_store_origin(
    connection: sqlite3.Connection,
    origin: LakeStoreOrigin,
) -> None:
    connection.execute(
        """
        INSERT INTO lake_store_origin(singleton, release_id, release_manifest_sha256)
        VALUES (1, ?, ?)
        ON CONFLICT(singleton) DO UPDATE SET
          release_id = excluded.release_id,
          release_manifest_sha256 = excluded.release_manifest_sha256
        """,
        (origin.release_id, origin.release_manifest_sha256),
    )


def advance_lake_store_origin(
    path: Path,
    *,
    expected: LakeStoreOrigin | None,
    target: LakeStoreOrigin,
) -> None:
    """Move the local marker only if the exported store still has its old identity."""

    try:
        with sqlite3.connect(path, isolation_level=None, timeout=30) as connection:
            connection.execute("BEGIN IMMEDIATE")
            actual = read_lake_store_origin_from_connection(connection)
            if actual != expected:
                connection.rollback()
                raise LakeStoreOriginError(
                    "market store origin changed while its release was being published"
                )
            write_lake_store_origin(connection, target)
            connection.commit()
    except LakeStoreOriginError:
        raise
    except sqlite3.Error as exc:
        raise LakeStoreOriginError("market store origin update failed") from exc
