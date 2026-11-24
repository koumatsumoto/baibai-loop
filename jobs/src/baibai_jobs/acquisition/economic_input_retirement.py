"""One exact cleanup obligation in each economic acquisition input pointer."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from baibai_engine.storage.objects.json import SavedObject, read_fixed_private, read_private
from baibai_engine.storage.objects.r2 import (
    Boto3R2Store,
    ImmutableObjectDeleter,
    LakeCASConflict,
    LakePublishError,
    R2Error,
    RemoteObject,
)
from baibai_jobs.acquisition.economic_staging import _PLANNING_INPUT_FORMATS, stage_prefix


class EconomicRetirementDeferred(LakePublishError):
    """A detached cleanup is uncertain, not an unsuccessful successor readback."""

    def __init__(self, *, accepted_stage: str | None) -> None:
        self.accepted_stage = accepted_stage
        super().__init__("detached economic input cleanup remains pending")


def _detached_failure_can_wait(error: R2Error) -> bool:
    # Do not infer a transport failure from a missing HTTP status. Authorization,
    # throttling, and unrecognized errors are not optional; strict stop bypasses us.
    return error.http_status in {408, 500, 502, 503, 504} or error.transport_uncertain


@dataclass(frozen=True, slots=True)
class _Body:
    key: str
    sha256: str
    size: int


def _manifest_key(stage: str) -> str:
    return stage_prefix(stage, namespace="source-inputs") + "/handoff.json"


def _body(group: str, packet: SavedObject, *, generations: object = None) -> _Body:
    manifest = json.loads(packet.payload)
    result = manifest["result"]
    binding = result["canonical_generations"]
    if (
        result["group"] != group
        or set(binding) != {"economic_etag"}
        or not isinstance(binding["economic_etag"], str)
        or not binding["economic_etag"]
        or (generations is not None and binding != generations)
    ):
        raise ValueError("invalid economic input binding")
    wire = _PLANNING_INPUT_FORMATS.get(group)
    field = "planning_key" if wire else "receipt_key"
    key, digest, size = manifest[field], manifest["receipt_sha256"], manifest["receipt_bytes"]
    if (
        manifest.get("receipt_format") != wire
        or {"receipt_key", "planning_key"}.intersection(manifest) != {field}
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or not isinstance(key, str)
        or re.fullmatch(
            rf"coordination/source-inputs/{re.escape(group)}-"
            rf"[A-Za-z0-9_-]+/receipts-{digest}\.sqlite",
            key,
        )
        is None
        or type(size) is not int
        or size <= 0
    ):
        raise ValueError("invalid economic input physical reference")
    return _Body(key, digest, size)


@dataclass(frozen=True, slots=True)
class RetiredEconomicInput:
    stage_id: str
    manifest_sha256: str
    manifest: SavedObject
    body: _Body

    def record(self) -> dict[str, str]:
        return {"stage_id": self.stage_id, "manifest_sha256": self.manifest_sha256}


def verified_predecessor(
    group: str, stage: str, manifest: SavedObject, *, generations: object
) -> RetiredEconomicInput | None:
    """An unusable old pointer can be replaced but cannot authorize any deletion."""
    if not isinstance(generations, dict):
        return None
    try:
        body = _body(group, manifest, generations=generations)
    except (KeyError, TypeError, ValueError):
        return None
    return RetiredEconomicInput(stage, hashlib.sha256(manifest.payload).hexdigest(), manifest, body)


def outstanding_retirement(
    store: ImmutableObjectDeleter, group: str, value: dict[str, Any]
) -> RetiredEconomicInput | None:
    if "retired" not in value:
        return None
    debt = value["retired"]
    if not isinstance(debt, dict) or debt.keys() != {"stage_id", "manifest_sha256"}:
        raise LakePublishError("invalid economic input cleanup obligation")
    stage, digest = debt["stage_id"], debt["manifest_sha256"]
    if (
        not isinstance(stage, str)
        or not stage.startswith(group + "-")
        or stage == value["stage_id"]
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        raise LakePublishError("invalid economic input cleanup obligation")
    try:
        saved = read_fixed_private(store, _manifest_key(stage), sha256=digest, single_attempt=True)
    except R2Error as error:
        if _detached_failure_can_wait(error):
            raise EconomicRetirementDeferred(accepted_stage=None) from error
        raise
    if saved is None:
        # The last step is manifest deletion, only when the body is absent or shared.
        return None
    return RetiredEconomicInput(stage, digest, saved, _body(group, saved))


def verify_retirement_selection(
    store: ImmutableObjectDeleter,
    pointer_key: str,
    current: SavedObject,
    before_delete: Callable[[], None],
) -> None:
    before_delete()
    if read_private(store, pointer_key) != current:
        raise LakeCASConflict("economic pointer changed before cleanup")


def retire_confirmed_predecessor(
    store: ImmutableObjectDeleter,
    group: str,
    pointer_key: str,
    current: SavedObject,
    retired: RetiredEconomicInput,
    *,
    before_delete: Callable[[], None],
) -> bool:
    """Caller read back this generation; False means only detached cleanup is pending."""
    value = json.loads(current.payload)
    if value["retired"] != retired.record() or value["stage_id"] == retired.stage_id:
        raise LakePublishError("economic cleanup differs from the selected pointer")
    active = read_private(store, _manifest_key(value["stage_id"]))
    if active is None:
        raise LakePublishError("economic cleanup requires the selected manifest")
    active_body = _body(group, active, generations=value["canonical_generations"])

    def head_detached(key: str) -> RemoteObject | None:
        if isinstance(store, Boto3R2Store):
            return store.head(key, single_attempt=True)
        return store.head(key)

    def delete(key: str, size: int, digest: str, *, etag: str | None = None) -> bool:
        try:
            observed = head_detached(key)
        except R2Error as error:
            if _detached_failure_can_wait(error):
                return False
            raise
        if observed is None:
            return True
        if (
            not observed.etag
            or observed.size != size
            or observed.metadata.get("sha256") != digest
            or (etag is not None and observed.etag != etag)
        ):
            raise LakePublishError("economic cleanup member changed")
        verify_retirement_selection(store, pointer_key, current, before_delete)
        # Immutable UUID keys are not reattached after detachment. R2's DELETE
        # is not a second CAS; writer exclusion and the one successor own this.
        try:
            store.delete_object(key)
            after = head_detached(key)
        except R2Error as error:
            if _detached_failure_can_wait(error):
                return False
            raise
        if after is None:
            return True
        if after != observed:
            raise LakePublishError("economic cleanup member changed after deletion")
        return False

    if retired.body.key != active_body.key and not delete(
        retired.body.key, retired.body.size, retired.body.sha256
    ):
        verify_retirement_selection(store, pointer_key, current, before_delete)
        return False
    done = delete(
        _manifest_key(retired.stage_id),
        len(retired.manifest.payload),
        retired.manifest_sha256,
        etag=retired.manifest.etag,
    )
    if not done:
        verify_retirement_selection(store, pointer_key, current, before_delete)
    return done
