"""Strict immutable manifest models for L1 and L2 market datasets."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from hashlib import sha256
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from .keys import (
    PartitionValue,
    canonical_object_key,
    raw_metadata_object_key,
    raw_object_key,
    release_manifest_key,
    validate_dataset_name,
    validate_identifier,
    validate_lake_object_key,
    validate_partition_layout,
    validate_sha256,
)

ManifestLayer = Literal["l1_canonical", "l2_analytical"]
CoverageStatus = Literal["complete", "partial"]
ReleaseProfile = Literal["pilot", "production"]
MAX_LAKE_JSON_BYTES = 16 * 1024 * 1024
_PILOT_YEAR_MONTH_DATASETS = frozenset({"jquants.daily_bars", "jquants.short_sale_reports"})


class RawArchiveMetadata(BaseModel):
    """Strict sidecar that binds provider Raw bytes to their retrieval identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    metadata_version: Literal[1]
    provider: str
    dataset: str
    ingest_id: str
    retrieved_at: datetime
    retention_class: Literal["preserve", "buffer"]
    suffix: Literal[".json.gz", ".csv.gz", ".zip"]
    endpoint: str | None = None
    request_start: date | None = None
    request_end: date | None = None
    object_key: str
    content_sha256: str
    bytes: int = Field(gt=0)

    @field_validator("provider", "dataset")
    @classmethod
    def validate_dataset_segment(cls, value: str) -> str:
        return validate_dataset_name(value)

    @field_validator("ingest_id")
    @classmethod
    def validate_ingest_id(cls, value: str) -> str:
        return validate_identifier(value, label="ingest_id")

    @field_validator("retrieved_at")
    @classmethod
    def validate_retrieved_at(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("retrieved_at must be UTC")
        return value

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("endpoint must not contain credentials, query, or fragment")
        return value

    @field_validator("content_sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return validate_sha256(value)

    @model_validator(mode="after")
    def validate_identity(self) -> RawArchiveMetadata:
        expected = raw_object_key(
            provider=self.provider,
            dataset=self.dataset,
            ingest_date=self.retrieved_at.date(),
            ingest_id=self.ingest_id,
            suffix=self.suffix,
        )
        if self.object_key != expected:
            raise ValueError("Raw object_key does not match metadata identity")
        if (
            self.request_start is not None
            and self.request_end is not None
            and self.request_start > self.request_end
        ):
            raise ValueError("request_start must not be after request_end")
        return self


class _SourceRefBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_id: str
    key: str
    sha256: str

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        return validate_identifier(value, label="source_id")

    @field_validator("sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return validate_sha256(value)


class RawIngestSourceRef(_SourceRefBase):
    kind: Literal["raw_ingest"]
    provider: str
    dataset: str
    request_start: date
    request_end: date
    metadata_version: int = Field(ge=1)
    metadata_key: str
    metadata_sha256: str

    @field_validator("provider", "dataset")
    @classmethod
    def validate_source_segment(cls, value: str) -> str:
        return validate_dataset_name(value)

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        key = validate_lake_object_key(value)
        if key.startswith("lake/l1/raw/legacy_sqlite/") or not (
            key.startswith("lake/l1/raw/") and key.endswith((".csv.gz", ".json.gz", ".zip"))
        ):
            raise ValueError("raw_ingest must reference an L1 Raw object")
        return key

    @field_validator("metadata_key")
    @classmethod
    def validate_metadata_key(cls, value: str) -> str:
        return validate_lake_object_key(value)

    @field_validator("metadata_sha256")
    @classmethod
    def validate_metadata_digest(cls, value: str) -> str:
        return validate_sha256(value)

    @model_validator(mode="after")
    def validate_identity(self) -> RawIngestSourceRef:
        if self.request_start > self.request_end:
            raise ValueError("raw_ingest request_start must not be after request_end")
        if PurePosixPath(self.key).name not in {
            f"{self.source_id}.csv.gz",
            f"{self.source_id}.json.gz",
            f"{self.source_id}.zip",
        }:
            raise ValueError("raw_ingest key does not match source_id")
        if self.metadata_key != raw_metadata_object_key(raw_key=self.key):
            raise ValueError("raw_ingest metadata key does not match Raw object key")
        return self


class SQLiteSnapshotSourceRef(_SourceRefBase):
    kind: Literal["sqlite_snapshot"]
    role: Literal["local_build_input"]
    schema_version: int = Field(ge=1)
    captured_at: datetime

    @field_validator("captured_at")
    @classmethod
    def validate_captured_at(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("captured_at must be UTC")
        return value

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        key = validate_lake_object_key(value)
        if not key.startswith("lake/l1/raw/legacy_sqlite/") or not key.endswith(".sqlite"):
            raise ValueError("sqlite_snapshot must reference a legacy SQLite snapshot object")
        return key

    @model_validator(mode="after")
    def validate_identity(self) -> SQLiteSnapshotSourceRef:
        path = PurePosixPath(self.key)
        if (
            path.parent.name != self.source_id
            or path.parent.parent.name != f"schema=v{self.schema_version}"
            or path.name != f"snapshot-{self.sha256}.sqlite"
        ):
            raise ValueError("sqlite_snapshot key does not match source identity")
        return self


class L1ReleaseSourceRef(_SourceRefBase):
    kind: Literal["l1_release"]
    manifest_version: int = Field(ge=1)

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        key = validate_lake_object_key(value)
        if not key.startswith("lake/manifests/releases/l1/") or not key.endswith(".json"):
            raise ValueError("l1_release must reference an L1 release manifest")
        return key

    @model_validator(mode="after")
    def validate_identity(self) -> L1ReleaseSourceRef:
        if self.key != release_manifest_key(release_id=self.source_id):
            raise ValueError("l1_release key does not match source_id")
        return self


type SourceRef = Annotated[
    RawIngestSourceRef | SQLiteSnapshotSourceRef | L1ReleaseSourceRef,
    Field(discriminator="kind"),
]


class LakeObject(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    key: str
    sha256: str
    bytes: int = Field(gt=0)
    rows: int = Field(ge=0)
    min_key: tuple[str, ...] = Field(min_length=1)
    max_key: tuple[str, ...] = Field(min_length=1)

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        return validate_lake_object_key(value)

    @field_validator("sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return validate_sha256(value)

    @model_validator(mode="after")
    def validate_key_range(self) -> LakeObject:
        if len(self.min_key) != len(self.max_key):
            raise ValueError("min_key and max_key must have the same arity")
        if self.min_key > self.max_key:
            raise ValueError("min_key cannot sort after max_key")
        return self


class PartitionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    values: Mapping[str, PartitionValue] = Field(min_length=1)
    objects: tuple[LakeObject, ...] = Field(min_length=1)
    sources: tuple[SourceRef, ...] = ()

    @field_validator("values")
    @classmethod
    def freeze_values(cls, values: Mapping[str, PartitionValue]) -> Mapping[str, PartitionValue]:
        return MappingProxyType(dict(values))

    @field_serializer("values")
    def serialize_values(self, values: Mapping[str, PartitionValue]) -> dict[str, PartitionValue]:
        return dict(values)

    @field_validator("sources")
    @classmethod
    def validate_sources(cls, values: tuple[SourceRef, ...]) -> tuple[SourceRef, ...]:
        identities = {(item.kind, item.source_id, item.key, item.sha256) for item in values}
        if len(identities) != len(values):
            raise ValueError("partition sources cannot contain duplicates")
        return values


class ManifestTotals(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    objects: int = Field(ge=0)
    bytes: int = Field(ge=0)
    rows: int = Field(ge=0)


class DatasetManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    manifest_version: Literal[1]
    dataset: str
    layer: ManifestLayer
    contract_version: int = Field(ge=1)
    build_id: str
    sources: tuple[SourceRef, ...]
    producer_git_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    transform_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    created_at: datetime
    data_as_of: date
    coverage_status: CoverageStatus
    partition_by: tuple[str, ...] = Field(min_length=1)
    partitions: tuple[PartitionManifest, ...] = Field(min_length=1)
    totals: ManifestTotals

    @field_validator("dataset")
    @classmethod
    def validate_dataset(cls, value: str) -> str:
        return validate_dataset_name(value)

    @field_validator("build_id")
    @classmethod
    def validate_build_id(cls, value: str) -> str:
        return validate_identifier(value, label="build_id")

    @field_validator("sources")
    @classmethod
    def validate_sources(cls, values: tuple[SourceRef, ...]) -> tuple[SourceRef, ...]:
        identities = {(item.kind, item.source_id, item.key, item.sha256) for item in values}
        if len(identities) != len(values):
            raise ValueError("dataset sources cannot contain duplicates")
        return values

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("created_at must be UTC")
        return value

    @field_validator("partition_by")
    @classmethod
    def validate_partition_by(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return validate_partition_layout(value)

    @model_validator(mode="after")
    def validate_semantics(self) -> DatasetManifest:
        if self.layer == "l1_canonical":
            if self.sources:
                raise ValueError("l1_canonical keeps lineage on each partition")
            if any(not partition.sources for partition in self.partitions):
                raise ValueError("each L1 partition requires source lineage")
            if any(
                source.kind not in {"raw_ingest", "sqlite_snapshot"}
                for partition in self.partitions
                for source in partition.sources
            ):
                raise ValueError("L1 partitions accept Raw ingest or SQLite snapshot sources only")
        else:
            if not self.sources or any(source.kind == "raw_ingest" for source in self.sources):
                raise ValueError("l2_analytical requires a fixed input generation source")
            if any(partition.sources for partition in self.partitions):
                raise ValueError("L2 lineage belongs to the dataset build, not each partition")
        if (
            self.dataset in _PILOT_YEAR_MONTH_DATASETS
            and self.contract_version == 1
            and self.partition_by != ("year", "month")
        ):
            raise ValueError("pilot time-series contract v1 requires year/month partitioning")

        partition_identities: set[tuple[tuple[str, PartitionValue], ...]] = set()
        object_keys: set[str] = set()
        object_count = 0
        byte_count = 0
        row_count = 0
        for partition in self.partitions:
            identity = tuple(sorted(partition.values.items()))
            if identity in partition_identities:
                raise ValueError("partition values cannot be repeated")
            partition_identities.add(identity)
            for lake_object in partition.objects:
                expected_key = canonical_object_key(
                    layer=self.layer,
                    dataset=self.dataset,
                    contract_version=self.contract_version,
                    partition_values=partition.values,
                    content_sha256=lake_object.sha256,
                    partition_by=self.partition_by,
                )
                if lake_object.key != expected_key:
                    raise ValueError("object key does not match manifest identity")
                if lake_object.key in object_keys:
                    raise ValueError("object keys cannot be repeated")
                object_keys.add(lake_object.key)
                object_count += 1
                byte_count += lake_object.bytes
                row_count += lake_object.rows
        expected_totals = (object_count, byte_count, row_count)
        actual_totals = (self.totals.objects, self.totals.bytes, self.totals.rows)
        if actual_totals != expected_totals:
            raise ValueError("totals must equal the manifest object inventory")
        return self


class ReleaseDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    build_id: str
    contract_version: int = Field(ge=1)
    manifest_sha256: str
    data_as_of: date
    coverage_status: CoverageStatus
    totals: ManifestTotals

    @field_validator("build_id")
    @classmethod
    def validate_build_id(cls, value: str) -> str:
        return validate_identifier(value, label="build_id")

    @field_validator("manifest_sha256")
    @classmethod
    def validate_manifest_digest(cls, value: str) -> str:
        return validate_sha256(value)


class ReleaseManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    manifest_version: Literal[1]
    release_id: str
    profile: ReleaseProfile
    created_at: datetime
    data_as_of: date
    datasets: Mapping[str, ReleaseDataset] = Field(min_length=1)

    @field_validator("release_id")
    @classmethod
    def validate_release_id(cls, value: str) -> str:
        return validate_identifier(value, label="release_id")

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("created_at must be UTC")
        return value

    @field_validator("datasets")
    @classmethod
    def validate_datasets(
        cls, values: Mapping[str, ReleaseDataset]
    ) -> Mapping[str, ReleaseDataset]:
        for dataset in values:
            validate_dataset_name(dataset)
        return MappingProxyType(dict(values))

    @field_serializer("datasets")
    def serialize_datasets(self, values: Mapping[str, ReleaseDataset]) -> dict[str, ReleaseDataset]:
        return dict(values)

    @model_validator(mode="after")
    def validate_inventory(self) -> ReleaseManifest:
        if self.data_as_of != min(item.data_as_of for item in self.datasets.values()):
            raise ValueError("release data_as_of must equal the oldest dataset watermark")
        return self


class ReleaseDatasetPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    dataset: str
    required: bool
    accepted_contract_versions: tuple[int, ...] = Field(min_length=1)

    @field_validator("dataset")
    @classmethod
    def validate_dataset(cls, value: str) -> str:
        return validate_dataset_name(value)

    @field_validator("accepted_contract_versions")
    @classmethod
    def validate_versions(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if any(isinstance(value, bool) or value < 1 for value in values):
            raise ValueError("accepted contract versions must be positive integers")
        if len(values) != len(set(values)):
            raise ValueError("accepted contract versions cannot contain duplicates")
        return values


class ReleasePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    policy_version: Literal[1]
    profile: ReleaseProfile
    datasets: tuple[ReleaseDatasetPolicy, ...] = Field(min_length=1)
    max_dataset_age_days: int = Field(ge=0)
    max_dataset_skew_days: int = Field(ge=0)
    require_complete_coverage: bool
    max_manifest_bytes: int = Field(gt=0)
    max_objects: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_dataset_inventory(self) -> ReleasePolicy:
        names = [item.dataset for item in self.datasets]
        if len(names) != len(set(names)):
            raise ValueError("release policy datasets cannot contain duplicates")
        if not any(item.required for item in self.datasets):
            raise ValueError("release policy requires at least one dataset")
        return self


PILOT_RELEASE_POLICY = ReleasePolicy(
    policy_version=1,
    profile="pilot",
    datasets=(
        ReleaseDatasetPolicy(
            dataset="jquants.daily_bars",
            required=True,
            accepted_contract_versions=(1,),
        ),
        ReleaseDatasetPolicy(
            dataset="jquants.short_sale_reports",
            required=True,
            accepted_contract_versions=(1,),
        ),
    ),
    max_dataset_age_days=31,
    max_dataset_skew_days=31,
    require_complete_coverage=True,
    max_manifest_bytes=16 * 1024 * 1024,
    max_objects=10_000,
)


def release_policy_for_profile(profile: ReleaseProfile) -> ReleasePolicy:
    """Return the registered release policy; unconfigured authority profiles fail closed."""
    if profile == "pilot":
        return PILOT_RELEASE_POLICY
    raise ValueError("production release policy is not configured")


type Manifest = DatasetManifest | ReleaseManifest


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _load_json_object(raw: str | bytes) -> dict[str, object]:
    wire_bytes = raw if isinstance(raw, bytes) else raw.encode()
    if len(wire_bytes) > MAX_LAKE_JSON_BYTES:
        raise ValueError("lake JSON payload exceeds the wire size limit")
    try:
        payload = json.loads(wire_bytes, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("invalid JSON payload") from None
    if not isinstance(payload, dict):
        raise ValueError("JSON root must be an object")
    return payload


def _validate_payload[LakeModelT: BaseModel](
    payload: dict[str, object], model: type[LakeModelT]
) -> LakeModelT:
    try:
        canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return model.model_validate_json(canonical)
    except ValidationError as exc:
        error = exc.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        )[0]
        location = ".".join(str(item) for item in error["loc"]) or "root"
        raise ValueError(f"invalid {model.__name__}: {error['type']} at {location}") from None


def load_lake_model_json[LakeModelT: BaseModel](
    raw: str | bytes, model: type[LakeModelT]
) -> LakeModelT:
    """Parse one lake wire model with duplicate-key rejection and redacted errors."""
    return _validate_payload(_load_json_object(raw), model)


def load_manifest_json(raw: str | bytes) -> Manifest:
    """Parse one strict manifest without accepting ambiguous or duplicate fields."""
    payload = _load_json_object(raw)
    is_dataset = "dataset" in payload
    is_release = "release_id" in payload
    if is_dataset == is_release:
        raise ValueError("manifest must identify exactly one dataset build or L1 release")
    if is_dataset:
        return _validate_payload(payload, DatasetManifest)
    return _validate_payload(payload, ReleaseManifest)


def canonical_lake_model_bytes(model: BaseModel) -> bytes:
    """Serialize one validated lake model for immutable storage and digesting."""
    return (
        json.dumps(
            model.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


def validate_release_policy(
    release: ReleaseManifest,
    manifests: Mapping[str, DatasetManifest],
    *,
    evaluated_at: datetime,
) -> None:
    """Require a release to be complete and fresh for its declared profile."""
    if evaluated_at.utcoffset() != timedelta(0):
        raise ValueError("release evaluation time must be UTC")
    if release.created_at > evaluated_at:
        raise ValueError("release creation time cannot be after its evaluation time")
    policy = release_policy_for_profile(release.profile)
    policy_by_dataset = {item.dataset: item for item in policy.datasets}
    unknown = set(release.datasets) - set(policy_by_dataset)
    if unknown:
        raise ValueError("release contains a dataset outside its profile")
    required = {item.dataset for item in policy.datasets if item.required}
    if not required.issubset(release.datasets):
        raise ValueError("release is missing a required dataset")
    if set(manifests) != set(release.datasets):
        raise ValueError("release validation requires every referenced dataset manifest")

    total_manifest_bytes = len(canonical_lake_model_bytes(release))
    total_objects = 0
    watermarks: list[date] = []
    for dataset, release_dataset in release.datasets.items():
        manifest = manifests[dataset]
        dataset_policy = policy_by_dataset[dataset]
        if manifest.layer != "l1_canonical" or manifest.dataset != dataset:
            raise ValueError("release accepts matching L1 dataset manifests only")
        if manifest.created_at > release.created_at:
            raise ValueError("release cannot predate a referenced dataset manifest")
        if manifest.contract_version not in dataset_policy.accepted_contract_versions:
            raise ValueError("release dataset contract is not accepted by its profile")
        manifest_bytes = canonical_lake_model_bytes(manifest)
        if release_dataset.manifest_sha256 != sha256(manifest_bytes).hexdigest():
            raise ValueError("release dataset manifest digest does not match")
        if (
            release_dataset.build_id != manifest.build_id
            or release_dataset.contract_version != manifest.contract_version
            or release_dataset.data_as_of != manifest.data_as_of
            or release_dataset.coverage_status != manifest.coverage_status
            or release_dataset.totals != manifest.totals
        ):
            raise ValueError("release dataset inventory does not match its manifest")
        if policy.require_complete_coverage and manifest.coverage_status != "complete":
            raise ValueError("release profile requires complete dataset coverage")
        age = evaluated_at.date() - manifest.data_as_of
        if age.days < 0 or age.days > policy.max_dataset_age_days:
            raise ValueError("release dataset is outside the profile freshness window")
        total_manifest_bytes += len(manifest_bytes)
        total_objects += manifest.totals.objects
        watermarks.append(manifest.data_as_of)

    if (max(watermarks) - min(watermarks)).days > policy.max_dataset_skew_days:
        raise ValueError("release dataset watermarks exceed the profile skew limit")
    if total_manifest_bytes > policy.max_manifest_bytes:
        raise ValueError("release manifest graph exceeds the profile byte budget")
    if total_objects > policy.max_objects:
        raise ValueError("release object inventory exceeds the profile budget")
