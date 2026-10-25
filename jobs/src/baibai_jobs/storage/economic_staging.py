"""Immutable source receipt handoff in the existing private stores bucket."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

from baibai_jobs.storage.external_assessment_staging import put_private, read_private
from baibai_jobs.storage.lake_publish import LakePublishError, ObjectStore
from baibai_jobs.storage.source_dependencies import save_dependencies


def stage_prefix(stage_id: str, *, market: bool = False, namespace: str | None = None) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}", stage_id) is None:
        raise ValueError("invalid source handoff identity")
    namespace = namespace or ("market-sources" if market else "economic-sources")
    if namespace not in {"market-sources", "economic-sources", "source-inputs"}:
        raise ValueError("invalid source handoff namespace")
    return f"coordination/{namespace}/{stage_id}"


def save_handoff(
    store: ObjectStore,
    stage_id: str,
    directory: Path,
    *,
    market: bool = False,
    namespace: str | None = None,
) -> str:
    prefix = stage_prefix(stage_id, market=market, namespace=namespace)
    path = directory / "receipts.sqlite"
    sha, md5 = hashlib.sha256(), hashlib.md5(usedforsecurity=False)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(chunk)
            md5.update(chunk)
    digest, size = sha.hexdigest(), path.stat().st_size
    key = f"{prefix}/receipts-{digest}.sqlite"
    remote = store.head(key)
    if remote is None:
        path.chmod(0o600)
        remote = store.put_file(
            key,
            path,
            sha256=digest,
            content_md5=base64.b64encode(md5.digest()).decode(),
            content_type="application/vnd.sqlite3",
            if_none_match=True,
        )
    if remote.size != size or remote.metadata.get("sha256") != digest:
        raise LakePublishError("source private receipt write mismatch")
    value: dict[str, object] = {
        "result": json.loads((directory / "result.json").read_text(encoding="utf-8")),
        "receipt_key": key,
        "receipt_sha256": digest,
        "receipt_bytes": size,
    }
    if market:
        value["dependencies"] = save_dependencies(store, prefix, directory)
    manifest = json.dumps(value, sort_keys=True).encode()
    manifest_key = prefix + "/handoff.json"
    prior = read_private(store, manifest_key)
    if prior is None:
        put_private(store, manifest_key, manifest, previous_etag=None)
    elif prior.payload != manifest:
        raise LakePublishError("source handoff identity already has different receipts")
    return stage_id


def load_handoff(
    store: ObjectStore,
    stage_id: str,
    directory: Path,
    *,
    market: bool = False,
    namespace: str | None = None,
) -> None:
    prefix = stage_prefix(stage_id, market=market, namespace=namespace)
    saved = read_private(store, prefix + "/handoff.json")
    if saved is None:
        raise LakePublishError("source private handoff is not complete")
    manifest = json.loads(saved.payload)
    digest, size = manifest["receipt_sha256"], manifest["receipt_bytes"]
    key = f"{prefix}/receipts-{digest}.sqlite"
    if manifest["receipt_key"] != key or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise LakePublishError("source handoff receipt identity differs")
    remote = store.head(key)
    if remote is None or remote.size != size or remote.metadata.get("sha256") != digest:
        raise LakePublishError("source handoff receipt metadata differs")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "receipts.sqlite"
    store.download_file(key, path, expect_bytes=size)
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != digest:
        raise LakePublishError("source handoff receipt content differs")
    path.chmod(0o600)
    (directory / "result.json").write_text(json.dumps(manifest["result"]), encoding="utf-8")
