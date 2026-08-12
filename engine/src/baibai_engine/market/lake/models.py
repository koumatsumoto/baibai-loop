"""Strict immutable manifest models for L1 and L2 market datasets."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .keys import (
    PartitionValue,
    canonical_object_key,
    validate_dataset_name,
    validate_identifier,
    validate_lake_object_key,
    validate_partition_layout,
    validate_sha256,
)

ManifestLayer = Literal["l1_canonical", "l2_analytical"]
_PILOT_YEAR_MONTH_DATASETS = frozenset({"jquants.daily_bars", "jquants.short_sale_reports"})


class LakeObject(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    key: str
    etag: str = Field(min_length=1)
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

    values: dict[str, PartitionValue] = Field(min_length=1)
    objects: tuple[LakeObject, ...] = Field(min_length=1)


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
    source_ingest_ids: tuple[str, ...]
    source_release_ids: tuple[str, ...]
    producer_git_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    transform_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    created_at: datetime
    data_as_of: date
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

    @field_validator("source_ingest_ids")
    @classmethod
    def validate_ingest_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            validate_identifier(value, label="source_ingest_id")
        if len(values) != len(set(values)):
            raise ValueError("source_ingest_ids cannot contain duplicates")
        return values

    @field_validator("source_release_ids")
    @classmethod
    def validate_release_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            validate_identifier(value, label="source_release_id")
        if len(values) != len(set(values)):
            raise ValueError("source_release_ids cannot contain duplicates")
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
            if not self.source_ingest_ids or self.source_release_ids:
                raise ValueError(
                    "l1_canonical requires source_ingest_ids and forbids source_release_ids"
                )
        elif not self.source_release_ids or self.source_ingest_ids:
            raise ValueError(
                "l2_analytical requires source_release_ids and forbids source_ingest_ids"
            )
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

    @field_validator("build_id")
    @classmethod
    def validate_build_id(cls, value: str) -> str:
        return validate_identifier(value, label="build_id")


class ReleaseManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    manifest_version: Literal[1]
    release_id: str
    created_at: datetime
    data_as_of: date
    datasets: dict[str, ReleaseDataset] = Field(min_length=1)

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
    def validate_datasets(cls, values: dict[str, ReleaseDataset]) -> dict[str, ReleaseDataset]:
        for dataset in values:
            validate_dataset_name(dataset)
        return values


type Manifest = DatasetManifest | ReleaseManifest


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def load_manifest_json(raw: str) -> Manifest:
    """Parse one strict manifest without accepting ambiguous or duplicate fields."""
    payload = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(payload, dict):
        raise ValueError("manifest root must be an object")
    is_dataset = "dataset" in payload
    is_release = "release_id" in payload
    if is_dataset == is_release:
        raise ValueError("manifest must identify exactly one dataset build or L1 release")
    if is_dataset:
        return DatasetManifest.model_validate_json(raw)
    return ReleaseManifest.model_validate_json(raw)
