"""What the lake keeps, what it may delete, and the pins that override both.

Retention is decided by reachability, never by age alone. Three kinds of root make
an object reachable: the current L1 release and the one before it, the current build
of each L2 dataset and the one before it, and explicit pins. Everything the closure
of those roots does not reach is a deletion candidate; everything it reaches is kept
regardless of how old it is.

Pins exist because a published study or an adopted calibration cohort has to remain
reproducible after the pointer has moved on twice. Pinning every daily generation
would make the store grow without bound, so a pin is an explicit, reasoned object
rather than something a routine run creates.

Deletion is planned before it is performed. ``plan_gc`` produces a plan and a hash of
that plan; applying requires the same hash back, so a plan computed against one state
of the store cannot be applied against another.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from baibai_engine.foundation.filesystem import write_bytes_atomic

from .keys import (
    current_l1_pointer_key,
    current_l2_pointer_key,
    dataset_manifest_key,
    pin_key,
    release_manifest_key,
    validate_dataset_name,
    validate_identifier,
    validate_lake_object_key,
)
from .models import DatasetManifest, ReleaseManifest
from .objects import mirror_path, sha256_bytes
from .release import L1ReleasePointer, canonical_json_bytes

_GRACE_DAYS = 30
_STAGING_GRACE_DAYS = 7


class LakeRetentionError(RuntimeError):
    """A pointer, pin, or deletion plan does not describe a consistent store."""


class L2DatasetPointer(BaseModel):
    """The mutable head of one L2 dataset, keeping its predecessor addressable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pointer_version: Literal[1] = 1
    dataset: str
    build_id: str
    manifest_key: str
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_build_id: str | None = None

    @field_validator("dataset")
    @classmethod
    def validate_dataset(cls, value: str) -> str:
        return validate_dataset_name(value)

    @field_validator("build_id", "previous_build_id")
    @classmethod
    def validate_build(cls, value: str | None) -> str | None:
        return None if value is None else validate_identifier(value, label="build_id")

    @field_validator("manifest_key")
    @classmethod
    def require_dataset_manifest_key(cls, value: str) -> str:
        key = validate_lake_object_key(value)
        if not key.startswith("lake/manifests/datasets/"):
            raise ValueError("L2 pointer must reference a dataset manifest")
        return key


