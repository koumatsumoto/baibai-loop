"""Attended two-key machine transition after the exact fresh-market L1 cutover.

This temporary tool never changes runs.sqlite, baibai.sqlite, or serving. The
old machine objects are retained under cutover-specific backup keys. A partial
transition stops for readback and review; it is not an idempotent migration.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Callable
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from botocore.awsrequest import AWSRequest
from botocore.exceptions import BotoCoreError, ClientError

from baibai_batch.storage.lake_publish import (
    Boto3R2Store,
    ObjectStore,
    RemoteObject,
    read_pointer_snapshot,
)
from baibai_batch.storage.publication_lease import (
    PublicationLeaseHandle,
    _load_handle,
    verify_held,
)
from baibai_batch.storage.publish_market_lake import _resolve_serving_release
from baibai_engine.collection.economic.store import (
    validate_current_schema as validate_economic_schema,
)
from baibai_engine.storage.lake.hydrate import dehydrate_market_store
from baibai_engine.storage.observations.lake_origin import LakeStoreOrigin, read_lake_store_origin
from baibai_engine.storage.observations.snapshot import create_snapshot, validate_snapshot
from tools.cutover.compact_market_v47 import build_compact_market
from tools.cutover.publish_fresh_market_v47 import (
    OLD_POINTER_KEY,
    PRODUCTION_SOURCE,
    CutoverError,
    SourcePin,
    _candidate_hashes,
    _read_small,
)

MARKET_KEY = "market.sqlite"
ECONOMIC_KEY = "indicators.sqlite"
RUNS_KEY = "runs.sqlite"
APPLICATION_KEY = "baibai.sqlite"
RECEIPT_KEY = "machine-manifest.json"
_ETAG = re.compile(r"[A-Za-z0-9._:-]+\Z")


@dataclass(frozen=True, slots=True)
class MachinePin:
    market_etag: str
    economic_etag: str
    runs_etag: str
    application_etag: str
    receipt_etag: str
    receipt_sha256: str
    market_sha256: str
    economic_sha256: str
    market_schema: int = 39
    economic_schema: int = 9


PRODUCTION_MACHINE = MachinePin(
    market_etag="8e85f5a957341cb9292c238a95fd9d80",
    economic_etag="53b3bd0183c4a0a399cb0980f23a485a",
    runs_etag="50ff97c5130d98edf98c6ea2fd89cd16",
    application_etag="e80da4dab959bd3f137e891ffecab798",
    receipt_etag="6a81d9293f2318cd19281973a541238f",
    receipt_sha256="6222cafa59f99cd92700fc5ddeb93a4aed252bc08506c4c83b944206bb7a12d0",
    market_sha256="f2556054de8f82d6e2754f2fd3488d31506380adf484f783d57611dd1249d33f",
    economic_sha256="0081422b0d4aefeb8107adb6ff37881156fab9ad66152456efc56c4e6cd48aae",
)


class MachineStore(ObjectStore, Protocol):
    def sqlite_user_version(self, key: str, *, etag: str) -> int: ...

    def copy_if_match(self, source: str, backup: str, *, etag: str) -> RemoteObject: ...


class R2MachineStore(Boto3R2Store):
    def sqlite_user_version(self, key: str, *, etag: str) -> int:
        import struct

        response = self._client().get_object(
            Bucket=self.bucket, Key=key, Range="bytes=0-99", IfMatch=etag
        )
        try:
            header = response["Body"].read(101)
        finally:
            response["Body"].close()
        if (
            response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 206
            or len(header) != 100
            or header[:16] != b"SQLite format 3\0"
        ):
            raise CutoverError(f"remote SQLite header is not exact: {key}")
        return int(struct.unpack(">I", header[60:64])[0])

    def copy_if_match(self, source: str, backup: str, *, etag: str) -> RemoteObject:
        client = self._client()

        def require_absent_destination(request: AWSRequest, **_: object) -> None:
            # R2's CopyObject destination condition is not S3's IfNoneMatch.
            request.headers["cf-copy-destination-if-none-match"] = "*"

        client.meta.events.register("before-sign.s3.CopyObject", require_absent_destination)
        try:
            client.copy_object(
                Bucket=self.bucket,
                Key=backup,
                CopySource={"Bucket": self.bucket, "Key": source},
                CopySourceIfMatch=etag,
                MetadataDirective="COPY",
            )
        except (ClientError, BotoCoreError) as exc:
            raise CutoverError(f"conditional backup copy outcome uncertain: {source}") from exc
        finally:
            client.meta.events.unregister("before-sign.s3.CopyObject", require_absent_destination)
        result = self.head(backup)
        if result is None:
            raise CutoverError(f"backup absent after acknowledged copy: {source}")
        return result


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _require_economic_schema(path: Path) -> None:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as connection:
        connection.execute("PRAGMA query_only = ON")
        validate_economic_schema(connection)
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise CutoverError("economic candidate has foreign key violations")


def _checkpointed_economic_identity(path: Path) -> tuple[str, str | None]:
    if not path.is_file() or path.is_symlink():
        raise CutoverError("economic candidate file is unavailable")
    wal = path.with_name(f"{path.name}-wal")
    if wal.exists() and wal.stat().st_size:
        raise CutoverError("economic candidate has an uncheckpointed WAL")
    return _candidate_hashes(path)


def _md5(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)  # nosec B324: transport checksum
    with path.open("rb") as source:
        while block := source.read(8 * 1024 * 1024):
            digest.update(block)
    return base64.b64encode(digest.digest()).decode()


def _require_head(store: MachineStore, key: str, etag: str) -> RemoteObject:
    head = store.head(key)
    if head is None or head.etag != etag:
        raise CutoverError(f"pinned generation changed: {key}")
    return head


def _pin_receipt(store: MachineStore, pin: MachinePin) -> None:
    head = _require_head(store, RECEIPT_KEY, pin.receipt_etag)
    if head.size > 4096:
        raise CutoverError("machine receipt is too large")
    raw = store.get_bytes(RECEIPT_KEY)
    if len(raw) != head.size or hashlib.sha256(raw).hexdigest() != pin.receipt_sha256:
        raise CutoverError("machine receipt bytes changed")
    if _require_head(store, RECEIPT_KEY, pin.receipt_etag).size != head.size:
        raise CutoverError("machine receipt changed during read")
    try:
        receipt = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CutoverError("machine receipt JSON is invalid") from None
    expected = {
        MARKET_KEY: pin.market_etag,
        RUNS_KEY: pin.runs_etag,
        ECONOMIC_KEY: pin.economic_etag,
    }
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema_version") != 1
        or receipt.get("keys") != expected
    ):
        raise CutoverError("machine receipt does not describe pinned store generations")


def _require_current_release(store: MachineStore, *, release_id: str, manifest_sha256: str) -> None:
    pointer = read_pointer_snapshot(store).pointer
    if (
        pointer is None
        or pointer.release_id != release_id
        or pointer.manifest_sha256 != manifest_sha256
    ):
        raise CutoverError("current L1 pointer differs from selected new release")


def _require_stage(
    store: MachineStore,
    lease: PublicationLeaseHandle,
    pin: MachinePin,
    *,
    source: SourcePin,
    release_id: str,
    manifest_sha256: str,
    market_etag: str,
    economic_etag: str,
) -> None:
    verify_held(store, lease)
    old = _read_small(
        store,
        OLD_POINTER_KEY,
        max_bytes=1024,
        expected_etag=source.pointer_etag,
        expected_sha256=source.pointer_sha256,
    )
    if json.loads(old).get("release_id") != source.release_id:
        raise CutoverError("legacy pointer changed during machine transition")
    _require_current_release(store, release_id=release_id, manifest_sha256=manifest_sha256)
    _require_head(store, MARKET_KEY, market_etag)
    _require_head(store, ECONOMIC_KEY, economic_etag)
    _require_head(store, RUNS_KEY, pin.runs_etag)
    _require_head(store, APPLICATION_KEY, pin.application_etag)
    _pin_receipt(store, pin)


def required_workspace_bytes(market_bytes: int, economic_bytes: int) -> int:
    """Market snapshot plus SQLite's 2x VACUUM allowance, or both snapshots."""
    return max(3 * market_bytes, market_bytes + economic_bytes) + 1024**3


