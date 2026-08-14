"""Deterministic, traversal-safe R2 object keys for the market lake."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date

LakeLayer = str
PartitionValue = str | int

_DATASET = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_KEY_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._=-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RAW_SUFFIXES = frozenset({".csv.gz", ".json.gz", ".zip"})
_PARTITION_ORDER = {"year": 0, "month": 1, "date": 2, "ingest_date": 3}


def _positive_version(contract_version: int) -> int:
    if isinstance(contract_version, bool) or not isinstance(contract_version, int):
        raise ValueError("contract_version must be an integer")
    if contract_version < 1:
        raise ValueError("contract_version must be positive")
    return contract_version


def validate_dataset_name(dataset: str) -> str:
    if not _DATASET.fullmatch(dataset):
        raise ValueError("dataset must be a lowercase path-safe identifier")
    return dataset


def validate_identifier(value: str, *, label: str) -> str:
    if not _IDENTIFIER.fullmatch(value) or value in {".", ".."}:
        raise ValueError(f"{label} must be a path-safe identifier")
    return value


def validate_sha256(value: str) -> str:
    if not _SHA256.fullmatch(value):
        raise ValueError("sha256 must contain 64 lowercase hexadecimal characters")
    return value


def _partition_value(key: str, value: PartitionValue) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"partition {key} must be a string or integer")
    if key == "year" and (not isinstance(value, int) or not 1900 <= value <= 9999):
        raise ValueError("partition year must be a four-digit integer")
    if key == "month" and (not isinstance(value, int) or not 1 <= value <= 12):
        raise ValueError("partition month must be an integer from 1 to 12")
    rendered = str(value)
    if not _IDENTIFIER.fullmatch(rendered) or rendered in {".", ".."}:
        raise ValueError(f"partition {key} must be path-safe")
    return rendered


def validate_partition_layout(partition_by: tuple[str, ...]) -> tuple[str, ...]:
    if not partition_by:
        raise ValueError("partition layout cannot be empty")
    if len(partition_by) != len(set(partition_by)):
        raise ValueError("partition layout cannot contain duplicates")
    for key in partition_by:
        if not _DATASET.fullmatch(key):
            raise ValueError("partition names must be lowercase path-safe identifiers")
    return partition_by


def partition_segments(
    values: Mapping[str, PartitionValue], *, partition_by: tuple[str, ...] | None = None
) -> tuple[str, ...]:
    if not values:
        raise ValueError("partition values cannot be empty")
    ordered: tuple[str, ...]
    if partition_by is None:
        validate_partition_layout(tuple(values))
        ordered = tuple(sorted(values, key=lambda key: (_PARTITION_ORDER.get(key, 100), key)))
    else:
        ordered = validate_partition_layout(partition_by)
        if set(values) != set(ordered):
            raise ValueError("partition values must match the ordered partition layout")
    return tuple(f"{key}={_partition_value(key, values[key])}" for key in ordered)


def validate_lake_object_key(key: str) -> str:
    if not key or key.startswith("/") or key.endswith("/") or "\\" in key:
        raise ValueError("lake object key must be a relative POSIX key")
    segments = key.split("/")
    if segments[0] != "lake":
        raise ValueError("lake object key must start with lake/")
    if any(
        not segment or segment in {".", ".."} or not _KEY_SEGMENT.fullmatch(segment)
        for segment in segments
    ):
        raise ValueError("lake object key contains an unsafe path segment")
    return key


def raw_object_key(
    *,
    provider: str,
    dataset: str,
    ingest_date: date,
    ingest_id: str,
    suffix: str,
) -> str:
    validate_dataset_name(provider)
    validate_dataset_name(dataset)
    validate_identifier(ingest_id, label="ingest_id")
    if suffix not in _RAW_SUFFIXES:
        raise ValueError("raw object suffix must be .json.gz, .csv.gz, or .zip")
    return validate_lake_object_key(
        f"lake/l1/raw/{provider}/{dataset}/ingest_date={ingest_date.isoformat()}/"
        f"{ingest_id}{suffix}"
    )


def raw_metadata_object_key(*, raw_key: str) -> str:
    """Return the sidecar key that binds Raw bytes to retrieval metadata."""
    key = validate_lake_object_key(raw_key)
    if key.startswith("lake/l1/raw/legacy_sqlite/") or not key.endswith(tuple(_RAW_SUFFIXES)):
        raise ValueError("Raw metadata requires an L1 Raw object key")
    return validate_lake_object_key(f"{key}.metadata.json")


def sqlite_snapshot_object_key(
    *, snapshot_id: str, schema_version: int, content_sha256: str
) -> str:
    """Return the content-bound key for a legacy SQLite seed snapshot."""
    validate_identifier(snapshot_id, label="snapshot_id")
    version = _positive_version(schema_version)
    digest = validate_sha256(content_sha256)
    return validate_lake_object_key(
        f"lake/l1/raw/legacy_sqlite/market/schema=v{version}/{snapshot_id}/snapshot-{digest}.sqlite"
    )


def canonical_object_key(
    *,
    layer: LakeLayer,
    dataset: str,
    contract_version: int,
    partition_values: Mapping[str, PartitionValue],
    content_sha256: str,
    partition_by: tuple[str, ...] | None = None,
) -> str:
    validate_dataset_name(dataset)
    version = _positive_version(contract_version)
    digest = validate_sha256(content_sha256)
    if layer == "l1_canonical":
        base = f"lake/l1/canonical/{dataset}"
    elif layer == "l2_analytical":
        base = f"lake/l2/{dataset}"
    else:
        raise ValueError("layer must be l1_canonical or l2_analytical")
    partitions = "/".join(partition_segments(partition_values, partition_by=partition_by))
    return validate_lake_object_key(
        f"{base}/contract=v{version}/{partitions}/part-{digest}.parquet"
    )


def dataset_manifest_key(*, dataset: str, build_id: str) -> str:
    validate_dataset_name(dataset)
    validate_identifier(build_id, label="build_id")
    return validate_lake_object_key(f"lake/manifests/datasets/{dataset}/{build_id}.json")


def release_manifest_key(*, release_id: str) -> str:
    validate_identifier(release_id, label="release_id")
    return validate_lake_object_key(f"lake/manifests/releases/l1/{release_id}.json")


def current_l1_pointer_key() -> str:
    return "lake/pointers/l1/current.json"


def current_l2_pointer_key(*, dataset: str) -> str:
    validate_dataset_name(dataset)
    return validate_lake_object_key(f"lake/pointers/l2/{dataset}/current.json")


def calibration_bundle_manifest_key(*, bundle_id: str) -> str:
    validate_identifier(bundle_id, label="bundle_id")
    return validate_lake_object_key(f"lake/manifests/calibration-bundles/{bundle_id}.json")


def current_calibration_bundle_pointer_key() -> str:
    return "lake/pointers/calibration/current.json"


def pin_key(*, pin_id: str) -> str:
    validate_identifier(pin_id, label="pin_id")
    return validate_lake_object_key(f"lake/manifests/pins/{pin_id}.json")