class LakePin(BaseModel):
    """An explicit reason to keep one release or build reachable indefinitely."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pin_version: Literal[1] = 1
    pin_id: str
    target_kind: Literal["l1_release", "l2_build"]
    target_id: str
    dataset: str | None = None
    reason: str = Field(min_length=1, max_length=500)
    owner: str = Field(min_length=1, max_length=100)
    created_at: datetime

    @field_validator("pin_id", "target_id")
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        return validate_identifier(value, label="pin identifier")

    @field_validator("dataset")
    @classmethod
    def validate_dataset(cls, value: str | None) -> str | None:
        return None if value is None else validate_dataset_name(value)

    def require_consistent_target(self) -> LakePin:
        if self.target_kind == "l2_build" and self.dataset is None:
            raise ValueError("an L2 build pin must name its dataset")
        if self.target_kind == "l1_release" and self.dataset is not None:
            raise ValueError("an L1 release pin must not name a dataset")
        return self


def read_l2_pointer(mirror_root: Path, dataset: str) -> L2DatasetPointer | None:
    path = mirror_path(mirror_root, current_l2_pointer_key(dataset=dataset))
    if not path.is_file():
        return None
    try:
        return L2DatasetPointer.model_validate_json(path.read_bytes())
    except ValueError as exc:
        raise LakeRetentionError(f"L2 pointer is invalid: {dataset}: {exc}") from exc


def advance_l2_pointer(
    mirror_root: Path,
    *,
    dataset: str,
    build_id: str,
    manifest_path: Path,
    expected_current_build_id: str | None,
) -> L2DatasetPointer:
    """Move one dataset head, refusing to write over a head someone else moved.

    The expected current build is passed in by the caller that read it, so a second
    writer that published in between is detected instead of silently overwritten.
    This is the local form of the conditional write the R2 publisher performs.
    """

    current = read_l2_pointer(mirror_root, dataset)
    actual = None if current is None else current.build_id
    if actual != expected_current_build_id:
        raise LakeRetentionError(
            f"L2 pointer for {dataset} moved to {actual!r} while this build was in flight"
        )
    if current is not None and current.build_id == build_id:
        return current
    pointer = L2DatasetPointer(
        dataset=dataset,
        build_id=build_id,
        manifest_key=dataset_manifest_key(dataset=dataset, build_id=build_id),
        manifest_sha256=sha256_bytes(manifest_path.read_bytes()),
        previous_build_id=actual,
    )
    path = mirror_path(mirror_root, current_l2_pointer_key(dataset=dataset))
    path.parent.mkdir(parents=True, exist_ok=True)
    # The one mutable byte string in the lake. Everything else is immutable or
    # content addressed and self-heals on a retry; a half-written pointer does not,
    # so it is written beside its target and renamed into place.
    write_bytes_atomic(path, canonical_json_bytes(pointer))
    return pointer


def create_pin(
    mirror_root: Path,
    *,
    pin_id: str,
    target_kind: Literal["l1_release", "l2_build"],
    target_id: str,
    reason: str,
    owner: str,
    dataset: str | None = None,
    created_at: datetime | None = None,
) -> Path:
    pin = LakePin(
        pin_id=pin_id,
        target_kind=target_kind,
        target_id=target_id,
        dataset=dataset,
        reason=reason,
        owner=owner,
        created_at=(created_at or datetime.now(UTC)).astimezone(UTC),
    ).require_consistent_target()
    path = mirror_path(mirror_root, pin_key(pin_id=pin_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_bytes(pin)
    try:
        with path.open("xb") as target:
            target.write(payload)
    except FileExistsError:
        if path.read_bytes() != payload:
            raise LakeRetentionError(
                f"pin already exists with different content: {pin_id}"
            ) from None
    return path


def remove_pin(mirror_root: Path, *, pin_id: str) -> bool:
    path = mirror_path(mirror_root, pin_key(pin_id=pin_id))
    if not path.is_file():
        return False
    path.unlink()
    return True


def read_pins(mirror_root: Path) -> tuple[LakePin, ...]:
    root = mirror_path(mirror_root, "lake/manifests/pins/placeholder.json").parent
    if not root.is_dir():
        return ()
    pins: list[LakePin] = []
    for path in sorted(root.glob("*.json")):
        try:
            pins.append(LakePin.model_validate_json(path.read_bytes()).require_consistent_target())
        except ValueError as exc:
            raise LakeRetentionError(f"pin is invalid: {path.name}: {exc}") from exc
    return tuple(pins)


@dataclass(frozen=True, slots=True)
class GcCandidate:
    key: str
    bytes: int
    age_days: int
    reason: str


@dataclass(slots=True)
class GcPlan:
    """Deletion candidates plus the roots and reachable set they were derived from."""

    plan_hash: str
    roots: tuple[str, ...]
    reachable: tuple[str, ...]
    candidates: tuple[GcCandidate, ...]
    unresolved_roots: tuple[str, ...] = field(default_factory=tuple)

    @property
    def candidate_bytes(self) -> int:
        return sum(item.bytes for item in self.candidates)

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": "lake_gc_plan",
            "plan_hash": self.plan_hash,
            "roots": list(self.roots),
            "reachable_objects": len(self.reachable),
            "candidates": [
                {
                    "key": item.key,
                    "bytes": item.bytes,
                    "age_days": item.age_days,
                    "reason": item.reason,
                }
                for item in self.candidates
            ],
            "candidate_bytes": self.candidate_bytes,
            "unresolved_roots": list(self.unresolved_roots),
        }


def published_l2_datasets(mirror_root: Path) -> tuple[str, ...]:
    """Every L2 dataset the store has a current pointer for.

    Roots are read from the store rather than declared by the caller. A dataset the
    caller forgot to name would otherwise have no root at all, which makes its live
    build look unreferenced — the same failure as an unreadable pointer, arriving
    from the other side.
    """

    root = mirror_path(mirror_root, "lake/pointers/l2/placeholder/current.json").parent.parent
    if not root.is_dir():
        return ()
    return tuple(
        sorted(
            child.name
            for child in root.iterdir()
            if child.is_dir() and (child / "current.json").is_file()
        )
    )


def plan_gc(
    mirror_root: Path,
    *,
    l2_datasets: Sequence[str] = (),
    now: datetime | None = None,
) -> GcPlan:
    """Compute what is reachable from every root, then what is not.

    A root that cannot be resolved is reported rather than skipped. Treating an
    unreadable pointer as "no root" would make everything it protects look
    unreferenced, which is the one way a reachability GC can delete live data. The
    same reasoning is why the L2 roots come from the store's own pointer prefix and
    why canonical objects with no pointer at all leave the plan unappliable.
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
            pointer = L1ReleasePointer.model_validate_json(pointer_path.read_bytes())
        except ValueError as exc:
            raise LakeRetentionError(f"L1 pointer is invalid: {exc}") from exc
        for release_id in (pointer.release_id, pointer.previous_release_id):
            if release_id is not None:
                _reach_release(mirror_root, release_id, reachable, unresolved)
    elif _has_objects_under(mirror_root, "lake/l1/canonical/"):
        # Canonical L1 objects exist but nothing points at them. Every one of them
        # would be unreferenced by construction, so the plan is not safe to apply.
        unresolved.append(current_l1_pointer_key())

    for dataset in sorted(set(published_l2_datasets(mirror_root)) | set(l2_datasets)):
        pointer_key = current_l2_pointer_key(dataset=dataset)
        l2_pointer = read_l2_pointer(mirror_root, dataset)
        if l2_pointer is None:
            if dataset in set(l2_datasets):
                unresolved.append(pointer_key)
            continue
        roots.append(pointer_key)
        reachable.add(pointer_key)
        for build_id in (l2_pointer.build_id, l2_pointer.previous_build_id):
            if build_id is not None:
                _reach_dataset(mirror_root, dataset, build_id, reachable, unresolved)

    for pin in read_pins(mirror_root):
        roots.append(pin_key(pin_id=pin.pin_id))
        reachable.add(pin_key(pin_id=pin.pin_id))
        if pin.target_kind == "l1_release":
            _reach_release(mirror_root, pin.target_id, reachable, unresolved)
        else:
            assert pin.dataset is not None
            _reach_dataset(mirror_root, pin.dataset, pin.target_id, reachable, unresolved)

    candidates = _unreachable(mirror_root, reachable=reachable, now=moment)
    plan_hash = hashlib.sha256(
        json.dumps(
            {
                "roots": sorted(roots),
                "candidates": sorted(item.key for item in candidates),
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
        unresolved_roots=tuple(sorted(unresolved)),
    )


def _reach_release(
    mirror_root: Path, release_id: str, reachable: set[str], unresolved: list[str]
) -> None:
    key = release_manifest_key(release_id=release_id)
    path = mirror_path(mirror_root, key)
    if not path.is_file():
        unresolved.append(key)
        return
    reachable.add(key)
    try:
        release = ReleaseManifest.model_validate_json(path.read_bytes())
    except ValueError as exc:
        raise LakeRetentionError(f"release manifest is invalid: {release_id}: {exc}") from exc
    for dataset, entry in release.datasets.items():
        _reach_dataset(mirror_root, dataset, entry.build_id, reachable, unresolved)


def _reach_dataset(
    mirror_root: Path, dataset: str, build_id: str, reachable: set[str], unresolved: list[str]
) -> None:
    key = dataset_manifest_key(dataset=dataset, build_id=build_id)
    path = mirror_path(mirror_root, key)
    if not path.is_file():
        unresolved.append(key)
        return
    reachable.add(key)
    try:
        manifest = DatasetManifest.model_validate_json(path.read_bytes())
    except ValueError as exc:
        raise LakeRetentionError(
            f"dataset manifest is invalid: {dataset}/{build_id}: {exc}"
        ) from exc
    for partition in manifest.partitions:
        for item in partition.objects:
            reachable.add(item.key)


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
        reason = _candidate_reason(key)
        if reason is None or key in reachable:
            continue
        age_days = (
            max(
                0, int((now - datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)).total_seconds())
            )
            // 86_400
        )
        grace = _STAGING_GRACE_DAYS if reason == "abandoned_staging" else _GRACE_DAYS
        if age_days < grace:
            continue
        candidates.append(
            GcCandidate(key=key, bytes=path.stat().st_size, age_days=age_days, reason=reason)
        )
    return tuple(candidates)


def _has_objects_under(mirror_root: Path, prefix: str) -> bool:
    root = mirror_root.resolve() / prefix
    return root.is_dir() and any(path.is_file() for path in root.rglob("*"))


def _candidate_reason(key: str) -> str | None:
    if key.startswith("lake/staging/"):
        return "abandoned_staging"
    if key.startswith("lake/l1/canonical/"):
        return "unreferenced_l1_object"
    if key.startswith("lake/l2/"):
        return "unreferenced_l2_object"
    if key.startswith("lake/manifests/datasets/"):
        return "unreferenced_dataset_manifest"
    if key.startswith("lake/manifests/releases/"):
        return "unreferenced_release_manifest"
    return None


def apply_gc(mirror_root: Path, plan: GcPlan, *, plan_hash: str) -> tuple[str, ...]:
    """Delete exactly the planned keys, and only against the state they were planned on."""

    if plan_hash != plan.plan_hash:
        raise LakeRetentionError("GC plan hash does not match the plan being applied")
    if plan.unresolved_roots:
        raise LakeRetentionError(
            "GC refuses to delete while a root is unresolved: " + ", ".join(plan.unresolved_roots)
        )
    deleted: list[str] = []
    for candidate in plan.candidates:
        path = mirror_path(mirror_root, candidate.key)
        if path.is_file():
            path.unlink()
            deleted.append(candidate.key)
    return tuple(deleted)
