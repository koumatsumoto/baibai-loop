"""One-shot, attended fresh-market lake cutover from the pinned schema-39 release.

The schema-47 candidate contains newly acquired L1 and starts with no lake origin.
This tool never hydrates or copies old L1, and does not push machine, runs, or
application stores. Remove it after the attended cutover is verified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import struct
import tempfile
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

from baibai_batch.storage.lake_publish import (
    Boto3R2Store,
    ObjectStore,
    PointerPrecondition,
    publish_release,
    read_pointer_snapshot,
)
from baibai_batch.storage.publication_lease import (
    PublicationLeaseHandle,
    _load_handle,
    verify_held,
)
from baibai_batch.storage.publish_market_lake import (
    _export_manifest_snapshot,
    _resolve_serving_release,
)
from baibai_engine.batch_api import (
    LakeStoreOrigin,
    LocalMirrorSource,
    advance_lake_store_origin,
    canonical_lake_model_bytes,
    create_lake_release,
    export_market_store,
    lake_dataset_manifest_key,
    lake_mirror_path,
    lake_release_manifest_key,
    lake_verified_git_commit,
    resolve_release,
    validate_lake_release_policy,
)
from baibai_engine.storage.lake.datasets import LAKE_DATASETS
from baibai_engine.storage.lake.writer import validate_snapshot_parity
from baibai_engine.storage.observations.lake_origin import (
    read_lake_store_origin,
    read_lake_store_origin_from_connection,
)
from baibai_engine.storage.observations.schema import (
    SQLITE_SCHEMA_VERSION,
    validate_current_schema,
)

OLD_POINTER_KEY = "lake/pointers/l1/current.json"
NEW_POINTER_KEY = "lake/pointers/market/current.json"
MARKET_KEY = "market.sqlite"
CANDIDATE_SCHEMA = 47
VERIFIED_CANDIDATE_SHA256 = "8e107d53c00c5101d4bc83a408ec768a1d644e3a6acae9e644728313f3b8ef4a"
VERIFIED_FIXED_RELEASE_ID = "20261019T100002Z-54cf954c-d37a5ef3028d"
VERIFIED_FIXED_RELEASE_SHA256 = "45eac7c1c004b629941979b4b382b23629ff85b3d017b72d074db54ec6a62891"

# The user accepted these twelve unavailable pre-baseline spans on 2026-10-19.
# This applies only to this one-shot transition from PRODUCTION_SOURCE. The old
# release remains reference data; no old rows or coverage ledger enter the new
# candidate. Ordinary coverage, required-dataset and integrity policy is intact.
# Each pair is (the exact pinned old floor, latest allowed fresh first date).
APPROVED_TRANSITION_GAPS: Mapping[str, tuple[date, date]] = {
    "edinet.metrics": (date(2026, 5, 8), date(2026, 10, 18)),
    "external_assessment.analyst_rating_observations": (date(2026, 10, 12), date(2026, 10, 19)),
    "external_assessment.model_valuation_observations": (date(2026, 10, 12), date(2026, 10, 19)),
    "external_assessment.quants_score_observations": (date(2026, 10, 12), date(2026, 10, 19)),
    "jpx.regulation_flags": (date(2026, 5, 8), date(2026, 10, 18)),
    "jpx.regulation_sources": (date(2026, 5, 8), date(2026, 10, 18)),
    "jquants.daily_bars": (date(2016, 10, 3), date(2016, 10, 18)),
    "jquants.fin_summaries": (date(2016, 10, 1), date(2016, 10, 18)),
    "jquants.market_calendar": (date(2016, 10, 17), date(2016, 10, 18)),
    "jquants.short_sale_reports": (date(2016, 8, 10), date(2016, 10, 18)),
    "jquants.weekly_margin": (date(2016, 8, 5), date(2016, 10, 21)),
    "tradingview.forecast_snapshots": (date(2026, 10, 11), date(2026, 10, 19)),
}


class CutoverError(RuntimeError):
    """A pinned source, candidate generation, or publication outcome is unsafe."""


@dataclass(frozen=True, slots=True)
class SourcePin:
    pointer_etag: str
    pointer_sha256: str
    release_id: str
    manifest_sha256: str
    market_etag: str
    market_bytes: int
    market_schema: int
    contracts: Mapping[str, int]


# Read-only R2 HEAD/GET on 2026-10-19. Any change requires a fresh review of the
# source manifest, its history floors, and the new candidate before updating this pin.
PRODUCTION_SOURCE = SourcePin(
    pointer_etag="03657456fb7a58f609d917172859f7d2",
    pointer_sha256="a4cc5b23564bf049b93587933366c51a2d8bf391ff63a08544fcc6bf36602eec",
    release_id="20261018T163124Z-29af5914-e063defface9",
    manifest_sha256="9e7f3ba9941deb53eb2b70bd0c4af4b3ff8417e0c8ca4094accb94377a915d43",
    market_etag="8e85f5a957341cb9292c238a95fd9d80",
    market_bytes=1556480,
    market_schema=39,
    contracts={
        "edinet.debt_schedule": 1,
        "edinet.document_lists": 2,
        "edinet.documents": 1,
        "edinet.metrics": 2,
        "edinet.segment_facts": 1,
        "edinet.tender_offer_exit_values": 1,
        "external_assessment.analyst_rating_observations": 1,
        "external_assessment.model_valuation_observations": 1,
        "external_assessment.quants_score_observations": 1,
        "jpx.delistings": 1,
        "jpx.earnings_calendar": 1,
        "jpx.regulation_flags": 1,
        "jpx.regulation_sources": 1,
        "jquants.all_issues_daily_margin": 2,
        "jquants.daily_bars": 2,
        "jquants.fin_summaries": 4,
        "jquants.margin_alerts": 1,
        "jquants.market_calendar": 2,
        "jquants.master_snapshots": 2,
        "jquants.short_sale_reports": 2,
        "jquants.weekly_margin": 1,
        "tradingview.forecast_snapshots": 1,
    },
)


class CutoverStore(ObjectStore, Protocol):
    def sqlite_user_version(self, key: str, *, etag: str) -> int: ...


class R2CutoverStore(Boto3R2Store):
    def sqlite_user_version(self, key: str, *, etag: str) -> int:
        """Read only SQLite header bytes 0..99 from the pinned remote generation."""
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
            raise CutoverError("remote market header range is not exact SQLite bytes")
        return int(struct.unpack(">I", header[60:64])[0])


def _read_small(
    store: CutoverStore,
    key: str,
    *,
    max_bytes: int,
    expected_sha256: str | None = None,
    expected_etag: str | None = None,
) -> bytes:
    before = store.head(key)
    if before is None or before.size > max_bytes or before.size <= 0:
        raise CutoverError(f"source manifest missing or too large: {key}")
    if expected_etag is not None and before.etag != expected_etag:
        raise CutoverError(f"source generation changed: {key}")
    payload = store.get_bytes(key)
    after = store.head(key)
    if after is None or after.etag != before.etag or len(payload) != before.size:
        raise CutoverError(f"source generation changed during read: {key}")
    if expected_sha256 is not None and hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise CutoverError(f"source manifest digest differs: {key}")
    return payload


def _dict(payload: bytes, *, label: str) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CutoverError(f"invalid {label} JSON") from None
    if not isinstance(value, dict):
        raise CutoverError(f"invalid {label} object")
    return value


def _sha_file(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _candidate_hashes(path: Path) -> tuple[str, str | None]:
    market = _sha_file(path)
    if market is None:
        raise CutoverError("candidate file disappeared during hash")
    return market, _sha_file(path.with_name(f"{path.name}-wal"))


def _write_recovery_record(
    path: Path,
    *,
    candidate: Path,
    release_id: str,
    digest: str,
    watch: _CandidateWatch,
    history_gaps: Mapping[str, dict[str, str]],
) -> None:
    market_hash, wal_hash = _candidate_hashes(candidate)
    watch.require_unchanged()
    record = {
        "record_version": 1,
        "candidate": str(candidate.resolve()),
        "market_sha256": market_hash,
        "wal_sha256": wal_hash,
        "release_id": release_id,
        "manifest_sha256": digest,
        "history_gaps": history_gaps,
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as target:
        json.dump(record, target, sort_keys=True)
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())


def _legacy_history_floors(store: CutoverStore, pin: SourcePin) -> dict[str, date]:
    """Read only the one pinned v1 graph; do not reinterpret it as a v2 model."""
    if read_pointer_snapshot(store).pointer is not None:
        raise CutoverError("new-format current pointer already exists")
    pointer = _dict(
        _read_small(
            store,
            OLD_POINTER_KEY,
            max_bytes=1024,
            expected_etag=pin.pointer_etag,
            expected_sha256=pin.pointer_sha256,
        ),
        label="legacy pointer",
    )
    release_key = f"lake/manifests/releases/l1/{pin.release_id}.json"
    if pointer != {
        "pointer_version": 1,
        "release_id": pin.release_id,
        "manifest_key": release_key,
        "manifest_sha256": pin.manifest_sha256,
    }:
        raise CutoverError("legacy pointer differs from the pinned v1 identity")
    market = store.head(MARKET_KEY)
    if market is None or market.etag != pin.market_etag or market.size != pin.market_bytes:
        raise CutoverError("production market store generation changed")
    if store.sqlite_user_version(MARKET_KEY, etag=pin.market_etag) != pin.market_schema:
        raise CutoverError("production market schema changed")
    release = _dict(
        _read_small(
            store, release_key, max_bytes=16 * 1024 * 1024, expected_sha256=pin.manifest_sha256
        ),
        label="legacy release",
    )
    entries = release.get("datasets")
    if (
        release.get("manifest_version") != 1
        or release.get("release_id") != pin.release_id
        or release.get("profile") != "production"
        or not isinstance(entries, dict)
        or set(entries) != set(pin.contracts)
    ):
        raise CutoverError("legacy release contract differs from pinned schema 39")
    floors: dict[str, date] = {}
    for name, expected_contract in sorted(pin.contracts.items()):
        entry = entries[name]
        if not isinstance(entry, dict) or entry.get("contract_version") != expected_contract:
            raise CutoverError(f"legacy dataset contract differs: {name}")
        build_id = entry.get("build_id")
        digest = entry.get("manifest_sha256")
        if not isinstance(build_id, str) or not isinstance(digest, str):
            raise CutoverError(f"legacy dataset identity is invalid: {name}")
        dataset = _dict(
            _read_small(
                store,
                lake_dataset_manifest_key(dataset=name, build_id=build_id),
                max_bytes=16 * 1024 * 1024,
                expected_sha256=digest,
            ),
            label=f"legacy dataset {name}",
        )
        if (
            dataset.get("manifest_version") != 1
            or dataset.get("dataset") != name
            or dataset.get("build_id") != build_id
            or dataset.get("contract_version") != expected_contract
            or dataset.get("totals") != entry.get("totals")
            or dataset.get("data_as_of") != entry.get("data_as_of")
            or dataset.get("coverage_status") != entry.get("coverage_status")
        ):
            raise CutoverError(f"legacy dataset manifest disagrees with release: {name}")
        start = dataset.get("coverage_start")
        if not isinstance(start, str):
            raise CutoverError(f"legacy coverage start is invalid: {name}")
        try:
            floors[name] = date.fromisoformat(start)
        except ValueError:
            raise CutoverError(f"legacy coverage start is invalid: {name}") from None
    return floors


def _file_identity(path: Path) -> tuple[tuple[int, int, int, int, int] | None, ...]:
    result: list[tuple[int, int, int, int, int] | None] = []
    for item in (path, path.with_name(f"{path.name}-wal")):
        try:
            stat = item.stat()
        except FileNotFoundError:
            result.append(None)
        else:
            result.append(
                (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            )
    return tuple(result)


class _CandidateWatch:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: sqlite3.Connection | None = None
        self.identity: tuple[tuple[int, int, int, int, int] | None, ...] = ()
        self.data_version = 0

    def __enter__(self) -> _CandidateWatch:
        if (
            not self.path.is_file()
            or self.path.is_symlink()
            or SQLITE_SCHEMA_VERSION != CANDIDATE_SCHEMA
        ):
            raise CutoverError("schema-47 candidate identity is unavailable")
        connection = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True)
        self.connection = connection
        connection.execute("PRAGMA query_only = ON")
        try:
            if connection.execute("PRAGMA user_version").fetchone()[0] != CANDIDATE_SCHEMA:
                raise CutoverError("candidate schema is not 47")
            validate_current_schema(connection)
            if read_lake_store_origin_from_connection(connection) is not None:
                raise CutoverError("fresh candidate must have no lake origin")
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise CutoverError("candidate SQLite quick_check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise CutoverError("candidate SQLite foreign key check failed")
            self.identity = _file_identity(self.path)
            self.data_version = connection.execute("PRAGMA data_version").fetchone()[0]
            return self
        except BaseException:
            connection.close()
            self.connection = None
            raise

    def __exit__(self, *_: object) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def require_unchanged(self) -> None:
        connection = self.connection
        if connection is None:
            raise CutoverError("candidate generation watch is unavailable")
        if (
            _file_identity(self.path) != self.identity
            or connection.execute("PRAGMA data_version").fetchone()[0] != self.data_version
            or connection.execute("PRAGMA user_version").fetchone()[0] != CANDIDATE_SCHEMA
            or read_lake_store_origin_from_connection(connection) is not None
        ):
            raise CutoverError("candidate SQLite changed during preparation or publication")

    def require_prior_history(self, floors: Mapping[str, date]) -> dict[str, dict[str, str]]:
        """Bound this one transition's accepted missing spans before export."""
        connection = self.connection
        if connection is None:
            raise CutoverError("candidate generation watch is unavailable")
        omissions: list[str] = []
        approved: dict[str, dict[str, str]] = {}
        for name, floor in sorted(floors.items()):
            dataset = LAKE_DATASETS.get(name)
            if dataset is None:
                omissions.append(f"{name}: no current dataset contract")
                continue
            # Names come from the fixed in-repository dataset registry, not input.
            row = connection.execute(
                f'SELECT MIN("{dataset.date_column}") FROM "{dataset.sqlite_table}"'
            ).fetchone()
            first = row[0] if row is not None else None
            if first is None:
                omissions.append(f"{name}: prior={floor.isoformat()}, candidate=None")
                continue
            first_date = date.fromisoformat(first)
            if first_date <= floor:
                continue
            allowance = APPROVED_TRANSITION_GAPS.get(name)
            if allowance is None or floor != allowance[0] or first_date > allowance[1]:
                omissions.append(f"{name}: prior={floor.isoformat()}, candidate={first}")
                continue
            approved[name] = {
                "previous_coverage_start": floor.isoformat(),
                "fresh_coverage_start": first,
                "quality": "missing_previous_history",
            }
        self.require_unchanged()
        if omissions:
            raise CutoverError("fresh candidate omits prior history: " + "; ".join(omissions))
        return approved


