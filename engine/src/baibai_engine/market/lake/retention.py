"""What the lake keeps and what it may delete.

Retention is decided by reachability, never by age alone. The roots are the current L1
release and the current calibration bundle. Everything the closure of those roots does
not reach is a deletion candidate; everything it reaches is kept regardless of how old
it is.

Both roots are live, and the bundle one is not about R2. This runs against any directory
shaped like a mirror, and the local calibration store is one: it writes `lake/l2/` objects
and switches its own bundle pointer inside its own root. Dropping the bundle root because
no publication carries L2 content to R2 would leave that store with no roots at all, and
every object in it a candidate once the grace period passes.

There is no rollback root. Repair here means publishing forward from a local mirror that
holds the whole graph, not stepping backwards to a generation the store was asked to
stop serving — and a pointer that names a generation as restorable is a promise someone
has to keep verifying on every publication.

There is deliberately no per-dataset L2 head among them. An L2 dataset is published as
part of a bundle and reached through it, so a second pointer naming the same builds
would be a second mutable statement of what the store serves, and only one of the two
can be carried into the next generation. An L2 dataset that is not part of the
calibration bundle needs its own authority designed before it can be a root.

There is no mechanism to keep a generation reachable indefinitely. Reproducing a
published study from the exact bytes it read is not a capability this store offers:
the report is the record, and holding every generation a report ever named is how a
store stops being able to say what it currently serves.

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

from .keys import (
    L1_RELEASE_PREFIX,
    current_calibration_bundle_pointer_key,
    current_l1_pointer_key,
    dataset_manifest_key,
    release_manifest_key,
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
    DatasetManifest,
    ReleaseManifest,
    RetainedSourceRef,
    SourceRef,
    load_lake_model_json,
    retained_sources,
)
from .objects import mirror_path, sha256_bytes
from .release import L1ReleasePointer
from .sources import resolve_source_ref, sha256_file, verified_source_scope

_GRACE_DAYS = 30
_STAGING_GRACE_DAYS = 7
_CANDIDATE_GRACE_DAYS = {"abandoned_staging": _STAGING_GRACE_DAYS}
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
    """Serialize local publication and retention finalization.

    Holding the lock is also what bounds the source verification scope: no other writer
    can change an immutable source while it is held, so one verification of a given
    identity stands for the whole operation instead of once per reference to it.
    """

    mirror_root.mkdir(parents=True, exist_ok=True)
    with (
        exclusive_lock(mirror_root / ".lake-writer.lock", subject="lake publication"),
        verified_source_scope(),
    ):
        yield


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

    Planning opens its own source verification scope. Reachability walks the same
    archive once per cohort that names it — 81 cohorts over three datasets against one
    500 MB archive is over 100 GB of hashing — and a dry run is the form of this command
    an operator is expected to run often. The scope makes the cost proportional to the
    distinct sources in the closure. It is opened here rather than left to the caller
    because the read-only plan is reached without the writer lock, which is where every
    other scope in the lake comes from; nesting inside that lock is harmless.
    """

    with verified_source_scope():
        return _plan_gc(mirror_root, now=now)


def _plan_gc(mirror_root: Path, *, now: datetime | None) -> GcPlan:
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
    elif _has_calibration_builds(mirror_root):
        unresolved.append(current_calibration_bundle_pointer_key())

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
    sources: Sequence[SourceRef | RetainedSourceRef],
    reachable: set[str],
    unresolved: list[str],
) -> None:
    """Mark what this mirror keeps for these sources, and only what it keeps.

    A cohort in the calibration store names the L1 release its rows came from, and that
    release is in the market mirror. This planner speaks for one mirror: calling a key
    it has never published "unresolved" would stop the calibration sweep on a fact about
    a different store, and calling it "reachable" would claim to protect bytes it does
    not hold. It is neither — the market mirror's own sweep answers for it.

    Absence alone does not decide that. A key under a namespace this mirror does publish
    is its own business, and a missing one there is the loss the unresolved list exists
    to report.
    """

    for source in retained_sources(sources):
        if source.key.startswith(L1_RELEASE_PREFIX) and not _publishes(
            mirror_root, L1_RELEASE_PREFIX
        ):
            continue
        try:
            resolve_source_ref(mirror_root, source)
        except (OSError, ValueError):
            unresolved.append(source.key)
            continue
        reachable.add(source.key)


def _publishes(mirror_root: Path, prefix: str) -> bool:
    """Whether this mirror holds anything under a namespace, so it can speak for it."""

    directory = mirror_root / prefix
    return directory.is_dir() and any(directory.iterdir())


def _unreachable(
    mirror_root: Path, *, reachable: Mapping[str, object] | set[str], now: datetime
) -> tuple[GcCandidate, ...]:
    """Everything under the canonical prefixes that no root reaches."""

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
    if key.startswith("lake/l1/canonical/"):
        return "unreferenced_l1_object"
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
    """Delete a plan's candidates under the writer lock, after recomputing it.

    Everything that could change between planning and deleting is checked here rather
    than waited out. The plan hash binds this call to the plan the operator read; the
    lock means nothing else can publish while it runs; the plan is recomputed inside the
    lock against the store's actual roots; an unresolved root refuses the whole sweep;
    and each candidate's bytes are verified immediately before unlinking. A candidate
    that became reachable in between changes the recomputed plan, so it never reaches
    the loop.

    Nothing is gained by writing marks and returning to delete a week later. The race is
    already excluded, and a planner that is wrong about reachability computes the same
    wrong answer on the second run — the delay would only make a single-operator store
    need two scheduled runs to finish collecting, which is how a retention policy stops
    being run at all. The 30 day grace before an object becomes a candidate is where the
    waiting belongs.
    """

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
        deleted: list[str] = []
        for candidate in fresh.candidates:
            path = mirror_path(mirror_root, candidate.key)
            if not path.is_file():
                continue
            if path.stat().st_size != candidate.bytes or sha256_file(path) != candidate.sha256:
                raise LakeRetentionError(
                    f"GC candidate identity changed before deletion: {candidate.key}"
                )
            path.unlink()
            deleted.append(candidate.key)
        return tuple(deleted)
