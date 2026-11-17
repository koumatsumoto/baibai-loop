"""Private create-only evidence; publication never depends on ephemeral logs alone."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from baibai_engine.storage.objects.json import put_private, read_private
from baibai_engine.storage.objects.r2 import Boto3R2Store
from baibai_jobs.integration.finance_recovery_contract import PREFIX


def digest_file(path: Path, algorithm: str = "sha256") -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def immutable_file(store: Boto3R2Store, path: Path, directory: Path) -> dict[str, Any]:
    digest, size = digest_file(path), path.stat().st_size
    key = PREFIX + "evidence/" + digest
    before = store.head(key)
    if before is None:
        with path.open("rb") as stream:
            md5 = base64.b64encode(hashlib.file_digest(stream, "md5").digest()).decode()
        before = store.put_file(
            key,
            path,
            sha256=digest,
            content_md5=md5,
            content_type="application/octet-stream",
            if_none_match=True,
        )
    if before.size != size or before.metadata.get("sha256") != digest:
        raise ValueError("existing immutable recovery evidence differs")
    check = directory / (digest + ".readback")
    if check.exists():
        raise ValueError("evidence verification destination already exists")
    store.download_file(key, check, expect_bytes=size, max_bytes=size)
    if digest_file(check) != digest or store.head(key) != before:
        raise ValueError("immutable evidence readback differs")
    check.unlink()  # Exact original and verified private remote copy remain.
    return {"key": key, "sha256": digest, "size": size, "etag": before.etag}


def immutable_json(store: Boto3R2Store, key: str, value: dict[str, Any]) -> None:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    put_private(store, key, payload, previous_etag=None)
    saved = read_private(store, key)
    if saved is None or saved.payload != payload:
        raise ValueError("private recovery receipt outcome uncertain")


def persist_transfer_record(root: Path, record_path: Path) -> None:
    """Called synchronously before the shell can perform its next publication step."""
    prefix = os.environ.get("FINANCE_RECOVERY_EVIDENCE_PREFIX", "")
    if not prefix:
        return
    if re.fullmatch(re.escape(PREFIX) + r"[0-9a-f]{64}/attempts/[0-9]+-1/", prefix) is None:
        raise ValueError("invalid finite finance evidence prefix")
    if os.environ.get("R2_STOP_ON_REJECTION") != "1":
        raise ValueError("private transfer evidence requires strict rejection stop")
    store = Boto3R2Store(bucket="baibai-stores")
    try:
        record = json.loads(record_path.read_bytes())
        if record["phase"] == "snapshot_ready":
            record["durable_body"] = immutable_file(store, root / record["body"], root)
        immutable_json(store, prefix + "pair/" + record_path.name, record)
    finally:
        store.close()
