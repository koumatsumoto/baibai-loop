"""Stage immutable Machine members and switch their Lake head exactly once."""

from __future__ import annotations

import base64
import hashlib
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from baibai_engine.machine_analysis.screening.discovery_contract import discovery_publication_id
from baibai_engine.machine_analysis.screening.selection_query import read_publication
from baibai_engine.storage.objects.r2 import (
    LakeCASConflict,
    LakePublishError,
    ObjectStore,
    R2Error,
    R2OperationStopped,
    VerifiedObjectReader,
)
from baibai_engine.storage.snapshots.keys import current_release_pointer_key
from baibai_engine.storage.snapshots.models import canonical_lake_model_bytes
from baibai_engine.storage.snapshots.objects import MAX_BUFFERED_OBJECT_BYTES
from baibai_engine.storage.snapshots.release import (
    LakeReleasePointer,
    MachineMember,
    MachineMembers,
    MachineRole,
    machine_member_key,
)
from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    LakeStoreOrigin,
    read_lake_store_origin,
)
from baibai_jobs.publication.lease import LeaseError
from baibai_jobs.publication.snapshot_release import (
    PointerSnapshot,
    StagedLake,
    read_pointer_snapshot,
)

_ROLES: tuple[MachineRole, ...] = ("market", "runs")
_SQLITE_CONTENT_TYPE = "application/vnd.sqlite3"
# Cloudflare's actual single-request cap is 5 GiB less 5 MiB.
MAX_MACHINE_PUT_BYTES = 5 * 1024**3 - 5 * 1024**2


class MachineWriteStore(ObjectStore, Protocol):
    def delete_object(self, key: str) -> None: ...


class MachineMemberUnavailable(LakePublishError):
    """An active or staged member disappeared or no longer matches its head."""


class MachineMemberTooLarge(LakePublishError):
    """The complete local transfer exceeds this transport's supported size."""


class MachineSwitchUnconfirmed(LakePublishError):
    """The candidate is not proven inactive; the interrupted PUT might still commit."""


class MachineStageInterrupted(LakePublishError):
    """The stage never reached CAS; only completed ACKs can be retired now."""

    def __init__(self, *, detached: tuple[str, ...], unconfirmed: str) -> None:
        super().__init__("machine member staging stopped before publication")
        self.detached = detached
        self.unconfirmed = unconfirmed


@dataclass(frozen=True, slots=True)
class StagedMachine:
    """A candidate is inseparable from the head against which it was prepared."""

    pointer: LakeReleasePointer
    base: PointerSnapshot


@dataclass(frozen=True, slots=True)
class MachineCommit:
    current: PointerSnapshot
    detached: tuple[str, ...]


def _machine(pointer: LakeReleasePointer | None) -> MachineMembers:
    if pointer is None or pointer.machine is None:
        raise MachineMemberUnavailable("current does not name a machine pair")
    return pointer.machine


def _require_members(
    store: ObjectStore, members: MachineMembers, roles: tuple[MachineRole, ...]
) -> None:
    for role in roles:
        item = members.member(role)
        remote = store.head(item.key)
        if remote is None or remote.etag != item.etag or remote.size != item.size:
            raise MachineMemberUnavailable(f"{role} member does not match the machine head")


def read_machine_current(
    store: ObjectStore, *, roles: tuple[MachineRole, ...] = _ROLES
) -> PointerSnapshot:
    """Resolve one head; retry once only when a newer head may have detached members."""
    captured = read_pointer_snapshot(store)
    members = _machine(captured.pointer)
    try:
        _require_members(store, members, roles)
    except MachineMemberUnavailable:
        replacement = read_pointer_snapshot(store)
        if replacement.precondition == captured.precondition:
            raise
        _require_members(store, _machine(replacement.pointer), roles)
        return replacement
    return captured


def machine_publication_ids(pointer: LakeReleasePointer) -> tuple[str, str]:
    generation_id = f"models-{pointer.release_id}"
    return generation_id, discovery_publication_id(generation_id)


def require_market_origin(pointer: LakeReleasePointer, market: Path) -> None:
    if read_lake_store_origin(market) != LakeStoreOrigin(
        pointer.release_id, pointer.manifest_sha256
    ):
        raise LakePublishError("Market origin differs from the selected Lake release")


