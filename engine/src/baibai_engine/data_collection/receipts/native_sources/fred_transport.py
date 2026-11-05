"""固定FRED受領の本文を共有packへ分離する。採用正本はschema13 SQLiteのまま。

Inventory is only a reusable-payload hint. Every immutable receipt fixes its own
pack locators; reading a receipt never follows inventory or another latest head.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
import tempfile
from collections import defaultdict
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.catalog import load_sources
from baibai_engine.data_collection.receipts.native_sources.confirmations import (
    validate_collection_confirmations,
)
from baibai_engine.data_collection.receipts.native_sources.store import (
    CONFIRMATION_TABLES,
    RECEIPT_KEYS,
    initialize_database,
    validate_current_schema,
)
from baibai_engine.storage.objects.r2 import LakeCASConflict, LakePublishError, ObjectStore

FORMAT = "economic-fred-packs-v1"
PREFIX = "l1/economic/fred/v1/"
_MAX_BYTES = 32 * 1024 * 1024
_PACK_TARGET = 1024 * 1024
_TABLES = (*RECEIPT_KEYS, *CONFIRMATION_TABLES)
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def _json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("invalid FRED transport object")
    return value


def _sources() -> set[str]:
    return {key for key, source in load_sources().items() if source.provider == "fred_csv"}


def _source(value: Any, allowed: set[str] | None = None) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}", value) is None
        or (allowed is not None and value not in allowed)
    ):
        raise ValueError("FRED transport contains another source")
    return value


def _digest(value: Any) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ValueError("invalid FRED transport digest")
    return value


def _read(store: ObjectStore, key: str) -> tuple[bytes, str] | None:
    before = store.head(key)
    if before is None:
        return None
    if not 0 < before.size <= _MAX_BYTES:
        raise LakePublishError("FRED transport object exceeds its bound")
    payload = store.get_bytes(key)
    after = store.head(key)
    if (
        after is None
        or after.etag != before.etag
        or len(payload) != before.size
        or before.metadata.get("sha256") != _sha(payload)
    ):
        raise LakePublishError("FRED transport object changed or failed integrity")
    return payload, before.etag


def _put(store: ObjectStore, key: str, payload: bytes, *, etag: str | None) -> None:
    if not 0 < len(payload) <= _MAX_BYTES:
        raise ValueError("FRED transport object exceeds its bound")
    with tempfile.TemporaryDirectory(prefix="fred-transport-") as temporary:
        path = Path(temporary) / "object"
        path.write_bytes(payload)
        path.chmod(0o600)
        try:
            store.put_file(
                key,
                path,
                sha256=_sha(payload),
                content_md5=base64.b64encode(
                    hashlib.md5(payload, usedforsecurity=False).digest()
                ).decode(),
                content_type="application/json",
                if_match=etag,
                if_none_match=etag is None,
            )
        except LakePublishError:
            actual = _read(store, key)
            if actual is not None and actual[0] == payload:
                return
            raise
    actual = _read(store, key)
    if actual is None or actual[0] != payload:
        raise LakePublishError("FRED transport PUT is not confirmed")


def _immutable(store: ObjectStore, kind: str, value: Any) -> str:
    payload = _json(value)
    digest = _sha(payload)
    key = PREFIX + kind + "/" + digest + ".json"
    saved = _read(store, key)
    if saved is None:
        try:
            _put(store, key, payload, etag=None)
        except LakeCASConflict:
            saved = _read(store, key)
            if saved is None or saved[0] != payload:
                raise
    elif saved[0] != payload:
        raise LakePublishError("FRED immutable object differs from its digest")
    return digest


def _fixed(store: ObjectStore, kind: str, digest: Any) -> Any:
    digest = _digest(digest)
    saved = _read(store, PREFIX + kind + "/" + digest + ".json")
    if saved is None or _sha(saved[0]) != digest:
        raise LakePublishError("fixed FRED transport object is missing or changed")
    return json.loads(saved[0])


def snapshot(path: Path) -> dict[str, list[list[Any]]]:
    """Canonical logical rows, preserving exact native payload bytes and clocks."""
    allowed = _sources()
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("BEGIN")
        validate_current_schema(conn)
        validate_collection_confirmations(conn)
        rows: dict[str, list[list[Any]]] = {}
        for table in _TABLES:
            order = ",".join(RECEIPT_KEYS.get(table, ("source_id", "observed_at")))
            if table == "economic_source_collection_confirmations":
                order = "source_id"
            # Both table and order are fixed Collection-owned identifiers.
            data = [list(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]  # nosec B608
            for row in data:
                _source(row[1] if table == "economic_source_runs" else row[0], allowed)
                if table == "economic_source_records":
                    row[4] = base64.b64encode(row[4]).decode()
            rows[table] = data
        if rows["economic_source_documents"]:
            raise ValueError("FRED transport cannot contain documents")
        return rows


def snapshot_digest(rows: Mapping[str, list[list[Any]]]) -> str:
    return _sha(_json(rows))


def _payloads(
    store: ObjectStore, source: str, bodies: dict[str, str], *, metadata: bool = False
) -> dict[str, str]:
    """Store unknown bytes once in packs; do not use inventory as adoption state."""
    pack_kind = source + ("/metadata-packs" if metadata else "/packs")
    key = PREFIX + source + ("/metadata-inventory.json" if metadata else "/inventory.json")
    saved = _read(store, key)
    inventory = _object(json.loads(saved[0])) if saved else {}
    refs: dict[str, str] = {}
    verified: dict[str, dict[str, Any]] = {}
    unknown = {}
    for digest, body in sorted(bodies.items()):
        pack = inventory.get(digest)
        if pack is None:
            unknown[digest] = body
            continue
        pack = _digest(pack)
        if pack not in verified:
            verified[pack] = _object(_fixed(store, pack_kind, pack))
        if verified[pack].get(digest) != body:
            raise LakePublishError("FRED payload inventory differs from retained bytes")
        refs[digest] = pack
    pending: dict[str, str] = {}
    size = 2
    for digest, body in unknown.items():
        encoded_size = len(body) + 70
        if pending and size + encoded_size > _PACK_TARGET:
            pack = _immutable(store, pack_kind, pending)
            refs.update(dict.fromkeys(pending, pack))
            pending, size = {}, 2
        pending[digest] = body
        size += encoded_size
    if pending:
        pack = _immutable(store, pack_kind, pending)
        refs.update(dict.fromkeys(pending, pack))
    if unknown:
        # Acquisition and publication can share the inventory concurrently. It is
        # a union of verified hints, never a last-writer-wins canonical head.
        for attempt in range(3):
            current = _object(json.loads(saved[0])) if saved else {}
            updated = {**refs, **current}
            try:
                _put(store, key, _json(updated), etag=saved[1] if saved else None)
                break
            except LakeCASConflict:
                if attempt == 2:
                    raise
                saved = _read(store, key)
    return refs


def save(store: ObjectStore, rows: dict[str, list[list[Any]]]) -> dict[str, str]:
    """Save only new payloads and changed metadata months, then a small fixed root."""
    bodies: dict[str, dict[str, str]] = defaultdict(dict)
    for source, _day, _stamp, _count, payload in rows["economic_source_records"]:
        bodies[source][_sha(base64.b64decode(payload, validate=True))] = payload
    refs = {source: _payloads(store, source, value) for source, value in bodies.items()}
    shards: dict[tuple[str, str, str], list[list[Any]]] = defaultdict(list)
    for source, day, stamp, count, payload in rows["economic_source_records"]:
        digest = _sha(base64.b64decode(payload, validate=True))
        shards[("records", source, day[:7])].append(
            [day, stamp, count, digest, refs[source][digest]]
        )
    for source, day, _checked, adopted, digest in rows["economic_source_record_confirmations"]:
        # All period checks share the actual complete-response clock held below.
        shards[("confirmations", source, day[:7])].append([day, adopted, digest])
    for row in rows["economic_source_runs"]:
        shards[("runs", row[1], row[4][:7])].append(row)
    root: dict[str, Any] = {
        "collections": rows["economic_source_collection_confirmations"],
        "months": [],
    }
    metadata_bodies: dict[str, dict[str, str]] = defaultdict(dict)
    for (_, source, _), values in shards.items():
        payload = _json(values)
        metadata_bodies[source][_sha(payload)] = base64.b64encode(payload).decode()
    metadata_refs = {
        source: _payloads(store, source, values, metadata=True)
        for source, values in metadata_bodies.items()
    }
    for (kind, source, month), values in sorted(shards.items()):
        digest = _sha(_json(values))
        root["months"].append([kind, source, month, digest, metadata_refs[source][digest]])
    return {"root": _immutable(store, "roots", root), "content_sha256": snapshot_digest(rows)}


def load(store: ObjectStore, receipt: Any, output: Path) -> None:
    """Reconstruct exact logical rows without reading mutable inventory or heads."""
    receipt = _object(receipt)
    if set(receipt) != {"root", "content_sha256"}:
        raise ValueError("invalid fixed FRED receipt")
    expected = _digest(receipt["content_sha256"])
    root = _object(_fixed(store, "roots", receipt["root"]))
    if set(root) != {"collections", "months"}:
        raise ValueError("invalid fixed FRED receipt root")
    collections = root["collections"]
    months = root["months"]
    if not isinstance(collections, list) or not isinstance(months, list):
        raise ValueError("invalid fixed FRED receipt rows")
    clocks = {}
    for row in collections:
        if not isinstance(row, list) or len(row) != 3:
            raise ValueError("invalid FRED collection state")
        clocks[_source(row[0])] = row[1]
    rows: dict[str, list[list[Any]]] = {table: [] for table in _TABLES}
    rows["economic_source_collection_confirmations"] = collections
    packs: dict[tuple[str, str], dict[str, Any]] = {}
    metadata_packs: dict[tuple[str, str], dict[str, Any]] = {}
    seen = set()
    for ref in months:
        if not isinstance(ref, list) or len(ref) != 5:
            raise ValueError("invalid FRED metadata reference")
        kind, source, month, digest, metadata_pack = ref
        source = _source(source)
        if kind not in {"records", "confirmations", "runs"} or not isinstance(month, str):
            raise ValueError("invalid FRED metadata scope")
        identity = (kind, source, month)
        if identity in seen:
            raise ValueError("duplicate FRED metadata scope")
        seen.add(identity)
        metadata_key = (source, _digest(metadata_pack))
        if metadata_key not in metadata_packs:
            metadata_packs[metadata_key] = _object(
                _fixed(store, source + "/metadata-packs", metadata_pack)
            )
        encoded_metadata = metadata_packs[metadata_key].get(_digest(digest))
        if not isinstance(encoded_metadata, str):
            raise LakePublishError("fixed FRED metadata is missing from its pack")
        metadata_bytes = base64.b64decode(encoded_metadata, validate=True)
        if _sha(metadata_bytes) != digest:
            raise LakePublishError("fixed FRED metadata differs from its digest")
        data = json.loads(metadata_bytes)
        if not isinstance(data, list):
            raise ValueError("invalid FRED metadata rows")
        for row in data:
            if not isinstance(row, list):
                raise ValueError("invalid FRED metadata row")
            if kind == "records":
                if len(row) != 5 or not isinstance(row[0], str) or row[0][:7] != month:
                    raise ValueError("FRED record differs from its month")
                day, stamp, count, content, pack = row
                content, pack = _digest(content), _digest(pack)
                key = (source, pack)
                if key not in packs:
                    packs[key] = _object(_fixed(store, source + "/packs", pack))
                encoded = packs[key].get(content)
                if (
                    not isinstance(encoded, str)
                    or _sha(base64.b64decode(encoded, validate=True)) != content
                ):
                    raise LakePublishError("FRED pack does not contain its fixed payload")
                rows["economic_source_records"].append([source, day, stamp, count, encoded])
            elif kind == "confirmations":
                if (
                    len(row) != 3
                    or not isinstance(row[0], str)
                    or row[0][:7] != month
                    or source not in clocks
                ):
                    raise ValueError("FRED confirmation differs from its complete response")
                rows["economic_source_record_confirmations"].append(
                    [source, row[0], clocks[source], row[1], row[2]]
                )
            else:
                if (
                    len(row) != 9
                    or row[1] != source
                    or not isinstance(row[4], str)
                    or row[4][:7] != month
                ):
                    raise ValueError("FRED run differs from its source or month")
                rows["economic_source_runs"].append(row)
    for table, values in rows.items():
        values.sort(
            key=lambda row: (
                row[0]
                if table in {"economic_source_runs", "economic_source_collection_confirmations"}
                else row[:3]
            )
        )
    if snapshot_digest(rows) != expected:
        raise LakePublishError("fixed FRED receipt content differs")
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".fred-read-", dir=output.parent) as temporary:
        path = Path(temporary) / "receipts.sqlite"
        with closing(initialize_database(path)) as conn, conn:
            for table, values in rows.items():
                for row in values:
                    fields = list(row)
                    if table == "economic_source_records":
                        fields[4] = base64.b64decode(fields[4], validate=True)
                    placeholders = ",".join("?" for _ in fields)
                    conn.execute(f"INSERT INTO {table} VALUES ({placeholders})", fields)  # nosec B608
            validate_collection_confirmations(conn)
        path.chmod(0o600)
        path.replace(output)