def publish_fresh_market(
    *,
    candidate: Path,
    mirror: Path,
    store: CutoverStore,
    lease: PublicationLeaseHandle,
    pin: SourcePin = PRODUCTION_SOURCE,
    release_id: str | None = None,
    producer_git_commit: str | None = None,
    recovery_record: Path | None = None,
    verified_fixed_release: bool = False,
) -> dict[str, object]:
    """Publish only a complete fresh L1; leave machine/application/runs unchanged."""
    verify_held(store, lease)
    floors = _legacy_history_floors(store, pin)
    with _CandidateWatch(candidate) as watch:
        history_gaps = watch.require_prior_history(floors)
        published_floors = {
            name: floor for name, floor in floors.items() if name not in history_gaps
        }
        if verified_fixed_release:
            if release_id is not None or producer_git_commit is not None:
                raise CutoverError("verified fixed release does not accept a new release identity")
            if _candidate_hashes(candidate) != (VERIFIED_CANDIDATE_SHA256, None):
                raise CutoverError("candidate differs from the verified fixed release source")
            fixed = resolve_release(
                LocalMirrorSource(mirror),
                VERIFIED_FIXED_RELEASE_ID,
                manifest_sha256=VERIFIED_FIXED_RELEASE_SHA256,
            )
            release_path = lake_mirror_path(
                mirror, lake_release_manifest_key(release_id=VERIFIED_FIXED_RELEASE_ID)
            )
            release = fixed.manifest
            manifests = fixed.dataset_manifests
            validate_lake_release_policy(
                release, manifests, published_coverage_start=published_floors
            )
            largest_object = max(
                (
                    item.bytes
                    for manifest in manifests.values()
                    for part in manifest.partitions
                    for item in part.objects
                ),
                default=0,
            )
            if shutil.disk_usage(tempfile.gettempdir()).free < largest_object + 2 * 1024**3:
                raise CutoverError("insufficient temporary space for upload and readback")
            digest = VERIFIED_FIXED_RELEASE_SHA256
        else:
            export = export_market_store(
                sqlite_path=candidate,
                mirror_root=mirror,
                producer_git_commit=producer_git_commit or lake_verified_git_commit(),
                expected_store_origin=None,
            )
            with _export_manifest_snapshot(export.datasets, mirror) as paths:
                release_path, release = create_lake_release(
                    dataset_manifest_paths=paths,
                    mirror_root=mirror,
                    release_id=release_id,
                    published_coverage_start=published_floors,
                )
            manifests = {name: item.manifest for name, item in export.datasets.items()}
            digest = hashlib.sha256(canonical_lake_model_bytes(release)).hexdigest()
        watch.require_unchanged()
        for name, floor in floors.items():
            manifest = manifests.get(name)
            if manifest is None:
                raise CutoverError(f"fresh release omits prior dataset: {name}")
            if name in history_gaps:
                if (
                    manifest.coverage_start.isoformat()
                    != history_gaps[name]["fresh_coverage_start"]
                ):
                    raise CutoverError(f"fresh release changes approved history gap: {name}")
            elif manifest.coverage_start > floor:
                raise CutoverError(f"fresh release loses prior history: {name}")
        if recovery_record is None:
            raise CutoverError("a private recovery record is required before publication")
        _write_recovery_record(
            recovery_record,
            candidate=candidate,
            release_id=release.release_id,
            digest=digest,
            watch=watch,
            history_gaps=history_gaps,
        )

        def before_switch() -> None:
            verify_held(store, lease)
            watch.require_unchanged()
            _legacy_history_floors(store, pin)

        before_switch()
        published = publish_release(
            mirror_root=mirror,
            release_manifest_path=release_path,
            store=store,
            pointer_precondition=PointerPrecondition(etag=None),
            expected_release_sha256=digest,
            before_pointer_switch=before_switch,
        )
        # A pointer may have switched before an error was raised. Do not infer rollback.
        watch.require_unchanged()
        _legacy_history_floors_after_switch(store, pin)
        current = read_pointer_snapshot(store)
        if (
            current.pointer is None
            or current.pointer.release_id != published.release_id
            or current.pointer.manifest_sha256 != digest
        ):
            raise CutoverError("new pointer differs after publication; origin remains unset")
        verify_held(store, lease)
        advance_lake_store_origin(
            candidate,
            expected=None,
            target=LakeStoreOrigin(published.release_id, digest),
        )
        return {
            "release_id": published.release_id,
            "release_manifest_sha256": digest,
            "prior_release_id": pin.release_id,
            "candidate_schema": CANDIDATE_SCHEMA,
            "old_dataset_count": len(floors),
            "history_gaps": history_gaps,
            "fresh_dataset_count": len(release.datasets),
            "uploaded_objects": published.transfers.uploaded_objects,
            "pointer_etag": current.precondition.etag,
            "market_machine_store_pushed": False,
        }