def require_runs_publication(pointer: LakeReleasePointer, runs: Path) -> None:
    generation_id, selection_id = machine_publication_ids(pointer)
    stored = read_publication(path=runs, selection_id=selection_id)
    if stored is None or stored.generation.generation_id != generation_id:
        raise LakePublishError("Runs lacks the selection for the selected Lake release")
    market = stored.generation.inputs.get("market")
    if (
        market is None
        or market.kind != "lake_release"
        or market.reference != pointer.release_id
        or market.sha256 != pointer.manifest_sha256
    ):
        raise LakePublishError("Runs generation differs from the selected Lake release")


def _require_sealed_body(path: Path) -> None:
    size = path.stat().st_size
    if size > MAX_MACHINE_PUT_BYTES:
        raise MachineMemberTooLarge("machine member exceeds the R2 single PUT limit")
    if not path.is_file() or size == 0:
        raise LakePublishError("machine member must be a nonempty sealed file")
    for suffix in ("-wal", "-journal"):
        try:
            if path.with_name(path.name + suffix).stat().st_size:
                raise LakePublishError("machine member has outstanding SQLite sidecar bytes")
        except FileNotFoundError:
            pass


def _checksums(path: Path) -> tuple[str, str, int]:
    sha = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(8 * 1024 * 1024):
            sha.update(chunk)
            md5.update(chunk)
            size += len(chunk)
    if size == 0:
        raise LakePublishError("cannot stage an empty machine member")
    return sha.hexdigest(), base64.b64encode(md5.digest()).decode(), size


def _stage_member(store: ObjectStore, key: str, path: Path) -> MachineMember:
    sha256, md5, size = _checksums(path)
    ack = store.put_file(
        key,
        path,
        sha256=sha256,
        content_md5=md5,
        content_type=_SQLITE_CONTENT_TYPE,
        if_none_match=True,
    )
    if (
        ack.size != size
        or ack.metadata.get("sha256") != sha256
        or ack.metadata.get("content-md5") != md5
        or ack.content_type != _SQLITE_CONTENT_TYPE
    ):
        raise LakePublishError("staged machine member does not match the submitted bytes")
    return MachineMember(key=key, etag=ack.etag, size=size)


def stage_machine_members(
    store: ObjectStore, *, lake: StagedLake, market: Path, runs: Path
) -> StagedMachine:
    """Bind verified graph and locally paired snapshots to the original current head."""
    if lake.base is None:
        raise ValueError("machine staging requires the head captured before preparation")
    _require_sealed_body(market)
    _require_sealed_body(runs)
    require_market_origin(lake.pointer, market)
    require_runs_publication(lake.pointer, runs)
    publication = uuid4().hex
    members: dict[MachineRole, MachineMember] = {}
    paths: dict[MachineRole, Path] = {"market": market, "runs": runs}
    for role, path in paths.items():
        key = machine_member_key(publication, role)
        try:
            members[role] = _stage_member(store, key, path)
        except (LakePublishError, OSError, ValueError) as cause:
            # These fresh keys cannot be selected: no StagedMachine was returned.
            # A timed-out PUT can still create the unconfirmed key later.
            raise MachineStageInterrupted(
                detached=tuple(member.key for member in members.values()), unconfirmed=key
            ) from cause
    staged = LakeReleasePointer(
        pointer_version=2,
        release_id=lake.pointer.release_id,
        manifest_key=lake.pointer.manifest_key,
        manifest_sha256=lake.pointer.manifest_sha256,
        machine=MachineMembers(market=members["market"], runs=members["runs"]),
    )
    return StagedMachine(pointer=staged, base=lake.base)


def _require_staged_lake(store: ObjectStore, candidate: LakeReleasePointer) -> None:
    key, digest = candidate.manifest_key, candidate.manifest_sha256
    # This is the small manifest uploaded last by stage_release, not the full graph.
    if isinstance(store, VerifiedObjectReader):
        present = (
            store.get_verified_bytes(key, sha256=digest, max_bytes=MAX_BUFFERED_OBJECT_BYTES)
            is not None
        )
    else:
        meta = store.head(key)
        present = (
            meta is not None
            and 0 < meta.size <= MAX_BUFFERED_OBJECT_BYTES
            and hashlib.sha256(store.get_bytes(key)).hexdigest() == digest
        )
    if not present:
        raise LakePublishError("candidate lake manifest is not staged or has changed")


