"""Stage immutable Machine members and switch their Lake head exactly once."""

from __future__ import annotations

import base64
import hashlib
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from baibai_engine.machine_analysis.screening.discovery_contract import discovery_publication_id
from baibai_engine.machine_analysis.screening.selection_query import read_publication
from baibai_engine.storage.objects.r2 import (
    Boto3R2Store,
    ImmutableMultipartWriter,
    LakeCASConflict,
    LakePublishError,
    ObjectStore,
    R2Error,
    R2OperationStopped,
    RemoteObject,
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
class ReadyMachineBase:
    """The captured head's former pair is already synchronously absent."""

    snapshot: PointerSnapshot


@dataclass(frozen=True, slots=True)
class StagedMachine:
    """The original head supplies both retirement proof and activation precondition."""

    pointer: LakeReleasePointer
    base: ReadyMachineBase


@dataclass(frozen=True, slots=True)
class MachineCommit:
    current: PointerSnapshot

    @property
    def detached(self) -> tuple[str, ...]:
        return _retirement_keys(self.current.pointer)


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


def _require_sealed_body(path: Path) -> int:
    size = path.stat().st_size
    if not path.is_file() or size == 0:
        raise LakePublishError("machine member must be a nonempty sealed file")
    for suffix in ("-wal", "-journal"):
        try:
            if path.with_name(path.name + suffix).stat().st_size:
                raise LakePublishError("machine member has outstanding SQLite sidecar bytes")
        except FileNotFoundError:
            pass
    return size


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


def _stage_member(
    store: ObjectStore,
    key: str,
    path: Path,
    *,
    expected_size: int,
    multipart_lease: Callable[[], None] | None = None,
) -> MachineMember:
    sha256, md5, size = _checksums(path)
    if size != expected_size:
        raise LakePublishError("sealed machine member size changed before staging")
    if multipart_lease is None:
        ack = store.put_file(
            key,
            path,
            sha256=sha256,
            content_md5=md5,
            content_type=_SQLITE_CONTENT_TYPE,
            if_none_match=True,
        )
    else:
        assert isinstance(store, ImmutableMultipartWriter)
        ack = store.put_immutable_multipart_file(
            key,
            path,
            size=size,
            sha256=sha256,
            content_md5=md5,
            content_type=_SQLITE_CONTENT_TYPE,
            while_held=multipart_lease,
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
    store: ObjectStore,
    *,
    lake: StagedLake,
    base: ReadyMachineBase,
    market: Path,
    runs: Path,
    while_held: Callable[[], None] | None = None,
) -> StagedMachine:
    """Bind verified graph and locally paired snapshots to the original current head."""
    if (
        lake.base is None
        or lake.base != base.snapshot
        or lake.base.payload != base.snapshot.payload
    ):
        raise LakePublishError("machine base differs from the staged Lake")
    market_size = _require_sealed_body(market)
    runs_size = _require_sealed_body(runs)
    if market_size > MAX_MACHINE_PUT_BYTES:
        raise MachineMemberTooLarge("Market exceeds the R2 single PUT limit")
    multipart = runs_size > MAX_MACHINE_PUT_BYTES
    if multipart and (while_held is None or not isinstance(store, ImmutableMultipartWriter)):
        raise MachineMemberTooLarge("Runs exceeds the R2 single PUT limit without multipart")
    require_market_origin(lake.pointer, market)
    require_runs_publication(lake.pointer, runs)
    publication = uuid4().hex
    members: dict[MachineRole, MachineMember] = {}
    paths: dict[MachineRole, Path] = {"market": market, "runs": runs}
    sizes: dict[MachineRole, int] = {"market": market_size, "runs": runs_size}
    for role, path in paths.items():
        key = machine_member_key(publication, role)
        try:
            members[role] = _stage_member(
                store,
                key,
                path,
                expected_size=sizes[role],
                multipart_lease=while_held if multipart and role == "runs" else None,
            )
        except (LakePublishError, OSError, ValueError) as cause:
            if isinstance(cause, R2Error) and cause.http_status in {401, 403, 429}:
                raise
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
        retired_publication_id=_machine_id(base.snapshot.pointer),
    )
    return StagedMachine(pointer=staged, base=base)


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
    candidate, base = staged.pointer, staged.base.snapshot
    members = _machine(candidate)
    prior = _machine_id(base.pointer)
    if members.publication_id == prior or candidate.retired_publication_id != prior:
        raise LakePublishError(
            "machine publication must use fresh keys and identify its former pair"
        )
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
    # One Python call is not enough: S3 region discovery can reissue its signed PUT.
    put_pointer = (
        partial(store.put_file, single_attempt=True)
        if isinstance(store, Boto3R2Store)
        else store.put_file
    )
    with tempfile.NamedTemporaryFile(prefix="baibai-machine-current-", suffix=".json") as temporary:
        temporary.write(payload)
        temporary.flush()
        before_switch()
        try:
            put_pointer(
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
        return MachineCommit(current=current)
    if current.pointer is None:
        # A missing read does not fence an accepted or still in-flight PUT.
        # Only a validated, different head can prove that the candidate lost.
        raise MachineSwitchUnconfirmed("machine current switch is not confirmed") from error
    if _machine_id(current.pointer) == members.publication_id:
        # A receipt-only update is not proof that the selected bodies are detached.
        raise MachineSwitchUnconfirmed("candidate machine remains selected by another receipt")
    if current.precondition != base.precondition:
        raise LakeCASConflict("machine current does not match the candidate") from error
    # Even a conflict-shaped response does not fence a PUT against the same ETag.
    raise MachineSwitchUnconfirmed("machine current switch is not confirmed") from error


def _machine_id(pointer: LakeReleasePointer | None) -> str | None:
    return None if pointer is None or pointer.machine is None else pointer.machine.publication_id


def _retirement_keys(pointer: LakeReleasePointer | None) -> tuple[str, ...]:
    previous = None if pointer is None else pointer.retired_publication_id
    return () if previous is None else tuple(machine_member_key(previous, role) for role in _ROLES)


def _head_detached(store: MachineWriteStore, key: str) -> RemoteObject | None:
    if isinstance(store, Boto3R2Store):
        return store.head(key, single_attempt=True)
    return store.head(key)


def _retire_detached(
    store: MachineWriteStore, keys: tuple[str, ...], *, while_held: Callable[[], None]
) -> None:
    """Only a typed stage/commit may supply keys; they are never reusable."""
    if not keys:
        return
    while_held()
    for key in keys:
        if _head_detached(store, key) is None:
            continue
        store.delete_object(key)
        if _head_detached(store, key) is not None:
            raise LakePublishError("detached machine member remains after deletion")


def finish_machine_retirement(
    store: MachineWriteStore, snapshot: PointerSnapshot, *, while_held: Callable[[], None]
) -> ReadyMachineBase:
    """Discharge at most one former pair before any new publication data is staged."""

    def while_current() -> None:
        while_held()
        observed = read_pointer_snapshot(store)
        if observed != snapshot or observed.payload != snapshot.payload:
            raise LakeCASConflict("machine current changed before completing retirement")

    _retire_detached(store, _retirement_keys(snapshot.pointer), while_held=while_current)
    return ReadyMachineBase(snapshot)


def publish_staged_machine(
    store: MachineWriteStore,
    *,
    lake: StagedLake,
    market: Path,
    runs: Path,
    while_held: Callable[[], None],
    base: ReadyMachineBase,
) -> tuple[MachineCommit, bool]:
    """Publish fresh members and retire only the now-demoted pair, never the old Lake."""
    try:
        staged = stage_machine_members(
            store, lake=lake, base=base, market=market, runs=runs, while_held=while_held
        )
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