def require_space(stage_dir: Path, market_bytes: int, economic_bytes: int) -> dict[str, int]:
    stage_dir.mkdir(parents=True, exist_ok=True)
    free = os.statvfs(stage_dir).f_bavail * os.statvfs(stage_dir).f_frsize
    required = required_workspace_bytes(market_bytes, economic_bytes)
    old_economic_copy = Path("/tmp/baibai-old-indicators-v9-readonly.sqlite")
    retained = old_economic_copy.stat().st_size if old_economic_copy.exists() else 0
    if free < required:
        raise CutoverError(
            f"insufficient staging space: free={free}, required={required}, "
            f"retained_old_economic_copy={retained}"
        )
    return {"free_bytes": free, "required_bytes": required, "retained_old_copy_bytes": retained}


def required_compact_workspace_bytes(compact_bytes: int, economic_bytes: int) -> int:
    """Measured compact output, one temporary allowance, economic copy and reserve."""
    return 2 * compact_bytes + economic_bytes + 1024**3


def require_compact_space(
    stage_dir: Path, *, compact_bytes: int, economic_bytes: int
) -> dict[str, int]:
    """After compact staging, reserve the rest before the economic snapshot."""
    free = os.statvfs(stage_dir).f_bavail * os.statvfs(stage_dir).f_frsize
    initial_bound = required_compact_workspace_bytes(compact_bytes, economic_bytes)
    additional = initial_bound - compact_bytes
    if free < additional:
        raise CutoverError(
            f"insufficient compact staging space: free={free}, "
            f"additional_required={additional}, measured_compact={compact_bytes}"
        )
    return {
        "free_bytes": free,
        "required_bytes": additional,
        "initial_bound_bytes": initial_bound,
        "measured_compact_bytes": compact_bytes,
    }


