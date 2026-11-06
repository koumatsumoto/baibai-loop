"""一回のEDINET移送に必要なmetadataと容量だけを読む。原本のGET・書込はしない。"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.providers.edinet.archive_native import (
    HEAD_KEY as ARCHIVE_HEAD,
)
from baibai_engine.data_collection.providers.edinet.archive_native import EdinetArchiveL1
from baibai_engine.data_collection.providers.edinet.list_native import HEAD_KEY as LIST_HEAD
from baibai_engine.data_collection.providers.edinet.list_native import EdinetListL1
from baibai_engine.data_collection.providers.edinet.snapshot_contracts import EDINET_SOURCE_ARCHIVES
from baibai_engine.storage.json import canonical_json
from baibai_engine.storage.objects.r2 import LakePublishError, RemoteObject
from baibai_engine.storage.snapshots.reader import FixedRelease
from baibai_jobs.acquisition.receipts import ReceiptStore, available_handoffs

_SOURCES = ("edinet-list", "edinet-metrics", "edinet-facts")


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _head_value(remote: RemoteObject | None) -> dict[str, Any]:
    if remote is None:
        return {"present": False, "bytes": 0, "etag": None, "metadata_sha256": None}
    return {
        "present": True,
        "bytes": remote.size,
        "etag": remote.etag,
        "metadata_sha256": remote.metadata.get("sha256"),
    }


def inspect_remote_inputs(
    store: ReceiptStore, release: FixedRelease, *, directory: Path
) -> dict[str, Any]:
    """Metadata-only observation, not a raw completeness or drain certificate.

    HEAD metadata is not the canonical SQLite file digest unless the downloaded
    file is subsequently verified. Source handoffs remain the finite import's
    authority; LIST totals also include unmanifested receipt objects.
    """
    canonical = _head_value(store.head("market.sqlite"))
    archive_remote = store.head(ARCHIVE_HEAD)
    archives = _head_value(archive_remote)
    archives.update(wire_sha256=None, identity=None)
    if archive_remote is not None:
        fixed = EdinetArchiveL1(store, directory / "archive-head-read").freeze()
        if store.head(ARCHIVE_HEAD) != archive_remote:
            raise LakePublishError("EDINET archive head moved during inspection")
        metadata = fixed.value()["archives"]
        archives.update(
            wire_sha256=archive_remote.metadata["sha256"],
            identity=fixed.identity,
            archive_count=len(metadata),
            unique_body_bytes=sum({item["sha256"]: item["size"] for item in metadata}.values()),
            scope_since=None if fixed.scope.since is None else fixed.scope.since.isoformat(),
            scope_doc_count=len(fixed.scope.doc_ids),
            legacy_origin_count=len(fixed.scope.legacy_origins),
        )
    list_remote = store.head(LIST_HEAD)
    lists = _head_value(list_remote)
    lists.update(wire_sha256=None, identity=None)
    if list_remote is not None:
        fixed_lists = EdinetListL1(store, directory / "list-head-read").freeze()
        if store.head(LIST_HEAD) != list_remote:
            raise LakePublishError("EDINET list head moved during inspection")
        receipts = fixed_lists.value()["receipts"]
        lists.update(
            wire_sha256=list_remote.metadata["sha256"],
            identity=fixed_lists.identity,
            receipt_count=len(receipts),
            unique_body_count=len({item["sha256"] for item in receipts}),
        )
    sources, entries, listed = [], [], []
    for source in _SOURCES:
        objects = sorted(store.iter_objects(f"coordination/market-sources/{source}-"))
        listed.extend(objects)
        handoffs = {key.rsplit("/", 1)[0] for key, _ in objects if key.endswith("/handoff.json")}
        receipts = [(key, size) for key, size in objects if key.endswith(".sqlite")]
        selected = available_handoffs(store, source=source, market=True, include_integrated=True)
        entries.extend(selected)
        sources.append(
            {
                "source": source,
                "objects": len(objects),
                "listed_bytes": sum(size for _, size in objects),
                "listed_inventory_sha256": _digest(objects),
                "receipt_objects": len(receipts),
                "receipt_bytes": sum(size for _, size in receipts),
                "handoffs": len(selected),
                "invalid_handoffs": sum(bool(item.get("invalid")) for item in selected),
                "receipts_without_handoff_directory": sum(
                    key.rsplit("/", 1)[0] not in handoffs for key, _ in receipts
                ),
            }
        )
    manifest = release.dataset_manifests.get(EDINET_SOURCE_ARCHIVES.name)
    return {
        "release_id": release.release_id,
        "manifest_sha256": release.manifest_sha256,
        "canonical": canonical,
        "archive_dataset": {
            "present": manifest is not None,
            "rows": 0 if manifest is None else manifest.totals.rows,
            "parquet_bytes": 0 if manifest is None else manifest.totals.bytes,
            "objects": 0 if manifest is None else manifest.totals.objects,
        },
        "native_archives": archives,
        "native_lists": lists,
        "source_stages": sources,
        "handoff_inventory_sha256": _digest(entries),
        "listed_inventory_sha256": _digest(sorted(listed)),
        "free_bytes": shutil.disk_usage(directory).free,
    }


def inspect_prepared_capacity(plan: dict[str, Any], *, directory: Path) -> dict[str, int]:
    """Measure retained files and the additional native ZIP work, without writing.

    Atomic body replacement temporarily needs the largest body twice. Head JSON
    and filesystem overhead are additional; the caller chooses its reserve. A
    compressed Parquet byte count is never a SQLite expansion estimate.
    """
    bodies = {item["metadata"]["sha256"]: item["metadata"]["size"] for item in plan["archives"]}
    unique_bytes = sum(bodies.values())
    largest = max(bodies.values(), default=0)
    return {
        "prepared_local_bytes": sum(
            path.stat().st_size for path in directory.rglob("*") if path.is_file()
        ),
        "canonical_local_bytes": Path(plan["databases"][0]["path"]).stat().st_size,
        "archive_identities": len(plan["archives"]),
        "unique_native_bodies": len(bodies),
        "unique_native_body_bytes": unique_bytes,
        "largest_native_body_bytes": largest,
        "native_body_write_peak_bytes": unique_bytes + largest,
        "free_bytes": shutil.disk_usage(directory).free,
    }
