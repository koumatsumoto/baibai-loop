"""What the lake keeps, what it may delete, and the pins that override both.

Retention is decided by reachability, never by age alone. Three kinds of root make
an object reachable: the current L1 release and the one before it, the current
calibration bundle and the one before it, non-calibration L2 dataset heads, and
explicit pins. Everything the closure of those roots does not reach is a deletion
candidate; everything it reaches is kept regardless of how old it is.

Pins exist because a published study or an adopted calibration cohort has to remain
reproducible after the pointer has moved on twice. Pinning every daily generation
would make the store grow without bound, so a pin is an explicit, reasoned object
rather than something a routine run creates.

Deletion is planned before it is performed. ``plan_gc`` produces a plan and a hash of
that plan; applying requires the same hash back, so a plan computed against one state
of the store cannot be applied against another.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from baibai_engine.foundation.filesystem import write_bytes_atomic

from .immutable import ImmutableInstallError, install_immutable_bytes
from .keys import (
    calibration_bundle_manifest_key,
    current_calibration_bundle_pointer_key,
    current_l1_pointer_key,
    dataset_manifest_key,
    pin_key,
    release_manifest_key,
    validate_identifier,
    validate_lake_object_key,
)
from .models import (
    CalibrationBundleManifest as _BundleRootManifest,
)
from .models import (
    CalibrationBundlePointer as _BundlePointer,
)
from .models import (
    CalibrationBundleRef as _BundleRef,
)
from .models import (
    CalibrationInputManifest,
    CalibrationInputSourceRef,
    DatasetManifest,
    RawArchiveMetadata,
    RawIngestSourceRef,
    ReleaseManifest,
    SourceRef,
    load_lake_model_json,
    retained_sources,
)
from .objects import mirror_path, sha256_bytes
from .release import L1ReleasePointer, canonical_json_bytes
from .sources import resolve_source_ref, sha256_file

_GRACE_DAYS = 30
_STAGING_GRACE_DAYS = 7
# Quarantined staging is the only record of what a failed build produced, so it is kept
# long enough to be investigated after the fact, and finite so a repeated large failure
# cannot fill the disk while every manifest-derived figure stays inside its budget.
_QUARANTINE_GRACE_DAYS = 90
_SECOND_SWEEP_GRACE_DAYS = 7
_CANDIDATE_GRACE_DAYS = {
    "abandoned_staging": _STAGING_GRACE_DAYS,
    "expired_quarantine": _QUARANTINE_GRACE_DAYS,
}
_CALIBRATION_DATASETS = frozenset(
    {"calibration.panel", "calibration.panel_diagnostics", "calibration.forward"}
)


class LakeRetentionError(RuntimeError):
    """A pointer, pin, or deletion plan does not describe a consistent store."""


@contextmanager
def exclusive_lock(lock_path: Path, *, subject: str) -> Iterator[None]:
    """Hold one writer at a time over whatever ``lock_path`` stands for."""

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LakeRetentionError(f"another writer holds the {subject} lock") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def lake_writer_lock(mirror_root: Path) -> Iterator[None]:
    """Serialize local publication, pin mutation, and retention finalization."""

    mirror_root.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(mirror_root / ".lake-writer.lock", subject="lake publication"):
        yield


class LakePin(BaseModel):
    """An explicit reason to keep one release or build reachable indefinitely."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pin_version: Literal[1] = 1
    pin_id: str
    target_kind: Literal["l1_release", "calibration_bundle"]
    target_id: str
    manifest_key: str
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=500)
    owner: str = Field(min_length=1, max_length=100)
    created_at: datetime

    @field_validator("pin_id", "target_id")
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        return validate_identifier(value, label="pin identifier")

    def require_consistent_target(self) -> LakePin:
        expected = (
            release_manifest_key(release_id=self.target_id)
            if self.target_kind == "l1_release"
            else calibration_bundle_manifest_key(bundle_id=self.target_id)
        )
        if self.manifest_key != expected:
            raise ValueError("pin manifest key does not match its target identity")
        return self


