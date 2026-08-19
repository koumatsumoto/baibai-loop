"""Resolution and digest validation for typed lake lineage references."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .models import RetainedSourceRef

_VERIFIED: ContextVar[dict[tuple[str, str], Path] | None] = ContextVar(
    "lake_verified_sources", default=None
)


@contextmanager
def verified_source_scope() -> Iterator[None]:
    """Verify each immutable source once for the length of one operation.

    A retained source is named by every partition built from it, so verifying per
    reference makes the work scale with how many times a source is mentioned rather than
    with how much of it there is. A release that carries 121 months would re-hash the
    same objects once per partition that names them.

    What makes memoizing safe is what makes the reference worth verifying at all — the
    bytes are immutable and content addressed, so a source that verified at the start of
    the operation is the same source at the end. Two references that differ in any field
    are still verified separately: the entry is keyed by the whole reference the caller
    asserted, so a memo hit means this exact question was already answered.
    """

    token = _VERIFIED.set({})
    try:
        yield
    finally:
        _VERIFIED.reset(token)


def resolve_source_ref(mirror_root: Path, source: RetainedSourceRef) -> Path:
    """Resolve one retained source inside the mirror and verify its immutable identity.

    Only sources the lake stores can be resolved. An identity-only reference such as a
    sealed SQLite generation names no key, so it is excluded by type rather than by a
    runtime branch that would otherwise have to decide what a missing file means.
    """

    memo = _VERIFIED.get()
    # The whole reference, not the part of it that names a generation. Two references
    # can agree on kind, id, and digest and still ask different questions, so an empty
    # payload reused under two namespaces would otherwise let one verified reference
    # stand for another whose object is not there at all.
    identity = (str(mirror_root.resolve()), source.model_dump_json())
    if memo is not None and identity in memo:
        return memo[identity]
    path = _resolve_source_ref(mirror_root, source)
    if memo is not None:
        memo[identity] = path
    return path


def _resolve_source_ref(mirror_root: Path, source: RetainedSourceRef) -> Path:
    root = mirror_root.resolve()
    path = (mirror_root / source.key).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("source reference does not resolve inside the lake mirror")
    if sha256_file(path) != source.sha256:
        raise ValueError("source reference digest does not match")
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_sqlite_snapshot(path: Path, *, expected_schema_version: int) -> None:
    uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        row = connection.execute("PRAGMA quick_check").fetchone()
        if row is None or row[0] != "ok":
            raise ValueError("SQLite source snapshot failed quick_check")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != expected_schema_version:
            raise ValueError("SQLite source snapshot schema version does not match")