def recover_committed_origin(
    *,
    candidate: Path,
    mirror: Path,
    record_path: Path,
    store: CutoverStore,
    lease: PublicationLeaseHandle,
    pin: SourcePin = PRODUCTION_SOURCE,
) -> dict[str, object]:
    """Reconcile one already committed pointer; never publish or hydrate a release."""
    record = _dict(record_path.read_bytes(), label="recovery record")
    if (
        set(record)
        != {
            "record_version",
            "candidate",
            "market_sha256",
            "wal_sha256",
            "release_id",
            "manifest_sha256",
            "history_gaps",
        }
        or record.get("record_version") != 1
        or record.get("candidate") != str(candidate.resolve())
        or not isinstance(record.get("history_gaps"), dict)
    ):
        raise CutoverError("recovery record does not identify this candidate")
    release_id = record["release_id"]
    digest = record["manifest_sha256"]
    if not isinstance(release_id, str) or not isinstance(digest, str):
        raise CutoverError("recovery record release identity is invalid")
    verify_held(store, lease)
    observed = read_pointer_snapshot(store)
    pointer = observed.pointer
    if pointer is None or pointer.release_id != release_id or pointer.manifest_sha256 != digest:
        raise CutoverError("current pointer is not the exact committed recovery release")
    _legacy_history_floors_after_switch(store, pin)
    origin = read_lake_store_origin(candidate)
    target = LakeStoreOrigin(release_id, digest)
    if origin is not None:
        raise CutoverError("candidate origin is already set; inspect its existing transition")
    if _candidate_hashes(candidate) != (
        record["market_sha256"],
        record["wal_sha256"],
    ):
        raise CutoverError("candidate differs from its pre-pointer recovery record")
    uri = f"{candidate.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA query_only = ON")
        if connection.execute("PRAGMA user_version").fetchone()[0] != CANDIDATE_SCHEMA:
            raise CutoverError("candidate schema is not 47")
        validate_current_schema(connection)
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise CutoverError("candidate SQLite quick_check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise CutoverError("candidate SQLite foreign key check failed")
        identity = _file_identity(candidate)
        data_version = connection.execute("PRAGMA data_version").fetchone()[0]
        release = _resolve_serving_release(store, mirror, pointer)
        if release.release_id != release_id or release.manifest_sha256 != digest:
            raise CutoverError("resolved release differs from the recovery record")
        for manifest in release.dataset_manifests.values():
            validate_snapshot_parity(sqlite_path=candidate, mirror_root=mirror, manifest=manifest)
        if (
            _file_identity(candidate) != identity
            or connection.execute("PRAGMA data_version").fetchone()[0] != data_version
        ):
            raise CutoverError("candidate changed during recovery verification")
        if _candidate_hashes(candidate) != (
            record["market_sha256"],
            record["wal_sha256"],
        ):
            raise CutoverError("candidate changed during recovery verification")
        verify_held(store, lease)
        latest = read_pointer_snapshot(store)
        if latest.precondition.etag != observed.precondition.etag or latest.pointer != pointer:
            raise CutoverError("current pointer changed during origin recovery")
    advance_lake_store_origin(candidate, expected=None, target=target)
    if read_lake_store_origin(candidate) != target:
        raise CutoverError("candidate origin update outcome is uncertain")
    return {"release_id": release_id, "origin": "advanced_after_readback"}


def _legacy_history_floors_after_switch(store: CutoverStore, pin: SourcePin) -> None:
    """The old source must still be fixed even though the new pointer now exists."""
    old = _read_small(
        store,
        OLD_POINTER_KEY,
        max_bytes=1024,
        expected_etag=pin.pointer_etag,
        expected_sha256=pin.pointer_sha256,
    )
    if _dict(old, label="legacy pointer").get("release_id") != pin.release_id:
        raise CutoverError("legacy pointer changed after new publication")
    market = store.head(MARKET_KEY)
    if market is None or market.etag != pin.market_etag or market.size != pin.market_bytes:
        raise CutoverError("production market store changed after new publication")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--mirror", type=Path, required=True)
    parser.add_argument("--lease-handle", type=Path, required=True)
    parser.add_argument("--release-id")
    parser.add_argument("--verified-fixed-release", action="store_true")
    parser.add_argument("--recovery-record", type=Path, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--publish", action="store_true")
    action.add_argument("--recover-origin", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    os.umask(0o077)
    store = R2CutoverStore(bucket="baibai-stores")
    lease = _load_handle(args.lease_handle)
    if args.recover_origin:
        if args.release_id is not None:
            raise CutoverError("recovery release comes only from the sealed record")
        result = recover_committed_origin(
            candidate=args.candidate,
            mirror=args.mirror,
            record_path=args.recovery_record,
            store=store,
            lease=lease,
        )
    else:
        result = publish_fresh_market(
            candidate=args.candidate,
            mirror=args.mirror,
            store=store,
            lease=lease,
            release_id=args.release_id,
            recovery_record=args.recovery_record,
            verified_fixed_release=args.verified_fixed_release,
        )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