def _require_uploaded(store: MachineStore, key: str, path: Path, digest: str) -> RemoteObject:
    observed = store.head(key)
    if (
        observed is None
        or observed.size != path.stat().st_size
        or observed.metadata.get("sha256") != digest
    ):
        raise CutoverError(f"conditional PUT outcome is unverified: {key}")
    return observed


def _put_reconciled(
    store: MachineStore, key: str, path: Path, *, digest: str, old_etag: str
) -> RemoteObject:
    try:
        store.put_file(
            key,
            path,
            sha256=digest,
            content_md5=_md5(path),
            content_type="application/octet-stream"
            if key.endswith(".sqlite")
            else "application/json",
            if_match=old_etag,
        )
    except Exception:
        # A timeout can follow a committed PUT. Read once; never blind-retry.
        return _require_uploaded(store, key, path, digest)
    return _require_uploaded(store, key, path, digest)


def transition_machine(
    *,
    candidate_market: Path,
    candidate_economic: Path,
    stage_dir: Path,
    mirror: Path,
    store: MachineStore,
    lease: PublicationLeaseHandle,
    release_id: str,
    manifest_sha256: str,
    transition_id: str,
    pin: MachinePin = PRODUCTION_MACHINE,
    source: SourcePin = PRODUCTION_SOURCE,
    prepare_market: Callable[[Path], None] | None = None,
    market_mode: Literal["snapshot_dehydrate", "compact"] = "snapshot_dehydrate",
) -> dict[str, object]:
    """Prepare and conditionally replace only market, economic, and their receipt."""
    if _ETAG.fullmatch(transition_id) is None:
        raise CutoverError("invalid transition identifier")
    if market_mode not in {"snapshot_dehydrate", "compact"}:
        raise CutoverError("unknown market staging mode")
    if market_mode == "compact" and prepare_market is not None:
        raise CutoverError("compact staging cannot use injected market preparation")
    if pin.market_etag != source.market_etag:
        raise CutoverError("machine and lake source pins disagree")
    if read_lake_store_origin(candidate_market) != LakeStoreOrigin(release_id, manifest_sha256):
        raise CutoverError("candidate market origin is not the selected new release")
    candidate_economic_identity = _checkpointed_economic_identity(candidate_economic)
    if validate_snapshot(candidate_market) != 47 or validate_snapshot(candidate_economic) != 12:
        raise CutoverError("candidate machine schemas are not 47 and 12")
    _require_economic_schema(candidate_economic)
    before = {
        MARKET_KEY: _require_head(store, MARKET_KEY, pin.market_etag),
        ECONOMIC_KEY: _require_head(store, ECONOMIC_KEY, pin.economic_etag),
    }
    if (
        before[MARKET_KEY].metadata.get("sha256") != pin.market_sha256
        or before[ECONOMIC_KEY].metadata.get("sha256") != pin.economic_sha256
    ):
        raise CutoverError("old machine content digests differ from pinned copies")
    if store.sqlite_user_version(MARKET_KEY, etag=pin.market_etag) != pin.market_schema:
        raise CutoverError("old market schema differs from pinned schema")
    if store.sqlite_user_version(ECONOMIC_KEY, etag=pin.economic_etag) != pin.economic_schema:
        raise CutoverError("old economic schema differs from pinned schema")
    _require_stage(
        store,
        lease,
        pin,
        source=source,
        release_id=release_id,
        manifest_sha256=manifest_sha256,
        market_etag=pin.market_etag,
        economic_etag=pin.economic_etag,
    )
    if market_mode == "snapshot_dehydrate":
        space = require_space(
            stage_dir, candidate_market.stat().st_size, candidate_economic.stat().st_size
        )
    else:
        stage_dir.mkdir(parents=True, exist_ok=True)
    market_copy = stage_dir / f"{transition_id}-market.sqlite"
    economic_copy = stage_dir / f"{transition_id}-indicators.sqlite"
    if market_copy.exists() or economic_copy.exists():
        raise CutoverError("transition staging files already exist; inspect previous attempt")
    candidate_market_identity = _candidate_hashes(candidate_market)
    if market_mode == "compact":
        pointer = read_pointer_snapshot(store).pointer
        assert pointer is not None
        release = _resolve_serving_release(store, mirror, pointer)

        def still_current() -> tuple[str, str]:
            current = read_pointer_snapshot(store).pointer
            if current is None:
                raise CutoverError("current release disappeared during dehydration")
            return current.release_id, current.manifest_sha256

        compact_report = build_compact_market(
            candidate_market,
            market_copy,
            release=release,
            still_current=still_current,
        )
        compact_bytes = compact_report["output_bytes"]
        if not isinstance(compact_bytes, int):
            raise CutoverError("compact builder did not report an integer output size")
        space = require_compact_space(
            stage_dir,
            compact_bytes=compact_bytes,
            economic_bytes=candidate_economic.stat().st_size,
        )
    else:
        create_snapshot(candidate_market, market_copy)
        if prepare_market is None:
            pointer = read_pointer_snapshot(store).pointer
            assert pointer is not None
            release = _resolve_serving_release(store, mirror, pointer)

            def still_current() -> tuple[str, str]:
                current = read_pointer_snapshot(store).pointer
                if current is None:
                    raise CutoverError("current release disappeared during dehydration")
                return current.release_id, current.manifest_sha256

            dehydrate_market_store(
                market_copy,
                release=release,
                still_current=still_current,
            )
        else:
            prepare_market(market_copy)
    # Dehydrate/VACUUM can require twice the market snapshot's size as scratch.
    # Do not stage the economic copy until that scratch has been released.
    create_snapshot(candidate_economic, economic_copy)
    if validate_snapshot(market_copy) != 47 or validate_snapshot(economic_copy) != 12:
        raise CutoverError("staged machine snapshots failed schema validation")
    _require_economic_schema(economic_copy)
    if read_lake_store_origin(market_copy) != LakeStoreOrigin(release_id, manifest_sha256):
        raise CutoverError("staged market origin changed")
    if (
        _candidate_hashes(candidate_market) != candidate_market_identity
        or _checkpointed_economic_identity(candidate_economic) != candidate_economic_identity
    ):
        raise CutoverError("source candidates changed during staging")
    new = {MARKET_KEY: market_copy, ECONOMIC_KEY: economic_copy}
    digests = {key: _sha(path) for key, path in new.items()}
    backup_keys: dict[str, str] = {}
    for key in (MARKET_KEY, ECONOMIC_KEY):
        _require_stage(
            store,
            lease,
            pin,
            source=source,
            release_id=release_id,
            manifest_sha256=manifest_sha256,
            market_etag=pin.market_etag,
            economic_etag=pin.economic_etag,
        )
        backup = f"cutover/backups/{transition_id}/{key}"
        if store.head(backup) is not None:
            raise CutoverError(f"backup key already exists: {backup}")
        with suppress(Exception):
            store.copy_if_match(key, backup, etag=before[key].etag)
        # A committed copy can time out; read back its exact identity below.
        saved = store.head(backup)
        if (
            saved is None
            or saved.size != before[key].size
            or saved.metadata.get("sha256")
            != (pin.market_sha256 if key == MARKET_KEY else pin.economic_sha256)
        ):
            raise CutoverError(f"old machine backup is unverified: {key}")
        backup_keys[key] = backup
    _require_stage(
        store,
        lease,
        pin,
        source=source,
        release_id=release_id,
        manifest_sha256=manifest_sha256,
        market_etag=pin.market_etag,
        economic_etag=pin.economic_etag,
    )
    market_new = _put_reconciled(
        store, MARKET_KEY, market_copy, digest=digests[MARKET_KEY], old_etag=pin.market_etag
    )
    _require_stage(
        store,
        lease,
        pin,
        source=source,
        release_id=release_id,
        manifest_sha256=manifest_sha256,
        market_etag=market_new.etag,
        economic_etag=pin.economic_etag,
    )
    economic_new = _put_reconciled(
        store, ECONOMIC_KEY, economic_copy, digest=digests[ECONOMIC_KEY], old_etag=pin.economic_etag
    )
    _require_stage(
        store,
        lease,
        pin,
        source=source,
        release_id=release_id,
        manifest_sha256=manifest_sha256,
        market_etag=market_new.etag,
        economic_etag=economic_new.etag,
    )
    keys = {MARKET_KEY: market_new.etag, RUNS_KEY: pin.runs_etag, ECONOMIC_KEY: economic_new.etag}
    receipt = {
        "schema_version": 1,
        "written_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "keys": keys,
    }
    receipt_path = stage_dir / f"{transition_id}-machine-manifest.json"
    with receipt_path.open("x", encoding="utf-8") as target:
        json.dump(receipt, target, sort_keys=True)
        target.write("\n")
    receipt_sha = _sha(receipt_path)
    _require_stage(
        store,
        lease,
        pin,
        source=source,
        release_id=release_id,
        manifest_sha256=manifest_sha256,
        market_etag=market_new.etag,
        economic_etag=economic_new.etag,
    )
    _put_reconciled(store, RECEIPT_KEY, receipt_path, digest=receipt_sha, old_etag=pin.receipt_etag)
    if store.get_bytes(RECEIPT_KEY) != receipt_path.read_bytes():
        raise CutoverError("new machine receipt readback differs")
    _require_head(store, RUNS_KEY, pin.runs_etag)
    _require_head(store, APPLICATION_KEY, pin.application_etag)
    return {
        "transition_id": transition_id,
        "backup_keys": backup_keys,
        "new_etags": keys,
        "space": space,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-market", type=Path, required=True)
    parser.add_argument("--candidate-economic", type=Path, required=True)
    parser.add_argument("--stage-dir", type=Path, required=True)
    parser.add_argument("--mirror", type=Path, required=True)
    parser.add_argument("--lease-handle", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--transition-id", required=True)
    parser.add_argument(
        "--market-mode", choices=("snapshot_dehydrate", "compact"), default="snapshot_dehydrate"
    )
    args = parser.parse_args()
    os.umask(0o077)
    result = transition_machine(
        candidate_market=args.candidate_market,
        candidate_economic=args.candidate_economic,
        stage_dir=args.stage_dir,
        mirror=args.mirror,
        store=R2MachineStore(bucket="baibai-stores"),
        lease=_load_handle(args.lease_handle),
        release_id=args.release_id,
        manifest_sha256=args.manifest_sha256,
        transition_id=args.transition_id,
        market_mode=args.market_mode,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
