"""CompanyGrowth の値を変えず、同じ銘柄・計算版の固定全文だけを共用する。"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import sqlite3
import zlib
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, Literal, cast

from baibai_engine.primitives.json import encode_json

# Not a gzip header. A binary marker cannot collide with any earlier complete row.
_PREFIX = b"\x00growth/1\x00"
_FRAME_KEYS = frozenset(("input_release", "requested_end"))
_LARGE_ROOT = 4096
_MIN_SAVING = 128

type Snapshot = Literal["main", "history"]


@dataclass(frozen=True, slots=True)
class _Reference:
    origin: str
    original_sha: str
    frame: dict[str, Any]

    def pack(self) -> bytes:
        header = (self.origin, self.original_sha, self.frame)
        return _PREFIX + gzip.compress(encode_json(header).encode(), mtime=0)


def _reference(blob: bytes) -> _Reference:
    if not blob.startswith(_PREFIX):
        raise ValueError("unknown company growth storage format")
    fields = json.loads(gzip.decompress(blob[len(_PREFIX) :]))
    if (
        not isinstance(fields, list)
        or len(fields) != 3
        or not isinstance(fields[0], str)
        or not isinstance(fields[1], str)
        or not isinstance(fields[2], dict)
        or not fields[2].keys() <= _FRAME_KEYS
    ):
        raise ValueError("invalid company growth reference")
    return _Reference(*fields)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _lookup(
    connection: sqlite3.Connection, database: Snapshot, origin: str, subject: str, revision: str
) -> bytes | None:
    row = connection.execute(
        f"SELECT payload_gzip FROM {database}.generated_model WHERE generation_id=? "
        "AND model_id='company_growth' AND subject=? AND calculation_revision=?",
        (origin, subject, revision),
    ).fetchone()
    return None if row is None else bytes(row[0])


def _root(
    connection: sqlite3.Connection, database: Snapshot, ref: _Reference, subject: str, revision: str
) -> bytes:
    original = _lookup(connection, database, ref.origin, subject, revision)
    if original is None or original.startswith(b"\0") or _sha(original) != ref.original_sha:
        raise ValueError("missing or changed fixed company growth body")
    return original


def _frame(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k in _FRAME_KEYS}


def _economics(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k not in _FRAME_KEYS}


def _object(
    connection: sqlite3.Connection, database: Snapshot, subject: str, revision: str, ref: _Reference
) -> dict[str, Any]:
    original = _root(connection, database, ref, subject, revision)
    base = _economics(json.loads(gzip.decompress(original)))
    return base | ref.frame


def model_payload(
    connection: sqlite3.Connection,
    model_id: str,
    subject: str,
    revision: str,
    blob: bytes,
    *,
    database: Snapshot = "main",
) -> dict[str, Any]:
    if model_id == "company_growth" and blob.startswith(b"\0"):
        return _object(connection, database, subject, revision, _reference(blob))
    return cast(dict[str, Any], json.loads(gzip.decompress(blob)))


def model_bytes(
    connection: sqlite3.Connection,
    model_id: str,
    subject: str,
    revision: str,
    blob: bytes,
    *,
    database: Snapshot = "main",
) -> bytes:
    if model_id == "company_growth" and blob.startswith(b"\0"):
        value = model_payload(connection, model_id, subject, revision, blob, database=database)
        return encode_json(value).encode()
    return gzip.decompress(blob)


def standalone_model(
    connection: sqlite3.Connection,
    model_id: str,
    subject: str,
    revision: str,
    blob: bytes,
    *,
    database: Snapshot = "main",
) -> bytes:
    """Make a selected row portable without importing its storage anchor."""
    if model_id != "company_growth" or not blob.startswith(b"\0"):
        return blob
    raw = model_bytes(connection, model_id, subject, revision, blob, database=database)
    return gzip.compress(raw, mtime=0)


def _parts(value: dict[str, Any]) -> dict[str, bytes]:
    return {
        key: (encode_json(key) + ":" + encode_json(item)).encode() for key, item in value.items()
    }


def _canonical(parts: dict[str, bytes], frame: dict[str, Any]) -> bytes:
    fields = parts | _parts(frame)
    return b"{" + b",".join(fields[key] for key in sorted(fields)) + b"}"


@dataclass(slots=True)
class GrowthWriter:
    previous: str
    _hint: dict[str, Any] | None = None
    _warned: bool = False

    def store(
        self,
        connection: sqlite3.Connection,
        subject: str,
        revision: str,
        payload: dict[str, Any],
    ) -> bytes:
        prior = _lookup(connection, "main", self.previous, subject, revision)
        if prior is None or (len(prior) < _LARGE_ROOT and not prior.startswith(b"\0")):
            return gzip.compress(encode_json(payload).encode(), mtime=0)
        stable, frame = _parts(_economics(payload)), _frame(payload)
        current = _canonical(stable, frame)
        try:
            if prior.startswith(b"\0"):
                ref = _reference(prior)
                original = _root(connection, "main", ref, subject, revision)
                origin, digest = ref.origin, ref.original_sha
            else:
                original, origin, digest = prior, self.previous, _sha(prior)
            raw = gzip.decompress(original)
            # Most companies share the last release frame. A guess avoids a JSON
            # parse, never a byte comparison; corrections or a sparse key cannot slip through.
            if self._hint is None or _canonical(stable, self._hint) != raw:
                old = json.loads(raw)
                if not isinstance(old, dict):
                    raise ValueError("invalid complete company growth body")
                hint = _frame(old)
                if _canonical(stable, hint) != raw:
                    return gzip.compress(current, mtime=0)
                self._hint = hint
        except (ValueError, EOFError, gzip.BadGzipFile, zlib.error):
            # A new, fully calculated generation does not repair or require an old bad row.
            # Explicit reads, retries of old IDs, history recovery and pruning still fail.
            if not self._warned:
                logging.getLogger(__name__).warning(
                    "unreadable earlier company growth; storing independent current result"
                )
                self._warned = True
            return gzip.compress(current, mtime=0)
        compact = _Reference(origin, digest, frame).pack()
        # RFC 1951 §§3.2.5/7: at most 258 output bytes per length/distance pair,
        # at least two input bits. No complete gzip can beat 1032:1. When even
        # this lower bound wins, do not compress a full history just to discard it.
        if len(current) >= 1032 * (len(compact) + _MIN_SAVING):
            return compact
        full = gzip.compress(current, mtime=0)
        return compact if len(compact) + _MIN_SAVING <= len(full) else full


def preserve_growth_before_deleting(
    connection: sqlite3.Connection, deleted: Collection[str]
) -> None:
    if not deleted:
        return
    # Only explicit pruning may remove an anchor. The oldest remaining child
    # becomes the complete copy; all other children still take at most one hop.
    retained = connection.execute(
        "SELECT generation_id,run_clock(evaluation_time) FROM model_generation "
        "WHERE generation_id NOT IN (SELECT value FROM json_each(?)) "
        "ORDER BY as_of,run_clock(evaluation_time),generation_id",
        (encode_json(tuple(deleted)),),
    ).fetchall()
    moved: dict[tuple[str, str, str, str], tuple[str, str, str]] = {}
    for generation, clock in retained:
        rows = connection.execute(
            "SELECT subject,calculation_revision,payload_gzip FROM generated_model "
            "WHERE generation_id=? AND model_id='company_growth' "
            "AND substr(payload_gzip,1,1)=x'00'",
            (generation,),
        ).fetchall()
        for subject, revision, blob in rows:
            ref = _reference(blob)
            if ref.origin not in deleted:
                continue
            key = (ref.origin, subject, revision, ref.original_sha)
            root = moved.get(key)
            if root is None or root[1] > clock:
                content = _object(connection, "main", subject, revision, ref)
                replacement = gzip.compress(encode_json(content).encode(), mtime=0)
                moved[key] = (generation, clock, _sha(replacement))
            else:
                replacement = _Reference(root[0], root[2], ref.frame).pack()
                if len(replacement) > len(blob):
                    # A conspicuously long ID or frame need not buy an expensive pointer.
                    alternative = standalone_model(
                        connection, "company_growth", subject, revision, blob
                    )
                    if len(replacement) + _MIN_SAVING > len(alternative):
                        replacement = alternative
            connection.execute(
                "UPDATE generated_model SET payload_gzip=? "
                "WHERE generation_id=? AND model_id='company_growth' AND subject=?",
                (replacement, generation, subject),
            )
