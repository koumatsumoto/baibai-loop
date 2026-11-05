"""Immutable source receipt handoff in the existing private stores bucket."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import tempfile
from pathlib import Path

from baibai_engine.data_collection.receipts.native_sources import fred_transport
from baibai_engine.storage.objects.r2 import (
    LakePublishError,
    ObjectStore,
)
from baibai_jobs.acquisition.dependencies import save_dependencies
from baibai_jobs.acquisition.external_assessments.staging import put_private, read_private


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
    report = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    if not market and report.get("group") in {"fred_csv", "economic-fred_csv"}:
        if namespace not in {None, "economic-sources", "source-inputs"}:
            raise ValueError("FRED packs require the economic receipt namespace")
        expected_group = "economic-fred_csv" if namespace == "source-inputs" else "fred_csv"
        if report["group"] != expected_group or (
            namespace == "source-inputs" and input_group != expected_group
        ):
            raise ValueError("FRED receipt differs from its acquisition scope")
        rows = fred_transport.snapshot(path)
        manifest_key = prefix + "/handoff.json"
        prior = read_private(store, manifest_key)
        if prior is not None:
            previous = json.loads(prior.payload)
            if (
                isinstance(previous, dict)
                and isinstance(previous.get("receipt"), dict)
                and previous.get("format") == fred_transport.FORMAT
                and previous.get("result") == report
                and previous["receipt"].get("content_sha256")
                == fred_transport.snapshot_digest(rows)
            ):
                with tempfile.TemporaryDirectory(prefix="fred-fixed-retry-") as temporary:
                    fred_transport.load(
                        store, previous["receipt"], Path(temporary) / "receipts.sqlite"
                    )
                return stage_id
            # Retry an already-published old stage without rewriting its immutable
            # contract. The old loader still verifies the original SQLite bytes.
            if isinstance(previous, dict) and "format" not in previous:
                with path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                if (
                    previous.get("result") == report
                    and previous.get("receipt_sha256") == digest
                    and previous.get("receipt_bytes") == path.stat().st_size
                ):
                    with tempfile.TemporaryDirectory(prefix="fred-legacy-retry-") as temporary:
                        load_handoff(
                            store,
                            stage_id,
                            Path(temporary),
                            namespace=namespace,
                            input_group=input_group,
                        )
                    return stage_id
            raise LakePublishError("source handoff identity already has different receipts")
        packed = {
            "format": fred_transport.FORMAT,
            "result": report,
            "receipt": fred_transport.save(store, rows),
        }
        payload = json.dumps(packed, sort_keys=True).encode()
        try:
            put_private(store, manifest_key, payload, previous_etag=None)
        except LakePublishError:
            saved = read_private(store, manifest_key)
            if saved is None or saved.payload != payload:
                raise
        saved = read_private(store, manifest_key)
        if saved is None or saved.payload != payload:
            raise LakePublishError("packed source handoff PUT is not confirmed")
        return stage_id
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
        reusable_receipt_key = reusable_receipt.get("receipt_key")
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
    input_group: str | None = None,
) -> None:
    prefix = stage_prefix(stage_id, market=market, namespace=namespace)
    saved = read_private(store, prefix + "/handoff.json")
    if saved is None:
        raise LakePublishError("source private handoff is not complete")
    manifest = json.loads(saved.payload)
    if isinstance(manifest, dict) and manifest.get("format") == fred_transport.FORMAT:
        result = manifest.get("result")
        expected_group = "economic-fred_csv" if namespace == "source-inputs" else "fred_csv"
        if (
            market
            or namespace not in {None, "economic-sources", "source-inputs"}
            or not isinstance(result, dict)
            or result.get("group") != expected_group
            or (namespace == "source-inputs" and input_group != expected_group)
        ):
            raise ValueError("FRED receipt differs from its acquisition scope")
        directory.mkdir(parents=True, exist_ok=True)
        fred_transport.load(store, manifest.get("receipt"), directory / "receipts.sqlite")
        (directory / "result.json").write_text(json.dumps(result), encoding="utf-8")
        return
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
    path.chmod(0o600)
    (directory / "result.json").write_text(json.dumps(manifest["result"]), encoding="utf-8")