def create_pin(
    mirror_root: Path,
    *,
    pin_id: str,
    target_kind: Literal["l1_release", "calibration_bundle"],
    target_id: str,
    reason: str,
    owner: str,
    created_at: datetime | None = None,
) -> Path:
    with lake_writer_lock(mirror_root):
        manifest_key = (
            release_manifest_key(release_id=target_id)
            if target_kind == "l1_release"
            else calibration_bundle_manifest_key(bundle_id=target_id)
        )
        manifest_path = mirror_path(mirror_root, manifest_key)
        if not manifest_path.is_file():
            raise LakeRetentionError(f"pin target manifest is missing: {manifest_key}")
        manifest_payload = manifest_path.read_bytes()
        try:
            if target_kind == "l1_release":
                release_manifest = load_lake_model_json(manifest_payload, ReleaseManifest)
                if release_manifest.release_id != target_id:
                    raise LakeRetentionError("pin target release identity differs")
            else:
                bundle_manifest = load_lake_model_json(manifest_payload, _BundleRootManifest)
                if bundle_manifest.bundle_id != target_id:
                    raise LakeRetentionError("pin target bundle identity differs")
        except ValueError as exc:
            raise LakeRetentionError("pin target manifest is invalid") from exc
        pin = LakePin(
            pin_id=pin_id,
            target_kind=target_kind,
            target_id=target_id,
            manifest_key=manifest_key,
            manifest_sha256=sha256_bytes(manifest_payload),
            reason=reason,
            owner=owner,
            created_at=(created_at or datetime.now(UTC)).astimezone(UTC),
        ).require_consistent_target()
        reachable: set[str] = set()
        unresolved: list[str] = []
        if target_kind == "l1_release":
            _reach_release(
                mirror_root,
                target_id,
                reachable,
                unresolved,
                expected_manifest_sha256=pin.manifest_sha256,
            )
        else:
            _reach_bundle(
                mirror_root,
                _BundleRef(
                    bundle_id=target_id,
                    manifest_key=pin.manifest_key,
                    manifest_sha256=pin.manifest_sha256,
                ),
                reachable,
                unresolved,
            )
        if unresolved:
            raise LakeRetentionError(
                "pin target closure is incomplete: " + ", ".join(sorted(unresolved))
            )
        path = mirror_path(mirror_root, pin_key(pin_id=pin_id))
        payload = canonical_json_bytes(pin)
        try:
            install_immutable_bytes(path, payload)
        except ImmutableInstallError as exc:
            raise LakeRetentionError(
                f"pin already exists with different content: {pin_id}"
            ) from exc
        _write_pin_audit(mirror_root, action="create", pin=pin)
        return path


def remove_pin(mirror_root: Path, *, pin_id: str) -> bool:
    with lake_writer_lock(mirror_root):
        path = mirror_path(mirror_root, pin_key(pin_id=pin_id))
        if not path.is_file():
            return False
        try:
            pin = load_lake_model_json(path.read_bytes(), LakePin).require_consistent_target()
        except ValueError as exc:
            raise LakeRetentionError(f"pin is invalid: {pin_id}: {exc}") from exc
        _write_pin_audit(mirror_root, action="remove_requested", pin=pin)
        path.unlink()
        _write_pin_audit(mirror_root, action="remove", pin=pin)
        return True


