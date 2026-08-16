"""Immutable L1 release creation and mutable-pointer wire contract."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .immutable import ImmutableInstallError, install_immutable_bytes
from .keys import (
    release_manifest_key,
    validate_identifier,
)
from .models import (
    DatasetManifest,
    ReleaseDataset,
    ReleaseManifest,
    ReleaseProfile,
    canonical_lake_model_bytes,
    load_lake_model_json,
    validate_release_policy,
)


class L1ReleasePointer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    pointer_version: Literal[1] = 1
    release_id: str
    manifest_key: str
    manifest_sha256: str

    @field_validator("release_id")
    @classmethod
    def validate_release_id(cls, value: str) -> str:
        return validate_identifier(value, label="release_id")

    @model_validator(mode="after")
    def validate_identity(self) -> L1ReleasePointer:
        if self.manifest_key != release_manifest_key(release_id=self.release_id):
            raise ValueError("L1 pointer key does not match release_id")
        return self


def create_l1_release(
    *,
    dataset_manifest_paths: list[Path],
    mirror_root: Path,
    release_id: str | None = None,
    created_at: datetime | None = None,
    profile: ReleaseProfile = "shadow",
) -> tuple[Path, ReleaseManifest]:
    if not dataset_manifest_paths:
        raise ValueError("at least one dataset manifest is required")
    root = mirror_root.resolve()
    resolved_paths = [_contained_path(root, path) for path in dataset_manifest_paths]
    manifest_payloads = [path.read_bytes() for path in resolved_paths]
    manifests = [load_lake_model_json(payload, DatasetManifest) for payload in manifest_payloads]
    if any(item.layer != "l1_canonical" for item in manifests):
        raise ValueError("L1 release accepts l1_canonical dataset manifests only")
    datasets = {
        item.dataset: ReleaseDataset(
            build_id=item.build_id,
            contract_version=item.contract_version,
            manifest_sha256=hashlib.sha256(payload).hexdigest(),
            data_as_of=item.data_as_of,
            coverage_status=item.coverage_status,
            totals=item.totals,
        )
        for item, payload in zip(manifests, manifest_payloads, strict=True)
    }
    if len(datasets) != len(manifests):
        raise ValueError("L1 release contains duplicate datasets")
    now = (created_at or datetime.now(UTC)).astimezone(UTC)
    inventory = hashlib.sha256(
        b"".join(payload for _, payload in sorted(zip(datasets, manifest_payloads, strict=True)))
    ).hexdigest()
    actual_release_id = release_id or (
        f"{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}-{inventory[:12]}"
    )
    validate_identifier(actual_release_id, label="release_id")
    release = ReleaseManifest(
        manifest_version=1,
        release_id=actual_release_id,
        profile=profile,
        created_at=now,
        data_as_of=min(item.data_as_of for item in manifests),
        datasets=datasets,
    )
    validate_release_policy(
        release,
        {item.dataset: item for item in manifests},
        evaluated_at=now,
    )
    path = (root / release_manifest_key(release_id=actual_release_id)).resolve()
    if not path.is_relative_to(root):
        raise ValueError("release manifest escapes mirror root")
    payload = canonical_lake_model_bytes(release)
    try:
        install_immutable_bytes(
            path,
            payload,
            validate=lambda value: load_lake_model_json(value, ReleaseManifest),
        )
    except ImmutableInstallError as exc:
        raise ValueError(str(exc)) from exc
    return path, release


def _contained_path(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("dataset manifest escapes mirror root")
    return resolved
