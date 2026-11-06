"""固定した旧TradingView 3受領だけを、既存lease/CAS/readbackで経済正本へ保全する。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, cast

from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import Boto3R2Store, LakeCASConflict, ObjectStore
from baibai_engine.storage.snapshots.sources import sha256_file
from baibai_engine.storage.sqlite.merge import MergeError
from baibai_engine.storage.sqlite.snapshot import create_snapshot
from baibai_jobs.acquisition.economic_staging import stage_prefix
from baibai_jobs.integration.economic_sources import KEY, _put_readback, _read
from baibai_jobs.integration.native_source_merge import verify_stores
from baibai_jobs.publication.lease import _load_handle, verify_held
from tools.diagnostics.inspect_legacy_economic_handoffs import (
    TARGETS,
    ReadOnlyInspectionStore,
    inspect,
)

TARGET = TARGETS[1]


def recover(store: ObjectStore, directory: Path, *, lease_handle: Path) -> dict[str, object]:
    """FRB・元stage・marker・input・L2には書かず、現正本を再読して有限に処置する。"""
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    report: dict[str, object] = {
        "kind": "fixed_tradingview_original_recovery",
        "stage_id": TARGET.stage_id,
        "manifest_sha256": TARGET.manifest_sha256,
        "status": "checking",
        "phase": "lease_and_fresh_base",
        "canonical_put_attempted": False,
        "canonical_readback_verified": False,
        "backup_put_attempted": False,
        "backup_readback_verified": False,
        "marker_updated": False,
        "frb_included": False,
        "inputs_published": False,
        "l2_published": False,
    }
    try:
        handle = _load_handle(lease_handle)
        verify_held(store, handle)
        original = store.head(KEY)
        if original is None:
            raise ValueError("economic canonical is missing")
        report["phase"] = "fixed_original_inspection"
        work = directory / "inspection"
        reader = ReadOnlyInspectionStore(store, (TARGET,))
        inspection = inspect(reader, work, targets=(TARGET,))
        report["inspection"] = inspection
        if inspection["status"] != "prepared_locally":
            raise ValueError("fixed original inspection did not complete")
        canonical = cast(dict[str, Any], inspection["canonical"])
        if canonical["etag_fingerprint"] != hashlib.sha256(original.etag.encode()).hexdigest():
            raise LakeCASConflict("canonical changed before inspection")
        base = work / "canonical.sqlite"
        scoped = work / "converted-0" / "receipts.sqlite"
        candidate = work / "candidate-0.sqlite"
        try:
            verify_stores(scoped, base)
        except MergeError:
            # Inspection already rejects payload/identity collisions. Only a
            # missing original requires publication; physical SQLite hashes do not.
            already_present = False
        else:
            already_present = True
        report["already_present"] = already_present
        report["phase"] = "publication_readback"
        handoffs = cast(list[dict[str, Any]], inspection["handoffs"])
        prefix = stage_prefix(TARGET.stage_id)
        original_keys = (
            prefix + "/handoff.json",
            prefix + "/integrated.json",
            prefix + "/receipts-" + handoffs[0]["receipt_sha256"] + ".sqlite",
        )

        def verify_originals() -> None:
            # The same wrapper saw each HEAD during fixed inspection. Canonical
            # intentionally changes; only the three immutable/source keys stay fixed.
            for key in original_keys:
                reader.head(key)

        def before_put() -> None:
            verify_originals()
            verify_held(store, handle)
            current = store.head(KEY)
            if current is None or current.etag != original.etag:
                raise LakeCASConflict("canonical changed before fixed recovery")

        before_put()
        readback = directory / "readback.sqlite"
        if already_present:
            published = _read(store, readback)
            if published.etag != original.etag or sha256_file(readback) != sha256_file(base):
                raise LakeCASConflict("canonical changed during no-op readback")
        else:
            upload = directory / "upload.sqlite"
            create_snapshot(candidate, upload)
            verify_stores(base, upload)
            verify_stores(scoped, upload)
            report["candidate_sha256"] = sha256_file(upload)
            backup = store.head(KEY + ".bak")

            def before_backup() -> None:
                before_put()
                report["backup_put_attempted"] = True
                write_json_atomic(directory / "result.json", report)

            _put_readback(
                store,
                KEY + ".bak",
                base,
                directory / "backup-readback.sqlite",
                expected=backup.etag if backup else None,
                before_put=before_backup,
            )
            report["backup_readback_verified"] = True

            def before_canonical() -> None:
                before_put()
                report["canonical_put_attempted"] = True
                write_json_atomic(directory / "result.json", report)

            published = _put_readback(
                store,
                KEY,
                upload,
                readback,
                expected=original.etag,
                before_put=before_canonical,
            )
        # No downstream success, seal or marker acknowledgement is inferred.
        verify_stores(base, readback)
        verify_stores(scoped, readback)
        report["canonical_readback_verified"] = True
        verify_originals()
        report["originals_unchanged"] = True
        report["canonical_sha256"] = sha256_file(readback)
        report["canonical_etag_fingerprint"] = hashlib.sha256(published.etag.encode()).hexdigest()
        verify_held(store, handle)
        current = store.head(KEY)
        if current is None or current.etag != published.etag:
            raise LakeCASConflict("canonical changed after recovery readback")
        report.update(
            status="already_present" if already_present else "recovered", phase="complete"
        )
    except Exception as error:
        # Retain attempted-vs-readback state; never print provider/error payloads.
        report.update(status="failed", reason=type(error).__name__)
    write_json_atomic(directory / "result.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--lease-handle", type=Path, required=True)
    args = parser.parse_args()
    store: Boto3R2Store | None = None
    try:
        store = Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
        report = recover(store, args.directory, lease_handle=args.lease_handle)
        print(json.dumps(report, sort_keys=True))
        return int(report["status"] not in {"recovered", "already_present"})
    except Exception as error:
        print(json.dumps({"status": "failed", "reason": type(error).__name__}))
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
