"""一回限りの経済schema 12→13公開。通常mergeは変更せず、L2を要求しない。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import uuid
from collections.abc import Callable
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.receipts.native_sources.store import (
    RECEIPT_KEYS,
    validate_current_schema,
)
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import (
    Boto3R2Store,
    LakePublishError,
    ObjectStore,
    RemoteObject,
)
from baibai_engine.storage.snapshots.sources import sha256_file
from baibai_engine.storage.sqlite.snapshot import create_snapshot, validate_database
from baibai_jobs.maintenance.migrate_economic_store import migrate
from baibai_jobs.publication.lease import _load_handle, verify_held

FORMAT = "economic-cutover-12-13-v1"
KEY = "indicators.sqlite"
MANIFEST = "machine-manifest.json"
BUNDLE_KEYS = ("market.sqlite", "runs.sqlite", KEY)


def _load(receipt: Path) -> dict[str, Any]:
    value = json.loads(receipt.read_bytes())
    if value.get("format") != FORMAT:
        raise ValueError("requires an economic schema12→13 cutover receipt")
    return dict(value)


def _closed_object(path: Path) -> None:
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-journal")):
        raise ValueError("cutover object must be a closed SQLite file without pending sidecars")


def prepare_copy(source: Path, output: Path, receipt: Path, generation_directory: Path) -> None:
    """Bind a separate converted copy to the exact downloaded schema12 object."""
    if receipt.exists():
        raise FileExistsError(receipt)
    # The base is an immutable downloaded object, not an active SQLite writer.
    _closed_object(source)
    generations = {
        key: (generation_directory / f"{key}.etag").read_text().strip().strip('"')
        for key in BUNDLE_KEYS
    }
    if not all(generations.values()):
        raise ValueError("cutover requires all three fresh machine generations")
    validate_database(source)
    digest = sha256_file(source)
    migrate(source, output)
    if sha256_file(source) != digest:
        raise ValueError("cutover base changed during conversion")
    write_json_atomic(
        receipt,
        {
            "format": FORMAT,
            "source": str(source.resolve()),
            "output": str(output.resolve()),
            "source_sha256": digest,
            "base_generations": generations,
        },
    )


def verify_copy(base: Path, candidate: Path) -> dict[str, int]:
    """Retain every old fact verbatim; validate only actual new confirmation anchors."""
    _closed_object(base)
    _closed_object(candidate)
    validate_database(candidate)
    with closing(sqlite3.connect(candidate.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        validate_current_schema(conn)
        conn.execute("ATTACH DATABASE ? AS source", (base.resolve().as_uri() + "?mode=ro",))
        if conn.execute("PRAGMA source.user_version").fetchone()[0] != 12:
            raise ValueError("cutover base is not schema12")
        for table in RECEIPT_KEYS:
            # Fixed owner allowlist, including original runs and artificial empty rows.
            if conn.execute(
                f"SELECT 1 FROM (SELECT * FROM source.{table} "  # nosec B608
                f"EXCEPT SELECT * FROM main.{table}) LIMIT 1"  # nosec B608
            ).fetchone():
                raise ValueError(f"cutover candidate lost or changed historical facts: {table}")
        for source_id, day, checked, adopted, digest in conn.execute(
            "SELECT * FROM economic_source_record_confirmations"
        ):
            anchor = conn.execute(
                "SELECT payload FROM economic_source_records "
                "WHERE source_id=? AND observed_at=? AND fetched_at=?",
                (source_id, day, adopted),
            ).fetchone()
            if (
                checked < adopted
                or anchor is None
                or hashlib.sha256(anchor[0]).hexdigest() != digest
            ):
                raise ValueError("cutover confirmation differs from its adopted receipt")
        missing = conn.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT source_id,observed_at "
            "FROM economic_source_records WHERE record_count>0 EXCEPT "
            "SELECT source_id,observed_at FROM economic_source_record_confirmations)"
        ).fetchone()[0]
    return {"unconfirmed_native_periods": missing}


def freeze(receipt: Path) -> dict[str, int]:
    """Freeze one upload before network work; never replace a pending candidate."""
    value = _load(receipt)
    base = Path(value["source"])
    if sha256_file(base) != value["source_sha256"]:
        raise ValueError("cutover base differs from the downloaded object")
    if "candidate" in value:
        candidate = Path(value["candidate"]["path"])
        if sha256_file(candidate) != value["candidate"]["sha256"]:
            raise ValueError("frozen cutover upload changed")
        return verify_copy(base, candidate)
    candidate = receipt.with_name(receipt.name + ".upload.sqlite").resolve()
    if candidate.exists():
        raise FileExistsError("unrecorded cutover upload exists; inspect it before freezing")
    with tempfile.TemporaryDirectory(prefix=".economic-cutover-", dir=receipt.parent) as tmp:
        snapshot = Path(tmp) / "upload.sqlite"
        create_snapshot(Path(value["output"]), snapshot)
        report = verify_copy(base, snapshot)
        os.link(snapshot, candidate)  # Create-only: never replace an earlier frozen upload.
    value["candidate"] = {"path": str(candidate), "sha256": sha256_file(candidate)}
    write_json_atomic(receipt, value)
    return report


def _read(store: ObjectStore, key: str, path: Path) -> RemoteObject:
    before = store.head(key)
    if before is None:
        raise ValueError(f"cutover object missing: {key}")
    store.download_file(key, path, expect_bytes=before.size)
    after = store.head(key)
    if after is None or after.etag != before.etag or path.stat().st_size != before.size:
        raise ValueError(f"cutover object changed during readback: {key}")
    return after


def _unchanged_peers(store: ObjectStore, expected: dict[str, str]) -> None:
    for key in BUNDLE_KEYS[:-1]:
        remote = store.head(key)
        if remote is None or remote.etag != expected[key]:
            raise ValueError(f"cutover peer changed: {key}; keep work and inspect the bundle")


def _publish_exact(
    store: ObjectStore,
    key: str,
    candidate: Path,
    *,
    base_etag: str,
    base_sha256: str,
    readback: Path,
    content_type: str,
    before_put: Callable[[], None],
) -> str:
    """Resolve uncertain PUTs by bytes, never metadata or an assumed success."""
    digest = sha256_file(candidate)
    remote = _read(store, key, readback)
    actual = sha256_file(readback)
    if actual == digest:
        return remote.etag
    if remote.etag != base_etag or actual != base_sha256:
        raise ValueError(f"cutover object is neither base nor frozen candidate: {key}")
    with candidate.open("rb") as stream:
        checksum = hashlib.file_digest(stream, lambda: hashlib.md5(usedforsecurity=False)).digest()
    # Downloads and hashing can outlast a lease. Check ownership immediately
    # before each new PUT, rather than carrying authority across a long read.
    before_put()
    # The attempt may succeed without an acknowledgement; always read the same key.
    with suppress(LakePublishError):
        store.put_file(
            key,
            candidate,
            sha256=digest,
            content_md5=base64.b64encode(checksum).decode(),
            content_type=content_type,
            if_match=base_etag,
        )
    remote = _read(store, key, readback)
    if sha256_file(readback) != digest:
        raise ValueError(f"cutover PUT not confirmed: {key}; resume with the same receipt")
    return remote.etag


def publish(store: ObjectStore, receipt: Path, lease_handle: Path) -> dict[str, Any]:
    """CAS indicators, read it back, then CAS/read back the existing bundle receipt."""
    value = _load(receipt)
    handle = _load_handle(lease_handle)
    verify_held(store, handle)
    candidate = Path(value["candidate"]["path"])
    if sha256_file(candidate) != value["candidate"]["sha256"]:
        raise ValueError("frozen cutover upload changed")
    base = Path(value["source"])
    if sha256_file(base) != value["source_sha256"]:
        raise ValueError("cutover base changed")
    report: dict[str, Any] = verify_copy(base, candidate)
    generations = value["base_generations"]

    def before_put() -> None:
        _unchanged_peers(store, generations)
        verify_held(store, handle)

    _unchanged_peers(store, generations)
    scratch = receipt.with_name(receipt.name + ".readback")
    manifest_readback = receipt.with_name(receipt.name + ".manifest-readback.json")
    remote_manifest = _read(store, MANIFEST, manifest_readback)
    if "publication" not in value:
        manifest = json.loads(manifest_readback.read_bytes())
        if manifest.get("schema_version") != 1 or manifest.get("keys") != generations:
            raise ValueError("fresh machine manifest differs from cutover base generations")
        value["publication"] = {
            "manifest_etag": remote_manifest.etag,
            "manifest_sha256": sha256_file(manifest_readback),
        }
        # Persist intent before the first remote write. Keep pulled ETags unchanged.
        write_json_atomic(receipt, value)
    publication = value["publication"]
    # Do not change indicators if an unrelated manifest replaced our base.
    current_manifest_sha = sha256_file(manifest_readback)
    allowed = {publication["manifest_sha256"]}
    if "manifest_candidate" in publication:
        allowed.add(publication["manifest_candidate"]["sha256"])
    if current_manifest_sha not in allowed:
        raise ValueError("machine manifest changed outside this cutover")
    verify_held(store, handle)
    indicator_etag = _publish_exact(
        store,
        KEY,
        candidate,
        base_etag=generations[KEY],
        base_sha256=value["source_sha256"],
        readback=scratch,
        content_type="application/vnd.sqlite3",
        before_put=before_put,
    )
    _unchanged_peers(store, generations)
    if "manifest_candidate" not in publication:
        manifest_path = receipt.with_name(receipt.name + ".manifest.json").resolve()
        write_json_atomic(
            manifest_path,
            {
                "schema_version": 1,
                "written_at_utc": datetime.now(UTC).isoformat(),
                "keys": {**generations, KEY: indicator_etag},
            },
        )
        publication["indicator_etag"] = indicator_etag
        publication["manifest_candidate"] = {
            "path": str(manifest_path),
            "sha256": sha256_file(manifest_path),
        }
        write_json_atomic(receipt, value)
    manifest_candidate = publication["manifest_candidate"]
    manifest_path = Path(manifest_candidate["path"])
    if (
        publication["indicator_etag"] != indicator_etag
        or sha256_file(manifest_path) != manifest_candidate["sha256"]
    ):
        raise ValueError("cutover confirmed upload generation or manifest candidate changed")
    verify_held(store, handle)
    _unchanged_peers(store, generations)
    manifest_etag = _publish_exact(
        store,
        MANIFEST,
        manifest_path,
        base_etag=publication["manifest_etag"],
        base_sha256=publication["manifest_sha256"],
        readback=manifest_readback,
        content_type="application/json",
        before_put=before_put,
    )
    _unchanged_peers(store, generations)
    current = store.head(KEY)
    if current is None or current.etag != indicator_etag:
        raise ValueError("cutover indicators changed after manifest publication")
    verify_held(store, handle)
    value["completed"] = {KEY: indicator_etag, MANIFEST: manifest_etag}
    write_json_atomic(receipt, value)
    return {**report, "readback_verified": True, "generations": value["completed"]}


class _InputStore:
    """Keep the existing input exporter, checking cutover ownership at every PUT."""

    def __init__(self, store: ObjectStore, before_put: Callable[[], None]) -> None:
        self.store, self.before_put = store, before_put

    def head(self, key: str) -> RemoteObject | None:
        return self.store.head(key)

    def get_bytes(self, key: str) -> bytes:
        return self.store.get_bytes(key)

    def download_file(self, key: str, path: Path, *, expect_bytes: int | None = None) -> None:
        self.store.download_file(key, path, expect_bytes=expect_bytes)

    def put_file(
        self,
        key: str,
        path: Path,
        *,
        sha256: str,
        content_md5: str,
        content_type: str,
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> RemoteObject:
        self.before_put()
        return self.store.put_file(
            key,
            path,
            sha256=sha256,
            content_md5=content_md5,
            content_type=content_type,
            if_match=if_match,
            if_none_match=if_none_match,
        )


def reissue_inputs(store: ObjectStore, receipt: Path, lease_handle: Path) -> dict[str, object]:
    """Reissue every economic scoped input from verified L1, without a lake/L2 read."""
    from baibai_engine.data_collection.acquisition.policy import acquisition_groups
    from baibai_jobs.acquisition.source_inputs import load_input, publish_inputs

    published = publish(store, receipt, lease_handle)
    value = _load(receipt)
    handle = _load_handle(lease_handle)
    candidate = Path(value["candidate"]["path"])
    generations = {"economic_etag": published["generations"][KEY]}

    def before_put() -> None:
        _unchanged_peers(store, value["base_generations"])
        current = store.head(KEY)
        if current is None or current.etag != generations["economic_etag"]:
            raise ValueError("canonical economic generation changed before input publication")
        verify_held(store, handle)

    input_store = _InputStore(store, before_put)
    updated = []
    # New attempt identities let an interrupted immutable handoff retry without
    # overwriting its earlier manifest. The existing input publisher reuses bytes.
    identity = "cutover-" + uuid.uuid4().hex
    for group in acquisition_groups():
        verify_held(store, handle)
        name = "economic-" + group
        report = publish_inputs(
            input_store,
            market=Path("unused-economic-cutover-market"),
            economic=candidate,
            identity=identity,
            asof=datetime.now(UTC).date(),
            generations=generations,
            only_group=name,
        )
        if report["failed"]:
            raise ValueError(f"cutover scoped input failed: {name}; retry reissue-inputs")
        with tempfile.TemporaryDirectory(
            prefix="economic-cutover-input-", dir=receipt.parent
        ) as tmp:
            directory = Path(tmp)
            database = directory / "input.sqlite"
            result = load_input(store, name, database, directory / "readback")
            with closing(
                sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
            ) as conn:
                validate_current_schema(conn)
            if result["canonical_generations"] != generations:
                raise ValueError("cutover scoped input generation changed during readback")
        updated.append(name)
    # A replaced canonical object invalidates the inputs, even if all uploads worked.
    publish(store, receipt, lease_handle)
    value["inputs_verified"] = {"economic_etag": generations["economic_etag"], "groups": updated}
    write_json_atomic(receipt, value)
    return {"updated": updated, "readback_verified": True}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare-copy", "freeze", "publish", "reissue-inputs"))
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--generation-directory",
        type=Path,
        default=Path(os.environ.get("R2_GENERATION_DIR") or "stores/.r2-generations"),
    )
    parser.add_argument("--lease-handle", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare-copy":
            if args.source is None or args.output is None:
                raise ValueError("--source and --output are required")
            prepare_copy(args.source, args.output, args.receipt, args.generation_directory)
            report: dict[str, Any] = {"copy_created": True, "published": False}
        elif args.command == "freeze":
            report = freeze(args.receipt)
        else:
            if args.lease_handle is None:
                raise ValueError("--lease-handle is required")
            store = Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
            try:
                report = (
                    publish(store, args.receipt, args.lease_handle)
                    if args.command == "publish"
                    else reissue_inputs(store, args.receipt, args.lease_handle)
                )
            finally:
                store.close()
        print(json.dumps(report, sort_keys=True))
        return 0
    except Exception as error:
        print(
            f"economic cutover failed: {type(error).__name__}; keep receipt and frozen files",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
