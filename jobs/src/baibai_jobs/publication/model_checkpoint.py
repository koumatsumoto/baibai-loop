"""Finite recovery's durable committed runs, before its lake pointer can advance."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from baibai_engine.machine_analysis.generation.contracts import GenerationMetadata
from baibai_engine.storage.objects.json import put_private, read_private
from baibai_engine.storage.objects.r2 import Boto3R2Store, ObjectStore
from baibai_engine.storage.snapshots.keys import release_manifest_key
from baibai_engine.storage.snapshots.models import canonical_lake_model_bytes
from baibai_engine.storage.snapshots.release import LakeReleasePointer
from baibai_engine.storage.sqlite.machine_analysis import RUN_STORE_SCHEMA_VERSION
from baibai_engine.storage.sqlite.snapshot import backup_to_new_file
from baibai_jobs.integration.jpx_recovery_verification import prior_generations, row_fingerprint
from baibai_jobs.publication.lease import PublicationLeaseHandle, verify_held
from baibai_jobs.publication.snapshot_publication import PreparedMarketLake

_PREFIX = "coordination/finance-recovery/"


def _digest(path: Path, algorithm: str = "sha256") -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


@dataclass(frozen=True, slots=True)
class ModelPublicationCheckpoint:
    """One reviewed contract gets one immutable prepared receipt, never a retry loop."""

    contract_sha256: str
    directory: Path
    prior_generation_ids: tuple[str, ...]
    minimum_remaining_seconds: int = 1200
    prior_fingerprints: dict[str, Any] | None = None
    legacy_fingerprints: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", self.contract_sha256) is None:
            raise ValueError("invalid recovery contract digest")
        if (
            type(self.minimum_remaining_seconds) is not int
            or not 0 < self.minimum_remaining_seconds < 220 * 60
            or len(set(self.prior_generation_ids)) != len(self.prior_generation_ids)
        ):
            raise ValueError("invalid model checkpoint conditions")

        if (self.prior_fingerprints is None) != (self.legacy_fingerprints is None):
            raise ValueError("prior and legacy fingerprints must be supplied together")
        if self.prior_fingerprints is not None and (
            self.prior_fingerprints.get("generation_ids") != sorted(self.prior_generation_ids)
            or self.legacy_fingerprints is None
            or set(self.legacy_fingerprints) != {"screening_run", "stock_analysis"}
        ):
            raise ValueError("checkpoint fingerprint coverage differs")

    @property
    def receipt_key(self) -> str:
        return f"{_PREFIX}{self.contract_sha256}/prepared.json"

    def guard(self, store: ObjectStore, handle: PublicationLeaseHandle) -> None:
        verify_held(store, handle)
        if (handle.expires_at_utc - datetime.now(UTC)).total_seconds() < (
            self.minimum_remaining_seconds
        ):
            raise ValueError("insufficient lease time for accepted pair publication")

    def protect(
        self,
        *,
        store: ObjectStore,
        handle: PublicationLeaseHandle,
        prepared: PreparedMarketLake,
        runs: Path,
        generation_id: str,
        selection_id: str,
    ) -> None:
        self.guard(store, handle)
        # Even identical receipts must not silently reopen an interrupted attempt.
        if store.head(self.receipt_key) is not None:
            raise ValueError("recovery prepared receipt already exists; reconcile instead of retry")
        self.directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="prepared-runs-", dir=self.directory) as directory:
            snapshot = Path(directory) / "runs.sqlite"
            backup_to_new_file(runs, snapshot)
            self._validate(snapshot, prepared, generation_id, selection_id)
            sha256 = _digest(snapshot)
            size = snapshot.stat().st_size
            md5 = _digest(snapshot, "md5")
            key = f"{_PREFIX}objects/{sha256}.sqlite"
            # No overwrite, and no blind retry after a missing acknowledgement.
            acknowledged = store.put_file(
                key,
                snapshot,
                sha256=sha256,
                content_md5=base64.b64encode(bytes.fromhex(md5)).decode(),
                content_type="application/vnd.sqlite3",
                if_none_match=True,
            )
            if (
                acknowledged.size != size
                or acknowledged.etag != md5
                or acknowledged.metadata.get("sha256") != sha256
                or store.head(key) != acknowledged
            ):
                raise ValueError("prepared runs acknowledgement differs")
            # Source runs remains untouched. Reuse the transfer slot so full GET
            # does not coexist with a second multi-GB snapshot or VACUUM copy.
            snapshot.unlink()
            if isinstance(store, Boto3R2Store):
                store.download_file(key, snapshot, expect_bytes=size, max_bytes=size)
            else:
                store.download_file(key, snapshot, expect_bytes=size)
            if (
                snapshot.stat().st_size != size
                or _digest(snapshot) != sha256
                or store.head(key) != acknowledged
            ):
                raise ValueError("prepared runs full readback differs")
            self._validate(snapshot, prepared, generation_id, selection_id)
            pointer = LakeReleasePointer(
                release_id=prepared.release.release_id,
                manifest_key=release_manifest_key(release_id=prepared.release.release_id),
                manifest_sha256=prepared.release_sha256,
            )
            receipt = {
                "version": 1,
                "contract_sha256": self.contract_sha256,
                "generation_id": generation_id,
                "selection_id": selection_id,
                "release_id": prepared.release.release_id,
                "manifest_sha256": prepared.release_sha256,
                "expected_previous_pointer_etag": prepared.serving.precondition.etag,
                "pointer_sha256": hashlib.sha256(canonical_lake_model_bytes(pointer)).hexdigest(),
                "runs": {"key": key, "sha256": sha256, "bytes": size, "etag": md5},
            }
            payload = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
            saved = put_private(store, self.receipt_key, payload, previous_etag=None)
            readback = read_private(store, self.receipt_key)
            if readback != saved:
                raise ValueError("prepared receipt full readback differs")
            (self.directory / "prepared.json").write_bytes(payload)
        # Upload/readback can take minutes; a check before them is insufficient.
        self.guard(store, handle)

    def _validate(
        self, snapshot: Path, prepared: PreparedMarketLake, generation_id: str, selection_id: str
    ) -> None:
        if self.prior_fingerprints is not None and (
            prior_generations(snapshot, self.prior_generation_ids) != self.prior_fingerprints
        ):
            raise ValueError("prior generation rows changed before publication")
        with closing(sqlite3.connect(snapshot.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            if db.execute("PRAGMA user_version").fetchone() != (
                RUN_STORE_SCHEMA_VERSION,
            ) or db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("prepared runs schema or integrity differs")
            if (
                self.legacy_fingerprints is not None
                and {
                    table: row_fingerprint(db.execute("SELECT * FROM " + table))  # nosec B608
                    for table in ("screening_run", "stock_analysis")
                }
                != self.legacy_fingerprints
            ):
                raise ValueError("legacy runs rows changed before publication")
            ids = {row[0] for row in db.execute("SELECT generation_id FROM model_generation")}
            if generation_id in self.prior_generation_ids or ids != {
                *self.prior_generation_ids,
                generation_id,
            }:
                raise ValueError("prepared runs does not preserve the exact generations")
            row = db.execute(
                "SELECT header FROM model_generation WHERE generation_id=?", (generation_id,)
            ).fetchone()
            assert row is not None
            metadata = GenerationMetadata.model_validate_json(row[0])
            market = metadata.inputs["market"]
            if (
                metadata.generation_id != generation_id
                or generation_id != f"models-{prepared.release.release_id}"
                or market.kind != "lake_release"
                or market.reference != prepared.release.release_id
                or market.sha256 != prepared.release_sha256
                or db.execute(
                    "SELECT selection_id FROM screening_selection WHERE generation_id=?",
                    (generation_id,),
                ).fetchall()
                != [(selection_id,)]
                or db.execute("PRAGMA foreign_key_check").fetchone() is not None
            ):
                raise ValueError("prepared generation, selection or release identity differs")
