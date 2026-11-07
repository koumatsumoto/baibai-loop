"""固定した一覧と原本の組を共有し、受領時計とscopeをそのまま保持する。"""

from __future__ import annotations

import base64
import hashlib
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, field_validator

from baibai_engine.data_collection.providers.edinet.archive_native import FixedEdinetArchives
from baibai_engine.data_collection.providers.edinet.list_native import FixedEdinetLists
from baibai_engine.machine_analysis.generation.contracts import Digest, InputSnapshot
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.objects.r2 import LakePublishError, ObjectStore

INPUT_FORMAT: Final = "edinet-input-v1"
INPUT_PREFIX = "fixed-inputs/edinet/v1/"


class _Descriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["edinet-input-v1"] = INPUT_FORMAT
    selection_sha256: Digest


class _Selection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["edinet-selection-v1"] = "edinet-selection-v1"
    list_sha256: Digest
    archive_sha256: Digest
    selected_at: AwareDatetime

    @field_validator("selected_at")
    @classmethod
    def utc_clock(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("EDINET selection clock must be UTC")
        return value


@dataclass(frozen=True, slots=True)
class EdinetModelInput:
    identity: InputSnapshot
    lists: FixedEdinetLists
    archives: FixedEdinetArchives
    selected_at: datetime


def _key(kind: str, digest: str) -> str:
    return INPUT_PREFIX + kind + "/" + digest + ".json"


def _read(store: ObjectStore, kind: str, digest: str) -> bytes:
    key = _key(kind, digest)
    metadata = store.head(key)
    if metadata is None or metadata.metadata.get("sha256") != digest:
        raise LakePublishError("fixed EDINET input is unavailable or changed")
    payload = store.get_bytes(key)
    if len(payload) != metadata.size or hashlib.sha256(payload).hexdigest() != digest:
        raise LakePublishError("fixed EDINET input failed integrity")
    return payload


def _retain(store: ObjectStore, kind: str, value: dict[str, Any]) -> str:
    payload = canonical_json(value).encode()
    digest = hashlib.sha256(payload).hexdigest()
    key = _key(kind, digest)
    if store.head(key) is None:
        with tempfile.TemporaryDirectory(prefix=".edinet-input-") as temporary:
            path = Path(temporary) / "input.json"
            path.write_bytes(payload)
            try:
                store.put_file(
                    key,
                    path,
                    sha256=digest,
                    content_md5=base64.b64encode(
                        hashlib.md5(payload, usedforsecurity=False).digest()
                    ).decode(),
                    content_type="application/json",
                    if_none_match=True,
                )
            except LakePublishError:
                # A lost acknowledgement only succeeds when these exact bytes survived.
                if store.head(key) is None:
                    raise
    if _read(store, kind, digest) != payload:
        raise LakePublishError("fixed EDINET input conflicts with retained bytes")
    return digest


def _validate_receipt_clocks(
    lists: FixedEdinetLists, archives: FixedEdinetArchives, selected_at: datetime
) -> None:
    for receipt in lists.value()["receipts"]:
        if datetime.fromisoformat(receipt["received_at_utc"]) > selected_at:
            raise ValueError("EDINET list receipt is later than its selection")
    for doc_id, source_type in archives.archive_keys:
        metadata = archives.metadata(doc_id, source_type)
        assert metadata is not None
        if metadata.stored_on > selected_at.date() or (
            metadata.received_at_utc is not None and metadata.received_at_utc > selected_at
        ):
            raise ValueError("EDINET archive receipt is later than its selection")


def retain_edinet_input(
    store: ObjectStore,
    lists: FixedEdinetLists,
    archives: FixedEdinetArchives,
    *,
    selected_at: datetime,
) -> InputSnapshot:
    """Retain metadata only; the two owners need not have matching receipt clocks."""
    if selected_at.utcoffset() is None:
        raise ValueError("EDINET selection clock requires a timezone")
    selected_at = selected_at.astimezone(UTC)
    _validate_receipt_clocks(lists, archives, selected_at)
    selection = _Selection(
        list_sha256=_retain(store, "lists", lists.value()),
        archive_sha256=_retain(store, "archives", archives.value()),
        selected_at=selected_at,
    )
    descriptor = _Descriptor(
        selection_sha256=_retain(store, "selections", selection.model_dump(mode="json"))
    ).model_dump(mode="json")
    digest = hashlib.sha256(canonical_json(descriptor).encode()).hexdigest()
    return InputSnapshot(
        kind="native_l1", reference="edinet-" + digest, sha256=digest, descriptor=descriptor
    )


def read_edinet_input(store: ObjectStore, saved: InputSnapshot) -> EdinetModelInput:
    """Recover the saved membership and clocks without current heads, L2, or HTTP."""
    saved = InputSnapshot.model_validate_json(saved.model_dump_json())
    if saved.kind != "native_l1" or saved.reference != "edinet-" + saved.sha256:
        raise ValueError("a digest-verified native EDINET input is required")
    descriptor = _Descriptor.model_validate(saved.descriptor)
    selection = _Selection.model_validate_json(
        _read(store, "selections", descriptor.selection_sha256)
    )
    lists = FixedEdinetLists(store, _read(store, "lists", selection.list_sha256))
    archives = FixedEdinetArchives(store, _read(store, "archives", selection.archive_sha256))
    if lists.identity != selection.list_sha256 or archives.identity != selection.archive_sha256:
        raise ValueError("fixed EDINET heads are not canonical")
    _validate_receipt_clocks(lists, archives, selection.selected_at)
    return EdinetModelInput(saved, lists, archives, selection.selected_at)
