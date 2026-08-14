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
    calibration_bundle_manifest_key,
    canonical_object_key,
    dataset_manifest_key,
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
CohortStatus = Literal["complete", "empty", "partial", "not_computed"]
ReleaseProfile = Literal["pilot", "production"]
MAX_LAKE_JSON_BYTES = 16 * 1024 * 1024
_PILOT_YEAR_MONTH_DATASETS = frozenset({"jquants.daily_bars", "jquants.short_sale_reports"})
CALIBRATION_DATASETS = frozenset(
    {"calibration.panel", "calibration.panel_diagnostics", "calibration.forward"}
)


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
        if not key.startswith("lake/build-inputs/sqlite/market/") or not key.endswith(".sqlite"):
            raise ValueError("sqlite_snapshot must reference a local SQLite build input")
        return key

    @model_validator(mode="after")
    def validate_identity(self) -> SQLiteSnapshotSourceRef:
        path = PurePosixPath(self.key)
        expected_source_id = f"market-v{self.schema_version}-{self.sha256[:24]}"
        if (
            self.source_id != expected_source_id
            or path.parent.name != f"schema=v{self.schema_version}"
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


class CalibrationInputSourceRef(_SourceRefBase):
    kind: Literal["calibration_input"]
    input_type: Literal["legacy_csv_archive", "sqlite_snapshot", "local_operation"]
    manifest_version: Literal[1]

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        key = validate_lake_object_key(value)
        if not key.startswith("lake/manifests/calibration-inputs/") or not key.endswith(".json"):
            raise ValueError("calibration_input must reference its input manifest")
        return key

    @model_validator(mode="after")
    def validate_identity(self) -> CalibrationInputSourceRef:
        if self.key != f"lake/manifests/calibration-inputs/{self.source_id}.json":
            raise ValueError("calibration_input key does not match source_id")
        return self


type SourceRef = Annotated[
    RawIngestSourceRef | SQLiteSnapshotSourceRef | L1ReleaseSourceRef | CalibrationInputSourceRef,
    Field(discriminator="kind"),
]


class CalibrationInputFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    key: str
    sha256: str
    bytes: int = Field(gt=0)

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        key = validate_lake_object_key(value)
        if not key.startswith("lake/l2/calibration-legacy/"):
            raise ValueError("calibration input file must use the legacy archive prefix")
        return key

    @field_validator("sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return validate_sha256(value)


class CalibrationInputManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    manifest_version: Literal[1]
    input_id: str
    input_type: Literal["legacy_csv_archive", "sqlite_snapshot", "local_operation"]
    files: Mapping[str, CalibrationInputFile] = Field(min_length=1)

    @field_validator("input_id")
    @classmethod
    def validate_input_id(cls, value: str) -> str:
        return validate_identifier(value, label="input_id")

    @field_validator("files")
    @classmethod
    def freeze_files(
        cls, values: Mapping[str, CalibrationInputFile]
    ) -> Mapping[str, CalibrationInputFile]:
        for name in values:
            if PurePosixPath(name).name != name or name in {".", ".."}:
                raise ValueError("calibration input file names must be flat safe names")
        return MappingProxyType(dict(values))

    @field_serializer("files")
    def serialize_files(
        self, values: Mapping[str, CalibrationInputFile]
    ) -> dict[str, CalibrationInputFile]:
        return dict(values)


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
    source_state_sha256: str

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

    @field_validator("source_state_sha256")
    @classmethod
    def validate_source_state_sha256(cls, value: str) -> str:
        return validate_sha256(value)


class ManifestTotals(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    objects: int = Field(ge=0)
    bytes: int = Field(ge=0)
    rows: int = Field(ge=0)


class CohortInventoryEntry(BaseModel):
    """Manifest-only proof that one analytical cohort was evaluated."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: CohortStatus
    rows: int = Field(ge=0)
    sources: tuple[SourceRef, ...] = Field(min_length=1)
    input_cutoff: date

    @field_validator("sources")
    @classmethod
    def validate_sources(cls, values: tuple[SourceRef, ...]) -> tuple[SourceRef, ...]:
        identities = {(item.kind, item.source_id, item.key, item.sha256) for item in values}
        if len(identities) != len(values):
            raise ValueError("cohort sources cannot contain duplicates")
        if any(item.kind == "raw_ingest" for item in values):
            raise ValueError("analytical cohorts require fixed-generation sources")
        return values

    @model_validator(mode="after")
    def validate_status(self) -> CohortInventoryEntry:
        if self.status == "complete" and self.rows == 0:
            raise ValueError("complete cohort must contain rows")
        if self.status in {"empty", "not_computed"} and self.rows != 0:
            raise ValueError(f"{self.status} cohort cannot contain rows")
        if any(
            source.kind == "sqlite_snapshot" and self.input_cutoff > source.captured_at.date()
            for source in self.sources
        ):
            raise ValueError("cohort input cutoff cannot follow SQLite snapshot capture")
        return self


class CalibrationDatasetRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    dataset: str
    build_id: str
    manifest_key: str
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rows: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_identity(self) -> CalibrationDatasetRef:
        if self.dataset not in CALIBRATION_DATASETS:
            raise ValueError("bundle references an unsupported calibration dataset")
        if self.manifest_key != dataset_manifest_key(dataset=self.dataset, build_id=self.build_id):
            raise ValueError("bundle dataset key does not match its identity")
        return self


class CalibrationCohortInventory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    panel: CohortInventoryEntry
    diagnostics: CohortInventoryEntry
    forward: CohortInventoryEntry

    @model_validator(mode="after")
    def validate_composition(self) -> CalibrationCohortInventory:
        if self.diagnostics.status not in {"complete", "partial"}:
            raise ValueError("computed panel cohort requires diagnostics")
        if self.diagnostics.status == "complete" and self.diagnostics.rows != 1:
            raise ValueError("complete diagnostics cohort must contain exactly one row")
        if self.panel.status == "not_computed":
            raise ValueError("a bundle cannot publish an uncomputed panel cohort")
        if (
            self.panel.sources != self.diagnostics.sources
            or self.panel.input_cutoff != self.diagnostics.input_cutoff
        ):
            raise ValueError("panel and diagnostics must use the same cohort input")
        return self


class CalibrationBundleManifest(BaseModel):
    """One externally visible calibration generation across all three datasets."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    manifest_version: Literal[1] = 1
    bundle_id: str
    created_at: datetime
    producer_git_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    cache_schema_version: str
    datasets: Mapping[str, CalibrationDatasetRef]
    cohorts: Mapping[str, CalibrationCohortInventory] = Field(min_length=1)

    @field_validator("bundle_id")
    @classmethod
    def validate_bundle_id(cls, value: str) -> str:
        return validate_identifier(value, label="bundle_id")

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("created_at must be UTC")
        return value

    @field_validator("datasets")
    @classmethod
    def freeze_datasets(
        cls, values: Mapping[str, CalibrationDatasetRef]
    ) -> Mapping[str, CalibrationDatasetRef]:
        if set(values) != CALIBRATION_DATASETS:
            raise ValueError("bundle must reference the complete calibration dataset set")
        if any(name != item.dataset for name, item in values.items()):
            raise ValueError("bundle dataset mapping keys must match their references")
        return MappingProxyType(dict(values))

    @field_serializer("datasets")
    def serialize_datasets(
        self, values: Mapping[str, CalibrationDatasetRef]
    ) -> dict[str, CalibrationDatasetRef]:
        return dict(values)

    @field_validator("cohorts")
    @classmethod
    def freeze_cohorts(
        cls, values: Mapping[str, CalibrationCohortInventory]
    ) -> Mapping[str, CalibrationCohortInventory]:
        for asof, cohort in values.items():
            if date.fromisoformat(asof).isoformat() != asof:
                raise ValueError("bundle cohort keys must be canonical ISO dates")
            cohort_asof = date.fromisoformat(asof)
            if cohort.panel.input_cutoff != cohort_asof:
                raise ValueError("panel input cutoff must equal its cohort as-of")
            if cohort.forward.input_cutoff < cohort_asof:
                raise ValueError("forward input cutoff cannot precede its cohort as-of")
        return MappingProxyType(dict(values))

    @field_serializer("cohorts")
    def serialize_cohorts(
        self, values: Mapping[str, CalibrationCohortInventory]
    ) -> dict[str, CalibrationCohortInventory]:
        return dict(values)


class CalibrationBundleRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    bundle_id: str
    manifest_key: str
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_identity(self) -> CalibrationBundleRef:
        if self.manifest_key != calibration_bundle_manifest_key(bundle_id=self.bundle_id):
            raise ValueError("bundle ref key does not match its identity")
        return self


class CalibrationBundlePointer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    pointer_version: Literal[1] = 1
    current: CalibrationBundleRef
    previous: CalibrationBundleRef | None = None


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
    coverage_start: date
    data_as_of: date
    population_count: int = Field(ge=0)
    coverage_status: CoverageStatus
    partition_by: tuple[str, ...] = Field(min_length=1)
    cohort_inventory: Mapping[str, CohortInventoryEntry] = Field(default_factory=dict)
    # An analytical build can legitimately publish nothing — a forward cohort where
    # no observation has resolved yet is a real state, and representing it as an
    # absent build would make "not computed" indistinguishable from "computed and
    # empty". A canonical L1 build with no partition is not a usable release input.
    partitions: tuple[PartitionManifest, ...] = ()
    totals: ManifestTotals

    @field_validator("dataset")
    @classmethod
    def validate_dataset(cls, value: str) -> str:
        return validate_dataset_name(value)

    @field_validator("build_id")
    @classmethod
    def validate_build_id(cls, value: str) -> str:
        return validate_identifier(value, label="build_id")

    @field_validator("producer_git_commit")
    @classmethod
    def validate_producer_commit(cls, value: str) -> str:
        if value == "0" * 40:
            raise ValueError("producer_git_commit cannot be an unknown zero identity")
        return value

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

    @field_validator("cohort_inventory")
    @classmethod
    def freeze_cohort_inventory(
        cls, values: Mapping[str, CohortInventoryEntry]
    ) -> Mapping[str, CohortInventoryEntry]:
        for asof in values:
            if date.fromisoformat(asof).isoformat() != asof:
                raise ValueError("cohort inventory keys must be canonical ISO dates")
        return MappingProxyType(dict(values))

    @field_serializer("cohort_inventory")
    def serialize_cohort_inventory(
        self, values: Mapping[str, CohortInventoryEntry]
    ) -> dict[str, CohortInventoryEntry]:
        return dict(values)

    @model_validator(mode="after")
    def validate_semantics(self) -> DatasetManifest:
        if self.coverage_start > self.data_as_of:
            raise ValueError("coverage_start cannot be after data_as_of")
        if self.population_count > self.totals.rows:
            raise ValueError("population_count cannot exceed total rows")
        if self.layer == "l1_canonical":
            if self.population_count == 0:
                raise ValueError("l1_canonical population_count must be positive")
            if self.cohort_inventory:
                raise ValueError("l1_canonical cannot contain analytical cohort inventory")
            if self.sources:
                raise ValueError("l1_canonical keeps lineage on each partition")
            if not self.partitions:
                raise ValueError("l1_canonical requires at least one partition")
            if any(not partition.sources for partition in self.partitions):
                raise ValueError("each L1 partition requires source lineage")
            if any(
                source.kind not in {"raw_ingest", "sqlite_snapshot"}
                for partition in self.partitions
                for source in partition.sources
            ):
                raise ValueError("L1 partitions accept Raw ingest or SQLite snapshot sources only")
        else:
            if self.sources:
                raise ValueError("L2 lineage belongs to each analytical cohort")
            if not self.cohort_inventory:
                raise ValueError("l2_analytical requires computed cohort inventory")
            if any(not item.sources for item in self.cohort_inventory.values()):
                raise ValueError("each L2 cohort requires a fixed input generation source")
            if any(partition.sources for partition in self.partitions):
                raise ValueError("L2 lineage belongs to each cohort, not each partition")
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
        if self.layer == "l2_analytical":
            inventory_rows = sum(item.rows for item in self.cohort_inventory.values())
            if inventory_rows != self.totals.rows:
                raise ValueError("L2 cohort inventory rows must equal manifest totals")
        return self


class ReleaseDataset(BaseModel):
    """One dataset build a release pins, named by identity and closed by digest.

    ``manifest_sha256`` is what makes the release fix data rather than names. A
    dataset manifest key is derived from ``(dataset, build_id)``, so without the
    digest the same release could be made to resolve to different objects by
    republishing that key — and every downstream check would pass, because the
    object digests it compares against come from the replaced manifest.
    """

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
    coverage_start_on_or_before: date
    minimum_rows: int = Field(gt=0)
    minimum_population_count: int = Field(gt=0)

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
            coverage_start_on_or_before=date(2016, 8, 1),
            minimum_rows=9_630_029,
            minimum_population_count=5_098,
        ),
        ReleaseDatasetPolicy(
            dataset="jquants.short_sale_reports",
            required=True,
            accepted_contract_versions=(1,),
            coverage_start_on_or_before=date(2016, 8, 10),
            minimum_rows=1_341_528,
            minimum_population_count=3_907,
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
        if manifest.coverage_start > dataset_policy.coverage_start_on_or_before:
            raise ValueError("release dataset does not reach the profile history boundary")
        if manifest.totals.rows < dataset_policy.minimum_rows:
            raise ValueError("release dataset is below the profile row floor")
        if manifest.population_count < dataset_policy.minimum_population_count:
            raise ValueError("release dataset is below the profile population floor")
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
