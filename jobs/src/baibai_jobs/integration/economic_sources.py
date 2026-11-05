"""経済L1を単独正本へ確定し、L2を待たず次回取得inputを発行する。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import uuid
from collections.abc import Callable
from contextlib import closing, suppress
from datetime import date
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.acquisition.policy import acquisition_groups
from baibai_engine.data_collection.providers.tradingview.market_models import MARKET_GROUP
from baibai_engine.data_collection.receipts.native_sources.confirmations import (
    validate_collection_confirmations,
)
from baibai_engine.data_collection.receipts.native_sources.handoff import accept_rechecks
from baibai_engine.data_collection.receipts.native_sources.store import (
    DEFAULT_DB_PATH,
    EconomicSchemaError,
    validate_current_schema,
)
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import (
    Boto3R2Store,
    LakeCASConflict,
    LakePublishError,
    ObjectStore,
    R2Error,
    RemoteObject,
)
from baibai_engine.storage.snapshots.sources import sha256_file
from baibai_engine.storage.sqlite.merge import MergeError
from baibai_engine.storage.sqlite.snapshot import create_snapshot
from baibai_jobs.acquisition.market_context import validate_packet
from baibai_jobs.acquisition.receipts import (
    ReceiptStore,
    available_handoffs,
    download_fixed,
    seal_integrated,
)
from baibai_jobs.acquisition.source_inputs import GuardedInputStore, load_input, publish_inputs
from baibai_jobs.integration.economic_handoff import validate_handoff
from baibai_jobs.integration.native_source_merge import merge_stores, verify_stores
from baibai_jobs.publication.lease import _load_handle, verify_held

KEY = "indicators.sqlite"


def _read(store: ObjectStore, path: Path, *, key: str = KEY) -> RemoteObject:
    before = store.head(key)
    if before is None:
        raise ValueError("economic canonical is missing; provision it explicitly")
    store.download_file(key, path, expect_bytes=before.size)
    after = store.head(key)
    if after is None or after.etag != before.etag or path.stat().st_size != before.size:
        raise LakePublishError("economic canonical changed during download")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        validate_current_schema(conn)
        validate_collection_confirmations(conn)
    return after


def _put_readback(
    store: ObjectStore,
    key: str,
    path: Path,
    readback: Path,
    *,
    expected: str | None,
    before_put: Callable[[], None],
) -> RemoteObject:
    digest = sha256_file(path)
    with path.open("rb") as stream:
        md5 = hashlib.file_digest(stream, lambda: hashlib.md5(usedforsecurity=False)).digest()
    before_put()
    with suppress(LakePublishError):
        store.put_file(
            key,
            path,
            sha256=digest,
            content_md5=base64.b64encode(md5).decode(),
            content_type="application/vnd.sqlite3",
            if_match=expected,
            if_none_match=expected is None,
        )
    remote = _read(store, readback, key=key)
    if sha256_file(readback) != digest:
        raise LakePublishError(f"economic PUT is not confirmed: {key}; keep original handoffs")
    return remote


def integrate(
    store: ReceiptStore,
    directory: Path,
    *,
    database: Path,
    asof: date,
    lease_handle: Path,
    log_publication: bool = False,
) -> dict[str, Any]:
    """Resume from originals; deduplicated unsealed rechecks may need explicit review."""
    if database.exists():
        raise FileExistsError("economic integration requires a fresh execution output")
    handle = _load_handle(lease_handle)
    verify_held(store, handle)
    # A new attempt cannot collide with an interrupted input's immutable manifest.
    identity = uuid.uuid4().hex
    work = directory / identity
    work.mkdir(parents=True, exist_ok=False)
    entries = available_handoffs(store, market=False)
    write_json_atomic(work / "plan.json", {"handoffs": entries})
    base = work / "base.sqlite"
    original = _read(store, base)
    base_digest = sha256_file(base)
    canonical = work / "canonical.sqlite"
    shutil.copyfile(base, canonical)
    outcomes: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        packet = work / f"receipt-{index:05d}"
        result: dict[str, Any] = {}
        applied, failed = False, False
        if entry.get("invalid") or entry.get("late_before_integrated"):
            result["reason"] = (
                "invalid_handoff"
                if entry.get("invalid")
                else "older_than_integrated_source_watermark"
            )
            failed = True
        else:
            try:
                download_fixed(store, entry, packet)
                group = str(entry["source"]).removeprefix("economic-")
                try:
                    if group == MARKET_GROUP:
                        validate_packet(packet, canonical=canonical)
                    else:
                        validate_handoff(packet, group)
                except sqlite3.DatabaseError as error:
                    raise ValueError("invalid economic source SQLite receipt") from error
                accepted = packet / "accepted.sqlite"
                result = accept_rechecks(packet / "receipts.sqlite", canonical, accepted)
                merge_stores(accepted, canonical)
                applied = True
                failed = bool(result["late_recovery_required"])
                source_report = json.loads((packet / "result.json").read_text())
                result["acquisition_failures"] = [
                    {
                        "source_id": row["source_id"],
                        "status": "failed",
                        **({"http_status": row["http_status"]} if "http_status" in row else {}),
                    }
                    for row in source_report["sources"]
                    if row.get("status") == "failed"
                ]
                result["acquisition_issues"] = [
                    row["source_id"] + ":history_recovery_required"
                    for row in source_report["sources"]
                    if row.get("history_recovery_required") is True
                ]
            except (R2Error, LakeCASConflict):
                raise
            except (
                KeyError,
                TypeError,
                ValueError,
                LakePublishError,
                MergeError,
                EconomicSchemaError,
                sqlite3.IntegrityError,
            ) as error:
                result = {"reason": type(error).__name__}
                failed = True
        outcomes.append(
            {
                "entry": entry,
                "result": result,
                "applied": applied,
                "failed": failed,
                "recovery": failed,
            }
        )
    report: dict[str, Any] = {
        "asof": asof.isoformat(),
        "handoffs": outcomes,
        "extractions": {},
        "external": None,
        "external_failed": [],
        "l1_published": False,
    }
    write_json_atomic(directory / "applied.json", report)
    candidate = canonical
    if any(outcome["applied"] for outcome in outcomes):
        candidate = work / "candidate.sqlite"
        create_snapshot(canonical, candidate)
    digest = sha256_file(candidate)

    def before_canonical_put() -> None:
        verify_held(store, handle)
        current = store.head(KEY)
        if current is None or current.etag != original.etag:
            raise LakeCASConflict("economic canonical changed before publication")

    before_canonical_put()
    readback = work / "readback.sqlite"
    if digest != base_digest:
        backup = store.head(KEY + ".bak")
        _put_readback(
            store,
            KEY + ".bak",
            base,
            work / "backup-readback.sqlite",
            expected=backup.etag if backup else None,
            before_put=before_canonical_put,
        )
        published = _put_readback(
            store,
            KEY,
            candidate,
            readback,
            expected=original.etag,
            before_put=before_canonical_put,
        )
    else:
        published = _read(store, readback)
        if sha256_file(readback) != digest:
            raise LakeCASConflict("economic canonical changed before readback")
    generations = {"economic_etag": published.etag}
    report.update(l1_published=True, canonical_generations=generations)
    write_json_atomic(directory / "applied.json", report)
    if log_publication:
        # Emit only this actual readback, never a previous attempt's local report.
        print(json.dumps(report, sort_keys=True), flush=True)
    for index, outcome in enumerate(outcomes):
        if outcome["applied"]:
            verify_stores(work / f"receipt-{index:05d}" / "accepted.sqlite", readback)

    def before_put() -> None:
        verify_held(store, handle)
        current = store.head(KEY)
        if current is None or current.etag != published.etag:
            raise LakeCASConflict("economic canonical changed before source-state publication")

    before_put()
    # L2 receives these verified bytes, never an empty fallback or another latest read.
    database.parent.mkdir(parents=True, exist_ok=True)
    create_snapshot(readback, database)
    guarded = GuardedInputStore(store, before_put)
    # Ordered sealing: do not pass a failed write and advance the same source watermark.
    for outcome in outcomes:
        if not outcome["entry"].get("invalid"):
            seal_integrated(
                guarded, outcome["entry"], generations=generations, recovery=outcome["recovery"]
            )
    report["l1_sealed"] = True
    write_json_atomic(directory / "applied.json", report)
    updated = []
    # Always reissue, even with no pending handoffs after an interrupted prior cycle.
    for group in acquisition_groups():
        name = "economic-" + group
        result = publish_inputs(
            guarded,
            market=work / "unused-market",
            economic=readback,
            identity=identity,
            asof=asof,
            generations=generations,
            only_group=name,
        )
        if result["failed"]:
            raise ValueError(f"economic scoped input failed: {name}")
        output = work / ("input-" + group)
        metadata = load_input(store, name, output.with_suffix(".sqlite"), output)
        if metadata["canonical_generations"] != generations:
            raise LakePublishError("economic scoped input generation changed during readback")
        updated.append(name)
    before_put()
    report["inputs_updated"] = updated
    write_json_atomic(directory / "applied.json", report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--asof", type=date.fromisoformat, required=True)
    parser.add_argument("--lease-handle", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
    try:
        report = integrate(
            store,
            args.directory,
            database=args.db,
            asof=args.asof,
            lease_handle=args.lease_handle,
            log_publication=True,
        )
        print(json.dumps(report, sort_keys=True))
        return 0
    except Exception as error:
        print(
            f"economic L1 integration failed: {type(error).__name__}; "
            "retained handoffs resume on the next cycle",
            file=sys.stderr,
        )
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
