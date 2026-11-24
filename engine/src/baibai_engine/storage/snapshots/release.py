"""Immutable market release creation and mutable-pointer wire contract."""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from baibai_engine.storage.snapshots.contracts import LakeDataset
from baibai_engine.storage.snapshots.immutable import ImmutableInstallError, install_immutable_bytes
from baibai_engine.storage.snapshots.keys import (
    release_manifest_key,
    validate_identifier,
    validate_sha256,
)
from baibai_engine.storage.snapshots.models import (
    DatasetManifest,
    ReleaseDataset,
    ReleaseManifest,
    ReleasePolicy,
    ReleaseProfile,
    canonical_lake_model_bytes,
    load_lake_model_json,
    validate_release_policy,
)

type MachineRole = Literal["market", "runs"]

_MACHINE_MEMBER_KEY = re.compile(r"machine/payloads/([0-9a-f]{32})/(market|runs)\.sqlite")


def machine_member_key(publication_id: str, role: MachineRole) -> str:
    key = f"machine/payloads/{publication_id}/{role}.sqlite"
    if _MACHINE_MEMBER_KEY.fullmatch(key) is None:
        raise ValueError("invalid machine member key")
    return key


class MachineMember(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    key: str
    etag: str
    size: int = Field(gt=0)

    @field_validator("etag")
    @classmethod
    def validate_etag(cls, value: str) -> str:
        # S3 ETags are opaque. The stored form is unquoted.
        if re.fullmatch(r"[\x21\x23-\x7e]{1,128}", value) is None:
            raise ValueError("invalid machine member ETag")
        return value


class MachineMembers(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    market: MachineMember
    runs: MachineMember

    @model_validator(mode="after")
    def validate_pair(self) -> MachineMembers:
        market = _MACHINE_MEMBER_KEY.fullmatch(self.market.key)
        runs = _MACHINE_MEMBER_KEY.fullmatch(self.runs.key)
        if (
            market is None
            or runs is None
            or market.group(2) != "market"
            or runs.group(2) != "runs"
            or market.group(1) != runs.group(1)
        ):
            raise ValueError("machine members must name one fresh key pair")
        return self

    @property
    def publication_id(self) -> str:
        # Both keys were validated as members of this UUID.
        return self.market.key.split("/")[2]

    def member(self, role: MachineRole) -> MachineMember:
        return self.market if role == "market" else self.runs


class LakeReleasePointer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    pointer_version: Literal[1, 2] = 1
    release_id: str
    manifest_key: str
    manifest_sha256: str
    machine: MachineMembers | None = Field(default=None, exclude_if=lambda value: value is None)
    retired_publication_id: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{32}$", exclude_if=lambda value: value is None
    )

    @field_validator("release_id")
    @classmethod
    def validate_release_id(cls, value: str) -> str:
        return validate_identifier(value, label="release_id")

    @model_validator(mode="after")
    def validate_identity(self) -> LakeReleasePointer:
        if self.manifest_key != release_manifest_key(release_id=self.release_id):
            raise ValueError("market pointer key does not match release_id")
        if (self.pointer_version == 2) != (self.machine is not None):
            raise ValueError("machine pointers require both machine members")
        if self.machine is not None:
            validate_sha256(self.manifest_sha256)
        if self.retired_publication_id is not None and (
            self.machine is None or self.retired_publication_id == self.machine.publication_id
        ):
            raise ValueError("machine retirement must name a different pair")
        return self


def create_release(
    *,
    catalog: Mapping[str, LakeDataset],
    policy: ReleasePolicy,
    dataset_manifest_paths: list[Path],
    mirror_root: Path,
    release_id: str | None = None,
    created_at: datetime | None = None,
    profile: ReleaseProfile = "production",
    published_coverage_start: Mapping[str, date] | None = None,
) -> tuple[Path, ReleaseManifest]:
    if not dataset_manifest_paths:
        raise ValueError("at least one dataset manifest is required")
    root = mirror_root.resolve()
    resolved_paths = [_contained_path(root, path) for path in dataset_manifest_paths]
    manifest_payloads = [path.read_bytes() for path in resolved_paths]
    manifests = [load_lake_model_json(payload, DatasetManifest) for payload in manifest_payloads]
    datasets = {
        item.dataset: ReleaseDataset(
            build_id=item.build_id,
            layer=item.layer,
            contract_version=item.contract_version,
            manifest_sha256=hashlib.sha256(payload).hexdigest(),
            data_as_of=item.data_as_of,
            coverage_status=item.coverage_status,
            totals=item.totals,
        )
        for item, payload in zip(manifests, manifest_payloads, strict=True)
    }
    if len(datasets) != len(manifests):
        raise ValueError("market release contains duplicate datasets")
    now = (created_at or datetime.now(UTC)).astimezone(UTC)
    inventory = hashlib.sha256(
        b"".join(payload for _, payload in sorted(zip(datasets, manifest_payloads, strict=True)))
    ).hexdigest()
    actual_release_id = release_id or (
        f"{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}-{inventory[:12]}"
    )
    validate_identifier(actual_release_id, label="release_id")
    release = ReleaseManifest(
        manifest_version=2,
        release_id=actual_release_id,
        profile=profile,
        created_at=now,
        data_as_of=min(item.data_as_of for item in manifests),
        datasets=datasets,
    )
    validate_release_policy(
        release,
        {item.dataset: item for item in manifests},
        published_coverage_start=published_coverage_start,
        policy=policy,
        catalog=catalog,
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
