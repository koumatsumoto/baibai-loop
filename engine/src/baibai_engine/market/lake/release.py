"""Immutable L1 release creation and mutable-pointer contract."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .keys import release_manifest_key, validate_lake_object_key
from .models import DatasetManifest, ReleaseDataset, ReleaseManifest


class L1ReleasePointer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pointer_version: Literal[1] = 1
    release_id: str
    manifest_key: str
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_release_id: str | None = None

    @field_validator("manifest_key")
    @classmethod
    def require_release_manifest_key(cls, value: str) -> str:
        key = validate_lake_object_key(value)
        if not key.startswith("lake/manifests/releases/l1/"):
            raise ValueError("L1 pointer must reference an L1 release manifest")
        return key


def create_l1_release(
    *,
    dataset_manifest_paths: list[Path],
    mirror_root: Path,
    release_id: str | None = None,
    created_at: datetime | None = None,
) -> tuple[Path, ReleaseManifest]:
    if not dataset_manifest_paths:
        raise ValueError("at least one dataset manifest is required")
    payloads = [path.read_bytes() for path in dataset_manifest_paths]
    manifests = [DatasetManifest.model_validate_json(payload) for payload in payloads]
    if any(item.layer != "l1_canonical" for item in manifests):
        raise ValueError("L1 release accepts l1_canonical dataset manifests only")
    datasets = {
        item.dataset: ReleaseDataset(
            build_id=item.build_id,
            contract_version=item.contract_version,
            manifest_sha256=hashlib.sha256(payload).hexdigest(),
        )
        for item, payload in zip(manifests, payloads, strict=True)
    }
    if len(datasets) != len(manifests):
        raise ValueError("L1 release contains duplicate datasets")
    now = (created_at or datetime.now(UTC)).astimezone(UTC)
    inventory = hashlib.sha256(
        json.dumps(
            {name: value.model_dump(mode="json") for name, value in sorted(datasets.items())},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    actual_release_id = release_id or (
        f"{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}-{inventory[:12]}"
    )
    release = ReleaseManifest(
        manifest_version=1,
        release_id=actual_release_id,
        created_at=now,
        data_as_of=min(item.data_as_of for item in manifests),
        datasets=datasets,
    )
    path = mirror_root / release_manifest_key(release_id=actual_release_id)
    payload = canonical_json_bytes(release)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as target:
            target.write(payload)
    except FileExistsError:
        if path.read_bytes() != payload:
            raise ValueError(f"immutable release manifest already differs: {path}") from None
    return path, release


def canonical_json_bytes(model: BaseModel) -> bytes:
    return (
        json.dumps(
            model.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
