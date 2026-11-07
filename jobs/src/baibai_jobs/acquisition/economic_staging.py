"""Immutable source receipt handoff in the existing private stores bucket."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

from baibai_engine.data_collection.receipts.native_sources.planning_inputs import (
    validate_planning_input,
)
from baibai_engine.storage.objects.json import put_private, read_private
from baibai_engine.storage.objects.r2 import (
    LakePublishError,
    ObjectStore,
)
from baibai_jobs.acquisition.dependencies import save_dependencies

_PLANNING_INPUT_GROUPS = {
    "pmi-acquisition-input-v1": "economic-spglobal_pmi",
    "umich-acquisition-input-v1": "economic-umich_sca",
    "ecb-fx-acquisition-input-v1": "economic-ecb_fx",
    "estat-acquisition-input-v1": "economic-estat",
    "mof-jgb-acquisition-input-v1": "economic-mof_jgb",
    "frb-h15-acquisition-input-v1": "economic-frb_h15",
    "yahoo-acquisition-input-v1": "economic-yahoo",
    "multpl-acquisition-input-v1": "economic-multpl",
    "cftc-acquisition-input-v1": "economic-cftc",
    "nikkei-indexes-acquisition-input-v1": "economic-nikkei_indexes",
}
_PLANNING_INPUT_FORMATS = {group: wire for wire, group in _PLANNING_INPUT_GROUPS.items()}


def stage_prefix(stage_id: str, *, market: bool = False, namespace: str | None = None) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}", stage_id) is None:
        raise ValueError("invalid source handoff identity")
    namespace = namespace or ("market-sources" if market else "economic-sources")
    if namespace not in {"market-sources", "economic-sources", "source-inputs", "article-sources"}:
        raise ValueError("invalid source handoff namespace")
    return f"coordination/{namespace}/{stage_id}"


def save_handoff(
    store: ObjectStore,
    stage_id: str,
    directory: Path,
    *,
    market: bool = False,
    namespace: str | None = None,
    reusable_receipt: dict[str, object] | None = None,
    input_group: str | None = None,
) -> str:
    prefix = stage_prefix(stage_id, market=market, namespace=namespace)
    path = directory / "receipts.sqlite"
    receipt_format = (
        _PLANNING_INPUT_FORMATS.get(input_group or "") if namespace == "source-inputs" else None
    )
    receipt_field = "planning_key" if receipt_format else "receipt_key"
    if receipt_format:
        if market:
            raise ValueError("planning input is not a market receipt handoff")
        validate_planning_input(path, _PLANNING_INPUT_GROUPS[receipt_format])
    sha, md5 = hashlib.sha256(), hashlib.md5(usedforsecurity=False)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(chunk)
            md5.update(chunk)
    digest, size = sha.hexdigest(), path.stat().st_size
    key = f"{prefix}/receipts-{digest}.sqlite"
    if reusable_receipt is not None and reusable_receipt.get("receipt_sha256") == digest:
        if namespace != "source-inputs" or input_group is None:
            raise ValueError("receipt reuse is limited to scoped source inputs")
        reusable_receipt_key = reusable_receipt.get(receipt_field)
        pattern = (
            rf"coordination/source-inputs/{re.escape(input_group)}-"
            rf"[A-Za-z0-9_-]+/receipts-{digest}\.sqlite"
        )
        if (
            isinstance(reusable_receipt_key, str)
            and reusable_receipt.get("receipt_bytes") == size
            and re.fullmatch(pattern, reusable_receipt_key) is not None
        ):
            key = reusable_receipt_key
    remote = store.head(key)
    if key != f"{prefix}/receipts-{digest}.sqlite" and (
        remote is None or remote.size != size or remote.metadata.get("sha256") != digest
    ):
        # A retired receipt may be missing or invalid; publish this stage's own object.
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
        receipt_field: key,
        "receipt_sha256": digest,
        "receipt_bytes": size,
    }
    if receipt_format:
        # Pre-reader-first code must fail rather than silently broaden recovery.
        value["receipt_format"] = receipt_format
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
    input_group: str | None = None,
) -> None:
    prefix = stage_prefix(stage_id, market=market, namespace=namespace)
    saved = read_private(store, prefix + "/handoff.json")
    if saved is None:
        raise LakePublishError("source private handoff is not complete")
    manifest = json.loads(saved.payload)
    receipt_format = manifest.get("receipt_format") if isinstance(manifest, dict) else None
    planning_group = (
        _PLANNING_INPUT_GROUPS.get(receipt_format) if isinstance(receipt_format, str) else None
    )
    if planning_group is not None:
        if (
            market
            or namespace != "source-inputs"
            or input_group != planning_group
            or "receipt_key" in manifest
        ):
            raise ValueError("planning input is not a source receipt handoff")
        manifest["receipt_key"] = manifest.get("planning_key")
    if (
        not isinstance(manifest, dict)
        or not isinstance(manifest.get("receipt_sha256"), str)
        or type(manifest.get("receipt_bytes")) is not int
        or not isinstance(manifest.get("receipt_key"), str)
        or not isinstance(manifest.get("result"), dict)
    ):
        raise ValueError("invalid source handoff manifest")
    digest, size = manifest["receipt_sha256"], manifest["receipt_bytes"]
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise LakePublishError("source handoff receipt identity differs")
    key = f"{prefix}/receipts-{digest}.sqlite"
    if namespace == "source-inputs" and input_group is not None:
        pattern = (
            rf"coordination/source-inputs/{re.escape(input_group)}-"
            rf"[A-Za-z0-9_-]+/receipts-{digest}\.sqlite"
        )
        if re.fullmatch(pattern, manifest["receipt_key"]) is not None:
            key = manifest["receipt_key"]
    if manifest["receipt_key"] != key:
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
    if planning_group is not None:
        validate_planning_input(path, planning_group)
    path.chmod(0o600)
    (directory / "result.json").write_text(json.dumps(manifest["result"]), encoding="utf-8")