def _write_pin_audit(mirror_root: Path, *, action: str, pin: LakePin) -> None:
    now = datetime.now(UTC)
    path = (
        mirror_root
        / "lake"
        / "audit"
        / "pins"
        / (f"{now:%Y%m%dT%H%M%S%fZ}-{pin.pin_id}-{action}.json")
    )
    write_bytes_atomic(
        path,
        json.dumps(
            {
                "action": action,
                "recorded_at": now.isoformat(),
                "pin": pin.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n",
    )


def read_pins(mirror_root: Path) -> tuple[LakePin, ...]:
    root = mirror_path(mirror_root, "lake/manifests/pins/placeholder.json").parent
    if not root.is_dir():
        return ()
    pins: list[LakePin] = []
    for path in sorted(root.glob("*.json")):
        try:
            pins.append(
                load_lake_model_json(path.read_bytes(), LakePin).require_consistent_target()
            )
        except ValueError as exc:
            raise LakeRetentionError(f"pin is invalid: {path.name}: {exc}") from exc
    return tuple(pins)


@dataclass(frozen=True, slots=True)
class GcCandidate:
    key: str
    bytes: int
    sha256: str
    age_days: int
    reason: str


@dataclass(slots=True)
class GcPlan:
    """Deletion candidates plus the roots and reachable set they were derived from."""

    plan_hash: str
    roots: tuple[str, ...]
    reachable: tuple[str, ...]
    candidates: tuple[GcCandidate, ...]
    evaluated_at: datetime
    unresolved_roots: tuple[str, ...] = field(default_factory=tuple)

    @property
    def candidate_bytes(self) -> int:
        return sum(item.bytes for item in self.candidates)

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": "lake_gc_plan",
            "plan_hash": self.plan_hash,
            "evaluated_at": self.evaluated_at.isoformat(),
            "roots": list(self.roots),
            "reachable_objects": len(self.reachable),
            "candidates": [
                {
                    "key": item.key,
                    "bytes": item.bytes,
                    "sha256": item.sha256,
                    "age_days": item.age_days,
                    "reason": item.reason,
                }
                for item in self.candidates
            ],
            "candidate_bytes": self.candidate_bytes,
            "unresolved_roots": list(self.unresolved_roots),
        }


def _has_calibration_builds(mirror_root: Path) -> bool:
    """Whether the store holds calibration dataset manifests at all.

    Used only to decide whether a missing bundle pointer is "nothing published yet" or
    "the one root that protects published builds is gone". Treating the second as the
    first is the single way a reachability sweep deletes live data.
    """

    return any(
        _has_objects_under(mirror_root, f"lake/manifests/datasets/{dataset}")
        for dataset in sorted(_CALIBRATION_DATASETS)
    )


def plan_gc(
    mirror_root: Path,
    *,
    now: datetime | None = None,
) -> GcPlan:
    """Compute what is reachable from every root, then what is not.

    A root that cannot be resolved is reported rather than skipped. Treating an
    unreadable pointer as "no root" would make everything it protects look
    unreferenced, which is the one way a reachability GC can delete live data. The
    same reasoning is why a store that holds calibration builds but no bundle pointer
    leaves the plan unappliable rather than treating those builds as unreferenced.
    """

    moment = (now or datetime.now(UTC)).astimezone(UTC)
    roots: list[str] = []
    reachable: set[str] = set()
    unresolved: list[str] = []

    pointer_path = mirror_path(mirror_root, current_l1_pointer_key())
    if pointer_path.is_file():
        roots.append(current_l1_pointer_key())
        reachable.add(current_l1_pointer_key())
        try:
            pointer = load_lake_model_json(pointer_path.read_bytes(), L1ReleasePointer)
        except ValueError as exc:
            raise LakeRetentionError(f"L1 pointer is invalid: {exc}") from exc
        _reach_release(
            mirror_root,
            pointer.release_id,
            reachable,
            unresolved,
            expected_manifest_sha256=pointer.manifest_sha256,
        )
        if pointer.previous_release_id is not None:
            _reach_release(
                mirror_root,
                pointer.previous_release_id,
                reachable,
                unresolved,
                expected_manifest_sha256=pointer.previous_manifest_sha256,
            )
    elif _has_objects_under(mirror_root, "lake/l1/canonical/"):
        # Canonical L1 objects exist but nothing points at them. Every one of them
        # would be unreferenced by construction, so the plan is not safe to apply.
        unresolved.append(current_l1_pointer_key())

    bundle_pointer_path = mirror_path(mirror_root, current_calibration_bundle_pointer_key())
    if bundle_pointer_path.is_file():
        roots.append(current_calibration_bundle_pointer_key())
        reachable.add(current_calibration_bundle_pointer_key())
        try:
            bundle_pointer = load_lake_model_json(bundle_pointer_path.read_bytes(), _BundlePointer)
        except ValueError as exc:
            raise LakeRetentionError(f"calibration bundle pointer is invalid: {exc}") from exc
        _reach_bundle(mirror_root, bundle_pointer.current, reachable, unresolved)
        if bundle_pointer.previous is not None:
            _reach_bundle(mirror_root, bundle_pointer.previous, reachable, unresolved)
    elif _has_calibration_builds(mirror_root):
        unresolved.append(current_calibration_bundle_pointer_key())

    for pin in read_pins(mirror_root):
        roots.append(pin_key(pin_id=pin.pin_id))
        reachable.add(pin_key(pin_id=pin.pin_id))
        if pin.target_kind == "l1_release":
            _reach_release(
                mirror_root,
                pin.target_id,
                reachable,
                unresolved,
                expected_manifest_sha256=pin.manifest_sha256,
            )
        else:
            _reach_bundle(
                mirror_root,
                _BundleRef(
                    bundle_id=pin.target_id,
                    manifest_key=pin.manifest_key,
                    manifest_sha256=pin.manifest_sha256,
                ),
                reachable,
                unresolved,
            )

    candidates = _unreachable(mirror_root, reachable=reachable, now=moment)
    plan_hash = hashlib.sha256(
        json.dumps(
            {
                "roots": sorted(roots),
                "reachable": {
                    key: _reachable_identity(mirror_root, key) for key in sorted(reachable)
                },
                "candidates": [
                    (item.key, item.sha256, item.bytes)
                    for item in sorted(candidates, key=lambda x: x.key)
                ],
                "unresolved": sorted(unresolved),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return GcPlan(
        plan_hash=plan_hash,
        roots=tuple(sorted(roots)),
        reachable=tuple(sorted(reachable)),
        candidates=candidates,
        evaluated_at=moment,
        unresolved_roots=tuple(sorted(unresolved)),
    )


def _reach_release(
    mirror_root: Path,
    release_id: str,
    reachable: set[str],
    unresolved: list[str],
    *,
    expected_manifest_sha256: str | None = None,
) -> None:
    key = release_manifest_key(release_id=release_id)
    path = mirror_path(mirror_root, key)
    if not path.is_file():
        unresolved.append(key)
        return
    payload = path.read_bytes()
    if expected_manifest_sha256 is not None and sha256_bytes(payload) != expected_manifest_sha256:
        unresolved.append(key)
        return
    reachable.add(key)
    try:
        release = load_lake_model_json(payload, ReleaseManifest)
    except ValueError as exc:
        raise LakeRetentionError(f"release manifest is invalid: {release_id}: {exc}") from exc
    for dataset, entry in release.datasets.items():
        _reach_dataset(
            mirror_root,
            dataset,
            entry.build_id,
            reachable,
            unresolved,
            expected_manifest_sha256=entry.manifest_sha256,
        )


def _reach_bundle(
    mirror_root: Path,
    reference: _BundleRef,
    reachable: set[str],
    unresolved: list[str],
) -> None:
    path = mirror_path(mirror_root, reference.manifest_key)
    if not path.is_file() or sha256_file(path) != reference.manifest_sha256:
        unresolved.append(reference.manifest_key)
        return
    reachable.add(reference.manifest_key)
    try:
        manifest = load_lake_model_json(path.read_bytes(), _BundleRootManifest)
    except ValueError as exc:
        raise LakeRetentionError(f"calibration bundle manifest is invalid: {exc}") from exc
    if manifest.bundle_id != reference.bundle_id or set(manifest.datasets) != _CALIBRATION_DATASETS:
        unresolved.append(reference.manifest_key)
        return
    for dataset, entry in manifest.datasets.items():
        _reach_dataset(
            mirror_root,
            dataset,
            entry.build_id,
            reachable,
            unresolved,
            expected_manifest_sha256=entry.manifest_sha256,
        )


def _reach_dataset(
    mirror_root: Path,
    dataset: str,
    build_id: str,
    reachable: set[str],
    unresolved: list[str],
    *,
    expected_manifest_sha256: str | None = None,
) -> None:
    key = dataset_manifest_key(dataset=dataset, build_id=build_id)
    path = mirror_path(mirror_root, key)
    if not path.is_file():
        unresolved.append(key)
        return
    payload = path.read_bytes()
    if expected_manifest_sha256 is not None and sha256_bytes(payload) != expected_manifest_sha256:
        unresolved.append(key)
        return
    reachable.add(key)
    try:
        manifest = load_lake_model_json(payload, DatasetManifest)
    except ValueError as exc:
        raise LakeRetentionError(
            f"dataset manifest is invalid: {dataset}/{build_id}: {exc}"
        ) from exc
    for partition in manifest.partitions:
        _reach_sources(mirror_root, partition.sources, reachable, unresolved)
        for item in partition.objects:
            object_path = mirror_path(mirror_root, item.key)
            if (
                not object_path.is_file()
                or object_path.stat().st_size != item.bytes
                or sha256_file(object_path) != item.sha256
            ):
                unresolved.append(item.key)
            else:
                reachable.add(item.key)
    _reach_sources(mirror_root, manifest.sources, reachable, unresolved)
    for cohort in manifest.cohort_inventory.values():
        _reach_sources(mirror_root, cohort.sources, reachable, unresolved)


def _reach_sources(
    mirror_root: Path,
    sources: Sequence[SourceRef],
    reachable: set[str],
    unresolved: list[str],
) -> None:
    for source in retained_sources(sources):
        try:
            path = resolve_source_ref(mirror_root, source)
        except (OSError, ValueError):
            key = getattr(source, "key", "unknown-source")
            unresolved.append(str(key))
            continue
        reachable.add(source.key)
        if isinstance(source, RawIngestSourceRef):
            reachable.add(source.metadata_key)
        elif isinstance(source, CalibrationInputSourceRef):
            manifest = load_lake_model_json(path.read_bytes(), CalibrationInputManifest)
            reachable.update(item.key for item in manifest.files.values())


def _unreachable(
    mirror_root: Path, *, reachable: Mapping[str, object] | set[str], now: datetime
) -> tuple[GcCandidate, ...]:
    """Everything under the canonical prefixes that no root reaches.

    Raw archives are outside this domain: their retention is decided by class and
    age rather than by manifest reachability, so a reachability sweep must not
    propose them.
    """

    root = mirror_root.resolve()
    candidates: list[GcCandidate] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        key = path.relative_to(root).as_posix()
        try:
            validate_lake_object_key(key)
        except ValueError:
            continue
        if key.startswith("lake/l1/raw/"):
            if not key.endswith(".metadata.json"):
                continue
            try:
                metadata = load_lake_model_json(path.read_bytes(), RawArchiveMetadata)
            except (OSError, ValueError) as exc:
                raise LakeRetentionError(f"Raw retention metadata is invalid: {key}") from exc
            if metadata.retention_class == "preserve":
                continue
            metadata_key = key
            object_key = metadata.object_key
            if metadata_key in reachable or object_key in reachable:
                continue
            age_days = max(0, (now.date() - metadata.retrieved_at.date()).days)
            if age_days < 90:
                continue
            for candidate_key in (object_key, metadata_key):
                candidate_path = mirror_path(mirror_root, candidate_key)
                if not candidate_path.is_file():
                    raise LakeRetentionError(f"Raw retention pair is incomplete: {candidate_key}")
                candidates.append(
                    GcCandidate(
                        key=candidate_key,
                        bytes=candidate_path.stat().st_size,
                        sha256=sha256_file(candidate_path),
                        age_days=age_days,
                        reason="expired_buffer_raw",
                    )
                )
            continue
        reason = _candidate_reason(key)
        if reason is None or key in reachable:
            continue
        age_days = (
            max(
                0, int((now - datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)).total_seconds())
            )
            // 86_400
        )
        grace = _CANDIDATE_GRACE_DAYS.get(reason, _GRACE_DAYS)
        if age_days < grace:
            continue
        candidates.append(
            GcCandidate(
                key=key,
                bytes=path.stat().st_size,
                sha256=sha256_file(path),
                age_days=age_days,
                reason=reason,
            )
        )
    return tuple(candidates)


def _has_objects_under(mirror_root: Path, prefix: str) -> bool:
    root = mirror_root.resolve() / prefix
    return root.is_dir() and any(path.is_file() for path in root.rglob("*"))


def _candidate_reason(key: str) -> str | None:
    if key.startswith("lake/staging/"):
        return "abandoned_staging"
    if key.startswith("lake/quarantine/"):
        return "expired_quarantine"
    if key.startswith("lake/l1/canonical/"):
        return "unreferenced_l1_object"
    if key.startswith("lake/l2/calibration-legacy/"):
        # These bytes are the only recoverable evidence for an incompatible legacy
        # cache, so they are retained independently of a readable L2 bundle.
        return None
    if key.startswith("lake/l2/"):
        return "unreferenced_l2_object"
    if key.startswith("lake/manifests/datasets/"):
        return "unreferenced_dataset_manifest"
    if key.startswith("lake/manifests/releases/"):
        return "unreferenced_release_manifest"
    if key.startswith("lake/manifests/calibration-bundles/"):
        return "unreferenced_calibration_bundle"
    return None


def _reachable_identity(mirror_root: Path, key: str) -> tuple[int, str] | None:
    path = mirror_path(mirror_root, key)
    if not path.is_file():
        return None
    return (path.stat().st_size, sha256_file(path))


def apply_gc(mirror_root: Path, plan: GcPlan, *, plan_hash: str) -> tuple[str, ...]:
    """Mark candidates, then delete only on a verified later sweep under the writer lock."""

    if plan_hash != plan.plan_hash:
        raise LakeRetentionError("GC plan hash does not match the plan being applied")
    with lake_writer_lock(mirror_root):
        fresh = plan_gc(
            mirror_root,
            now=plan.evaluated_at,
        )
        if fresh.plan_hash != plan.plan_hash:
            raise LakeRetentionError(
                "GC root generation or candidate identity changed after planning"
            )
        if fresh.unresolved_roots:
            raise LakeRetentionError(
                "GC refuses to delete while a root is unresolved: "
                + ", ".join(fresh.unresolved_roots)
            )
        candidate_keys = {candidate.key for candidate in fresh.candidates}
        marks_root = mirror_root / "lake" / "retention" / "marks"
        if marks_root.is_dir():
            for marker in sorted(marks_root.glob("*.json")):
                try:
                    marked = json.loads(marker.read_bytes())
                    marked_key = marked["key"]
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    raise LakeRetentionError(f"GC mark is invalid: {marker.name}") from exc
                if not isinstance(marked_key, str):
                    raise LakeRetentionError(f"GC mark is invalid: {marker.name}")
                if marked_key not in candidate_keys:
                    marker.unlink()
        deleted: list[str] = []
        for candidate in fresh.candidates:
            marker = (
                mirror_root
                / "lake"
                / "retention"
                / "marks"
                / (hashlib.sha256(candidate.key.encode()).hexdigest() + ".json")
            )
            identity = {
                "key": candidate.key,
                "sha256": candidate.sha256,
                "bytes": candidate.bytes,
            }
            if not marker.is_file():
                marker.parent.mkdir(parents=True, exist_ok=True)
                write_bytes_atomic(
                    marker,
                    json.dumps(
                        {**identity, "marked_at": fresh.evaluated_at.isoformat()},
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                    + b"\n",
                )
                continue
            try:
                marked = json.loads(marker.read_bytes())
                marked_at = datetime.fromisoformat(marked["marked_at"])
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise LakeRetentionError(f"GC mark is invalid: {candidate.key}") from exc
            if {key: marked.get(key) for key in identity} != identity:
                raise LakeRetentionError(
                    f"GC candidate identity changed after marking: {candidate.key}"
                )
            age = (fresh.evaluated_at - marked_at.astimezone(UTC)).total_seconds()
            if age < _SECOND_SWEEP_GRACE_DAYS * 86_400:
                continue
            path = mirror_path(mirror_root, candidate.key)
            if path.is_file():
                if path.stat().st_size != candidate.bytes or sha256_file(path) != candidate.sha256:
                    raise LakeRetentionError(
                        f"GC candidate identity changed before deletion: {candidate.key}"
                    )
                path.unlink()
                deleted.append(candidate.key)
            marker.unlink(missing_ok=True)
        return tuple(deleted)
