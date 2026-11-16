"""One-time CAS cutover; never run from normal daily or change runs/pointer.

A failed PUT is not retried. A separate finish-manifest invocation may complete
only the exact prepared market with the original runs, pointer and old receipt.
Retire this tool with the schema-52 conversion after accepted production readback.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from tools.migrations.market_schema_52 import digest, verify_copy

from baibai_engine.storage.objects.r2 import (
    Boto3R2Store,
    ObjectStore,
    R2OperationStopped,
    VerifiedObjectReader,
)
from baibai_engine.storage.sqlite.source_cache.lake_origin import read_lake_store_origin
from baibai_jobs.publication.lease import PublicationLeaseHandle, _load_handle, verify_held

MARKET = "market.sqlite"
RUNS = "runs.sqlite"
MANIFEST = "machine-manifest.json"
POINTER = "lake/pointers/market/current.json"
MAX_MARKET_BYTES = 64 * 1024**2
# The first core generation and pair readback must stay inside this same lease.
MIN_DAILY_REMAINING = timedelta(minutes=180)


def _require_publish_time(handle: PublicationLeaseHandle) -> None:
    if handle.expires_at_utc - datetime.now(UTC) < MIN_DAILY_REMAINING:
        raise ValueError("insufficient daily lease time before schema cutover")


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    format: Literal["market-schema-52-cutover-v1"]
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    target_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    market_etag: str = Field(min_length=1, max_length=256)
    runs_etag: str = Field(min_length=1, max_length=256)
    manifest_etag: str = Field(min_length=1, max_length=256)
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    pointer_etag: str = Field(min_length=1, max_length=256)
    pointer_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    written_at_utc: datetime

    @field_validator("written_at_utc")
    @classmethod
    def utc_clock(cls, value: datetime) -> datetime:
        if value.utcoffset() != UTC.utcoffset(None):
            raise ValueError("cutover receipt clock must be UTC")
        return value


def _read(
    store: ObjectStore, key: str, *, sha256: str, max_bytes: int, etag: str | None = None
) -> tuple[bytes, str]:
    before = store.head(key)
    if before is None or not 0 < before.size <= max_bytes or (etag and before.etag != etag):
        raise ValueError(f"fixed remote object changed or exceeds bound: {key}")
    if not isinstance(store, VerifiedObjectReader):
        raise ValueError("cutover requires bounded verified object reads")
    body = store.get_verified_bytes(key, sha256=sha256, max_bytes=max_bytes)
    after = store.head(key)
    if body is None or after != before or len(body) != before.size:
        raise ValueError(f"fixed remote read was not stable: {key}")
    return body, before.etag


def _put(store: ObjectStore, key: str, path: Path, *, etag: str | None, expected_sha: str) -> str:
    if digest(path) != expected_sha:
        raise ValueError("prepared upload bytes changed")
    with path.open("rb") as stream:
        checksum = base64.b64encode(
            hashlib.file_digest(stream, lambda: hashlib.md5(usedforsecurity=False)).digest()
        ).decode()
    answer = store.put_file(
        key,
        path,
        sha256=expected_sha,
        content_md5=checksum,
        content_type="application/octet-stream" if key.endswith(".sqlite") else "application/json",
        if_match=etag,
        if_none_match=etag is None,
    )
    return answer.etag


def _boundary(store: ObjectStore, plan: Plan, handle: PublicationLeaseHandle) -> bytes:
    verify_held(store, handle)
    runs = store.head(RUNS)
    if runs is None or runs.etag != plan.runs_etag:
        raise ValueError("runs object changed; do not publish or repair the receipt")
    pointer, _ = _read(
        store, POINTER, sha256=plan.pointer_sha256, max_bytes=16_384, etag=plan.pointer_etag
    )
    return pointer


def _backup_key(kind: str, checksum: str) -> str:
    return f"maintenance/market-schema-52/{kind}/{checksum}" + (
        ".sqlite" if kind == "source" else ".json"
    )


def _backup(
    store: ObjectStore, path: Path, *, kind: str, expected_sha: str, record: Callable[[str], None]
) -> None:
    if digest(path) != expected_sha:
        raise ValueError("prepared backup bytes changed")
    key = _backup_key(kind, expected_sha)
    if store.head(key) is None:
        record("backup_" + kind + "_put_started")
        _put(store, key, path, etag=None, expected_sha=expected_sha)
    _read(store, key, sha256=expected_sha, max_bytes=MAX_MARKET_BYTES)
    record("backup_" + kind + "_verified")


def cutover(
    store: ObjectStore,
    *,
    plan: Plan,
    source: Path,
    target: Path,
    handle: PublicationLeaseHandle,
    attempt: Path,
    mode: Literal["publish", "finish-manifest"],
) -> dict[str, object]:
    if mode not in {"publish", "finish-manifest"}:
        raise ValueError("unknown cutover operation")
    purposes = {"daily"} if mode == "publish" else {"operator", "daily"}
    if handle.purpose not in purposes or handle.protocol_version != 2:
        raise ValueError("cutover requires its own current lease for this operation")
    if mode == "publish" and handle.expires_at_utc - handle.acquired_at_utc != timedelta(
        minutes=220
    ):
        raise ValueError("schema cutover requires the current daily lease duration")
    if attempt.exists() or attempt.is_symlink():
        raise ValueError("a cutover attempt must never be overwritten")
    if any(not 0 < p.stat().st_size <= MAX_MARKET_BYTES for p in (source, target)):
        raise ValueError("cutover requires bounded prepared machine copies")
    verify_copy(source, target, source_sha256=plan.source_sha256, target_sha256=plan.target_sha256)
    attempt.mkdir(parents=True, mode=0o700)
    journal = attempt / "events.jsonl"

    def record(phase: str) -> None:
        with journal.open("a") as stream:
            stream.write(json.dumps({"phase": phase, "at": datetime.now(UTC).isoformat()}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    (attempt / "plan.json").write_text(plan.model_dump_json(indent=2) + "\n")
    record("verified_prepared_copy")
    try:
        pointer = _boundary(store, plan, handle)
        if mode == "publish":
            _require_publish_time(handle)
        origin = read_lake_store_origin(source)
        root = json.loads(pointer)
        if origin is None or (origin.release_id, origin.release_manifest_sha256) != (
            root["release_id"],
            root["manifest_sha256"],
        ):
            raise ValueError("prepared source origin differs from the fixed pointer")
        old_manifest_path = attempt / "old-manifest.json"
        pointer_path = attempt / "old-pointer.json"
        pointer_path.write_bytes(pointer)
        if mode == "publish":
            manifest, _ = _read(
                store,
                MANIFEST,
                sha256=plan.manifest_sha256,
                max_bytes=16_384,
                etag=plan.manifest_etag,
            )
            old = json.loads(manifest)
            if old.get("schema_version") != 1 or old.get("keys") != {
                MARKET: plan.market_etag,
                RUNS: plan.runs_etag,
            }:
                raise ValueError("source machine receipt differs from the planned pair")
            old_manifest_path.write_bytes(manifest)
            _read(
                store,
                MARKET,
                sha256=plan.source_sha256,
                max_bytes=MAX_MARKET_BYTES,
                etag=plan.market_etag,
            )
            for path, kind, checksum in (
                (source, "source", plan.source_sha256),
                (old_manifest_path, "manifest", plan.manifest_sha256),
                (pointer_path, "pointer", plan.pointer_sha256),
            ):
                _backup(store, path, kind=kind, expected_sha=checksum, record=record)
            _boundary(store, plan, handle)
            _require_publish_time(handle)
            record("market_put_started")
            market_etag = _put(
                store, MARKET, target, etag=plan.market_etag, expected_sha=plan.target_sha256
            )
            (attempt / "market-put-etag.json").write_text(json.dumps(market_etag) + "\n")
            record("market_put_acknowledged")
        else:
            # Explicit recovery reads the intended body, never retries or restores market.
            _, market_etag = _read(
                store, MARKET, sha256=plan.target_sha256, max_bytes=MAX_MARKET_BYTES
            )
            for kind, checksum in (
                ("source", plan.source_sha256),
                ("manifest", plan.manifest_sha256),
                ("pointer", plan.pointer_sha256),
            ):
                _read(
                    store, _backup_key(kind, checksum), sha256=checksum, max_bytes=MAX_MARKET_BYTES
                )
            record("exact_target_market_and_backups_verified_for_finish")
        expected = json.dumps(
            {
                "schema_version": 1,
                "written_at_utc": plan.written_at_utc.isoformat(),
                "keys": {MARKET: market_etag, RUNS: plan.runs_etag},
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        manifest_path = attempt / "target-manifest.json"
        manifest_path.write_bytes(expected)
        expected_sha = hashlib.sha256(expected).hexdigest()
        _boundary(store, plan, handle)
        _read(
            store, MARKET, sha256=plan.target_sha256, max_bytes=MAX_MARKET_BYTES, etag=market_etag
        )
        current = store.head(MANIFEST)
        if current is None:
            raise ValueError("machine receipt disappeared")
        if mode == "finish-manifest" and current.etag != plan.manifest_etag:
            _read(store, MANIFEST, sha256=expected_sha, max_bytes=16_384, etag=current.etag)
            record("target_manifest_already_verified")
        else:
            original, _ = _read(
                store,
                MANIFEST,
                sha256=plan.manifest_sha256,
                max_bytes=16_384,
                etag=plan.manifest_etag,
            )
            old = json.loads(original)
            if old.get("schema_version") != 1 or old.get("keys") != {
                MARKET: plan.market_etag,
                RUNS: plan.runs_etag,
            }:
                raise ValueError("old receipt no longer names the original pair")
            old_manifest_path.write_bytes(original)
            _boundary(store, plan, handle)
            record("manifest_put_started")
            _put(store, MANIFEST, manifest_path, etag=plan.manifest_etag, expected_sha=expected_sha)
            record("manifest_put_acknowledged")
        _boundary(store, plan, handle)
        _read(
            store, MARKET, sha256=plan.target_sha256, max_bytes=MAX_MARKET_BYTES, etag=market_etag
        )
        _read(store, MANIFEST, sha256=expected_sha, max_bytes=16_384)
        _boundary(store, plan, handle)
        record("verified_market_and_receipt_with_unchanged_runs_and_pointer")
        result: dict[str, object] = {
            "status": "verified_cutover",
            "mode": mode,
            "market_etag": market_etag,
            "runs_etag": plan.runs_etag,
            "manifest_sha256": expected_sha,
            "runs_and_pointer_written": False,
        }
        (attempt / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        return result
    except BaseException as exc:
        record("stopped_without_retry:" + type(exc).__name__)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("publish", "finish-manifest"))
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--lease-handle", type=Path, required=True)
    parser.add_argument("--attempt", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        plan = Plan.model_validate_json(args.plan.read_bytes())
        handle = _load_handle(args.lease_handle)
        env = dict(os.environ)
        env.update(R2_STOP_ON_REJECTION="1")
        with closing(
            Boto3R2Store(
                bucket=env.get("R2_STORES_BUCKET") or "baibai-stores",
                env=env,
                verify_put_with_head=False,
            )
        ) as store:
            result = cutover(
                store,
                plan=plan,
                source=args.source,
                target=args.target,
                handle=handle,
                attempt=args.attempt,
                mode=args.mode,
            )
        print(json.dumps(result))
        return 0
    except (Exception, R2OperationStopped) as exc:
        print(
            f"schema cutover stopped ({type(exc).__name__}); inspect retained attempt",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