def activate_machine(
    store: ObjectStore,
    staged: StagedMachine,
    *,
    before_switch: Callable[[], None],
) -> MachineCommit:
    """Accept only a staged graph and pair; the caller owns the lease and any cleanup.

    An initial migration checks the legacy pair before invoking this operation. The
    same CAS is used for every subsequent publication; readers never fall back.
    """
    candidate, base = staged.pointer, staged.base
    members = _machine(candidate)
    prior = None if base.pointer is None else base.pointer.machine
    detached = () if prior is None else tuple(prior.member(role).key for role in _ROLES)
    if set(detached) & {members.member(role).key for role in _ROLES}:
        raise LakePublishError("a new machine publication must use fresh member keys")
    if (
        base.pointer is not None
        and base.pointer.release_id == candidate.release_id
        and (prior is not None or base.pointer.manifest_sha256 != candidate.manifest_sha256)
    ):
        raise LakePublishError("a new machine publication must use a new Lake release")
    _require_staged_lake(store, candidate)
    _require_members(store, members, _ROLES)

    payload = canonical_lake_model_bytes(candidate)
    etag = base.precondition.etag
    key = current_release_pointer_key()
    error: LakePublishError | None = None
    with tempfile.NamedTemporaryFile(prefix="baibai-machine-current-", suffix=".json") as temporary:
        temporary.write(payload)
        temporary.flush()
        before_switch()
        try:
            store.put_file(
                key,
                Path(temporary.name),
                sha256=hashlib.sha256(payload).hexdigest(),
                content_md5=base64.b64encode(
                    hashlib.md5(payload, usedforsecurity=False).digest()
                ).decode(),
                content_type="application/json",
                if_match=etag,
                if_none_match=etag is None,
            )
        except LakePublishError as cause:
            if isinstance(cause, R2Error) and cause.http_status in {401, 403, 429}:
                raise
            error = cause

    try:
        current = read_pointer_snapshot(store)
    except LakePublishError as cause:
        raise MachineSwitchUnconfirmed("machine current switch is not confirmed") from cause
    if current.payload == payload:
        return MachineCommit(current=current, detached=detached)
    if isinstance(error, LakeCASConflict) or current.precondition != base.precondition:
        raise LakeCASConflict("machine current does not match the candidate") from error
    # A read of the predecessor does not cancel an earlier in-flight request.
    raise MachineSwitchUnconfirmed("machine current switch is not confirmed") from error


def _retire_detached(
    store: MachineWriteStore, keys: tuple[str, ...], *, while_held: Callable[[], None]
) -> None:
    """Only a typed stage/commit may supply keys; they are never reusable."""
    if not keys:
        return
    while_held()
    for key in keys:
        store.delete_object(key)
        if store.head(key) is not None:
            raise LakePublishError("detached machine member remains after deletion")


def publish_staged_machine(
    store: MachineWriteStore,
    *,
    lake: StagedLake,
    market: Path,
    runs: Path,
    while_held: Callable[[], None],
) -> tuple[MachineCommit, bool]:
    """Publish fresh members and retire only the now-demoted pair, never the old Lake."""
    try:
        staged = stage_machine_members(store, lake=lake, market=market, runs=runs)
    except MachineStageInterrupted as error:
        # The original publication failure is still the outcome.
        with suppress(LakePublishError, LeaseError, R2OperationStopped):
            _retire_detached(store, error.detached, while_held=while_held)
        raise

    try:
        committed = activate_machine(store, staged, before_switch=while_held)
    except LakeCASConflict:
        # The observed head already rejects this attempt's original CAS. Its fresh
        # keys cannot become current later, including through a new base.
        candidate = _machine(staged.pointer)
        with suppress(LakePublishError, LeaseError, R2OperationStopped):
            _retire_detached(
                store, tuple(candidate.member(role).key for role in _ROLES), while_held=while_held
            )
        raise

    try:
        _retire_detached(store, committed.detached, while_held=while_held)
    except (LakePublishError, LeaseError, R2OperationStopped):
        # Publication is already confirmed. Let source seals and serving complete;
        # report the cleanup failure rather than mislabeling the joint CAS.
        return committed, False
    return committed, True
