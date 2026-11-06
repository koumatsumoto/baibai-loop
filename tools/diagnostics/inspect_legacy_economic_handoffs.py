"""二つの旧schema受領を読取り、既存APIで回復copyだけを準備する一回用診断。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Never

from baibai_engine.data_collection.receipts.native_sources.store import RECEIPT_KEYS
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.json import read_private
from baibai_engine.storage.objects.r2 import (
    Boto3R2Store,
    LakePublishError,
    ObjectStore,
    RemoteObject,
)
from baibai_engine.storage.snapshots.sources import sha256_file
from baibai_jobs.acquisition.economic_staging import stage_prefix
from baibai_jobs.acquisition.market_context import validate_packet
from baibai_jobs.acquisition.receipts import download_fixed
from baibai_jobs.integration.economic_handoff import validate_handoff
from baibai_jobs.integration.economic_sources import _read
from baibai_jobs.integration.native_source_merge import reconcile_stores, verify_stores
from baibai_jobs.maintenance.economic_cutover import verify_copy
from baibai_jobs.maintenance.migrate_economic_store import migrate


@dataclass(frozen=True)
class Target:
    group: str
    stage_id: str
    manifest_sha256: str
    source_id: str
    acquisition_status: str


TARGETS = (
    Target(
        "frb_h15",
        "frb_h15-2026-11-04-41861189726-2",
        "e36ad1004448588fd981754e9f9310425864c2cfe53934a00044e24d0be89ef4",
        "fed-h15",
        "failed",
    ),
    Target(
        "tradingview_market",
        "tradingview_market-41950108361-1",
        "2cd5ed90231604700bf1cd84de0991f192597b33000b3e17dcd4a83982fb559a",
        "tradingview-market",
        "ok",
    ),
)


class ReadOnlyInspectionStore:
    """固定した二stageと経済正本以外を読まず、remote writeを公開しない。"""

    def __init__(self, store: ObjectStore, targets: tuple[Target, ...]) -> None:
        self._store = store
        self._manifests = {
            stage_prefix(target.stage_id) + "/handoff.json": target.manifest_sha256
            for target in targets
        }
        prefixes = [re.escape(stage_prefix(target.stage_id)) for target in targets]
        self._allowed = re.compile(
            r"(?:indicators\.sqlite|(?:"
            + "|".join(prefixes)
            + r")/(?:handoff\.json|integrated\.json|receipts-[0-9a-f]{64}\.sqlite))"
        )
        self._observed: dict[str, RemoteObject | None] = {}

    def _check_key(self, key: str) -> None:
        if self._allowed.fullmatch(key) is None:
            raise ValueError("object is outside the finite inspection scope")

    def head(self, key: str) -> RemoteObject | None:
        self._check_key(key)
        value = self._store.head(key)
        if key in self._observed and self._observed[key] != value:
            raise LakePublishError("inspection object changed during read")
        self._observed[key] = value
        return value

    def get_bytes(self, key: str) -> bytes:
        self._check_key(key)
        value = self._store.get_bytes(key)
        expected = self._manifests.get(key)
        if expected is not None and hashlib.sha256(value).hexdigest() != expected:
            raise LakePublishError("fixed inspection manifest changed")
        return value

    def download_file(self, key: str, path: Path, *, expect_bytes: int | None = None) -> None:
        self._check_key(key)
        self._store.download_file(key, path, expect_bytes=expect_bytes)

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
    ) -> Never:
        raise PermissionError("read-only inspection cannot publish any object")

    def verify_unchanged(self) -> None:
        for key in tuple(self._observed):
            self.head(key)


def _marker(store: ObjectStore, target: Target, started_at: str) -> dict[str, object]:
    saved = read_private(store, stage_prefix(target.stage_id) + "/integrated.json")
    if saved is None:
        return {"present": False, "status": "absent"}
    value = json.loads(saved.payload)
    if (
        value.get("manifest_sha256") != target.manifest_sha256
        or value.get("source") != "economic-" + target.group
        or value.get("started_at") != started_at
        or value.get("status") not in {"integrated", "needs_explicit_recovery"}
    ):
        raise ValueError("marker differs from the fixed original handoff")
    return {
        "present": True,
        "status": value["status"],
        "sha256": hashlib.sha256(saved.payload).hexdigest(),
        "etag_fingerprint": hashlib.sha256(saved.etag.encode()).hexdigest(),
    }


def _counts(database: Path) -> dict[str, object]:
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        return {
            "schema_version": conn.execute("PRAGMA user_version").fetchone()[0],
            "rows": {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # nosec B608
                for table in RECEIPT_KEYS
            },
            "run_status_counts": {
                status: conn.execute(
                    "SELECT COUNT(*) FROM economic_source_runs WHERE status=?", (status,)
                ).fetchone()[0]
                for status in ("ok", "failed")
            },
        }


def inspect(
    store: ObjectStore, directory: Path, *, targets: tuple[Target, ...] = TARGETS
) -> dict[str, object]:
    """Remoteはread-only。schema変換・reconcileは新しいlocal copyだけに行う。"""
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    reader = ReadOnlyInspectionStore(store, targets)
    handoffs: list[dict[str, object]] = []
    report: dict[str, object] = {
        "kind": "legacy_economic_read_only_inspection",
        "status": "inspecting",
        "phase": "canonical_read",
        "remote_write": False,
        "raw_upload": False,
        "recovery_published": False,
        "handoffs": handoffs,
    }
    try:
        canonical = directory / "canonical.sqlite"
        remote = _read(reader, canonical)
        original_sha = sha256_file(canonical)
        metadata_sha = remote.metadata.get("sha256")
        if metadata_sha is not None and metadata_sha != original_sha:
            raise LakePublishError("canonical content differs from its recorded digest")
        report["canonical"] = {
            "schema_version": 13,
            "bytes": remote.size,
            "sha256": original_sha,
            "etag_fingerprint": hashlib.sha256(remote.etag.encode()).hexdigest(),
            "metadata_sha256_verified": metadata_sha is not None,
        }
        candidate = canonical
        for index, target in enumerate(targets):
            report["phase"] = "legacy_handoff_read"
            report["active_stage"] = target.stage_id
            raw = directory / f"raw-{index}"
            entry: dict[str, object] = {
                "stage_id": target.stage_id,
                "market": False,
                "manifest_sha256": target.manifest_sha256,
            }
            download_fixed(reader, entry, raw)
            original = json.loads((raw / "result.json").read_text())
            if original.get("sources") != [
                {
                    "source_id": target.source_id,
                    "status": target.acquisition_status,
                    **({"history_recovery_required": False} if target.group == "frb_h15" else {}),
                }
            ]:
                raise ValueError("original acquisition outcome differs from the fixed scope")
            marker = _marker(reader, target, original["started_at"])
            source = raw / "receipts.sqlite"
            source_sha = sha256_file(source)
            outcome: dict[str, object] = {
                "stage_id": target.stage_id,
                "source": "economic-" + target.group,
                "manifest_sha256": target.manifest_sha256,
                "receipt_sha256": source_sha,
                "receipt_bytes": source.stat().st_size,
                "acquisition_status": target.acquisition_status,
                "marker": marker,
                **_counts(source),
            }
            handoffs.append(outcome)
            report["phase"] = "offline_copy"
            converted = directory / f"converted-{index}"
            converted.mkdir(mode=0o700)
            scoped = converted / "receipts.sqlite"
            migrate(source, scoped)
            shutil.copyfile(raw / "result.json", converted / "result.json")
            verify_copy(source, scoped)
            if target.group == "tradingview_market":
                packet = validate_packet(converted, canonical=candidate)
                references = packet["market_receipts"]
                if not isinstance(references, list) or len(references) != 3:
                    raise ValueError("fixed market handoff must retain its three captures")
                outcome["capture_count"] = 3
            else:
                validate_handoff(converted, target.group, target.source_id)
            # Do not transplant market-origin.json: conversion changes the SQLite hash.
            report["phase"] = "local_reconcile"
            combined = directory / f"candidate-{index}.sqlite"
            shutil.copyfile(scoped, combined)
            reconcile_stores(candidate, combined)
            verify_stores(candidate, combined)
            verify_stores(scoped, combined)
            if sha256_file(source) != source_sha or sha256_file(canonical) != original_sha:
                raise ValueError("read-only input copy changed")
            outcome["originals_verified"] = True
            candidate = combined
        report["phase"] = "final_readback"
        reader.verify_unchanged()
        report.update(
            status="prepared_locally",
            remote_objects_unchanged=True,
            candidate={
                "sha256": sha256_file(candidate),
                "bytes": candidate.stat().st_size,
                "base_and_legacy_originals_verified": True,
            },
        )
        report.pop("active_stage", None)
    except Exception as error:
        # Error text and tracebacks can contain provider payload/URLs. Keep them private.
        report.update(status="failed", reason=type(error).__name__)
    write_json_atomic(directory / "summary.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    store: Boto3R2Store | None = None
    try:
        store = Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
        report = inspect(store, args.directory)
        print(json.dumps(report, sort_keys=True))
        return int(report["status"] != "prepared_locally")
    except Exception as error:
        print(json.dumps({"status": "failed", "reason": type(error).__name__}))
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
