from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from baibai_engine.cli import DOMAINS
from baibai_engine.market.lake import models as lake_models
from baibai_engine.market.lake.cli import main as lake_main
from baibai_engine.market.lake.inventory import inventory
from baibai_engine.market.lake.keys import (
    canonical_object_key,
    current_l1_pointer_key,
    dataset_manifest_key,
    raw_metadata_object_key,
    raw_object_key,
    release_manifest_key,
)
from baibai_engine.market.lake.models import (
    MAX_LAKE_JSON_BYTES,
    DatasetManifest,
    PartitionManifest,
    RawArchiveMetadata,
    RawIngestSourceRef,
    ReleaseManifest,
    canonical_lake_model_bytes,
    load_lake_model_json,
    load_manifest_json,
    validate_release_policy,
)
from baibai_engine.market.lake.sources import resolve_source_ref


def _raw_source_payload(*, dataset: str, ingest_id: str) -> dict[str, object]:
    key = raw_object_key(
        provider="jquants",
        dataset=dataset,
        ingest_date=date(2026, 8, 12),
        ingest_id=ingest_id,
        suffix=".json.gz",
    )
    metadata = RawArchiveMetadata(
        metadata_version=1,
        provider="jquants",
        dataset=dataset,
        ingest_id=ingest_id,
        retrieved_at=datetime(2026, 8, 12, 12, tzinfo=UTC),
        retention_class="buffer",
        suffix=".json.gz",
        endpoint="https://api.jquants.com/v2/markets",
        request_start=date(2026, 8, 1),
        request_end=date(2026, 8, 12),
        object_key=key,
        content_sha256="f" * 64,
        bytes=3,
    )
    metadata_bytes = canonical_lake_model_bytes(metadata)
    return {
        "kind": "raw_ingest",
        "source_id": ingest_id,
        "provider": "jquants",
        "dataset": dataset,
        "request_start": "2026-08-01",
        "request_end": "2026-08-12",
        "key": key,
        "sha256": "f" * 64,
        "metadata_version": 1,
        "metadata_key": raw_metadata_object_key(raw_key=key),
        "metadata_sha256": hashlib.sha256(metadata_bytes).hexdigest(),
    }


def _snapshot_source_payload(digest: str = "9" * 64, schema_version: int = 22) -> dict[str, object]:
    """The sealed store generation a pilot manifest was exported from, identity only."""
    return {
        "kind": "sqlite_snapshot",
        "source_id": f"market-v{schema_version}-{digest[:24]}",
        "sha256": digest,
        "schema_version": schema_version,
        "captured_at": "2026-08-12T12:00:00Z",
    }


def _dataset_payload(
    *,
    dataset: str = "jquants.daily_bars",
    raw_dataset: str = "daily_bars",
    data_as_of: str = "2026-08-12",
    digest: str = "a" * 64,
    coverage_start: str = "2026-08-01",
    population_count: int = 100,
    rows: int = 456,
) -> dict[str, object]:
    key = canonical_object_key(
        layer="l1_canonical",
        dataset=dataset,
        contract_version=1,
        partition_values={"month": 8, "year": 2026},
        content_sha256=digest,
    )
    return {
        "manifest_version": 1,
        "dataset": dataset,
        "layer": "l1_canonical",
        "contract_version": 1,
        "build_id": "20260812T123456Z-58d3057a-build",
        "sources": [],
        "producer_git_commit": "b" * 40,
        "transform_fingerprint": f"sha256:{'c' * 64}",
        "created_at": "2026-08-12T12:34:56Z",
        "coverage_start": coverage_start,
        "data_as_of": data_as_of,
        "population_count": population_count,
        "coverage_status": "complete",
        "partition_by": ["year", "month"],
        "partitions": [
            {
                "values": {"year": 2026, "month": 8},
                "sources": [
                    _raw_source_payload(
                        dataset=raw_dataset,
                        ingest_id="20260812T120000Z-ingest",
                    ),
                    _snapshot_source_payload(),
                ],
                "source_state_sha256": "b" * 64,
                "objects": [
                    {
                        "key": key,
                        "sha256": digest,
                        "bytes": 123,
                        "rows": rows,
                        "min_key": ["2026-08-01", "1301"],
                        "max_key": ["2026-08-12", "9999"],
                    }
                ],
            }
        ],
        "totals": {"objects": 1, "bytes": 123, "rows": rows},
    }


def _load_dataset(payload: dict[str, object]) -> DatasetManifest:
    manifest = load_manifest_json(json.dumps(payload))
    assert isinstance(manifest, DatasetManifest)
    return manifest


def test_dataset_manifest_is_strict_and_round_trips() -> None:
    payload = _dataset_payload()
    payload["cohort_inventory"] = {}
    manifest = _load_dataset(payload)

    reparsed = load_lake_model_json(manifest.model_dump_json(), DatasetManifest)

    assert reparsed == manifest
    assert manifest.model_dump(mode="json") == payload


def _release_payload() -> dict[str, object]:
    return {
        "manifest_version": 1,
        "release_id": "20260812T130000Z-release",
        "profile": "pilot",
        "created_at": "2026-08-12T13:00:00Z",
        "data_as_of": "2026-08-12",
        "datasets": {
            "jquants.daily_bars": {
                "build_id": "20260812T123456Z-58d3057a-build",
                "contract_version": 1,
                "manifest_sha256": "d" * 64,
                "data_as_of": "2026-08-12",
                "coverage_status": "complete",
                "totals": {"objects": 1, "bytes": 123, "rows": 456},
            }
        },
    }


def test_release_manifest_is_strict_and_round_trips() -> None:
    payload = _release_payload()

    manifest = load_manifest_json(json.dumps(payload))

    assert isinstance(manifest, ReleaseManifest)
    assert load_lake_model_json(manifest.model_dump_json(), ReleaseManifest) == manifest


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("manifest_sha256", None),
        ("manifest_sha256", ""),
        ("manifest_sha256", "D" * 64),
        ("manifest_sha256", "d" * 63),
    ],
)
def test_release_dataset_requires_a_well_formed_manifest_digest(field: str, value: object) -> None:
    """The digest is what makes a release pin data rather than names."""

    payload = _release_payload()
    datasets = payload["datasets"]
    assert isinstance(datasets, dict)
    if value is None:
        del datasets["jquants.daily_bars"][field]
    else:
        datasets["jquants.daily_bars"][field] = value

    with pytest.raises((ValidationError, ValueError)):
        load_manifest_json(json.dumps(payload))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("contract_version",), 0),
        (("coverage_start",), None),
        (("population_count",), 0),
        (("population_count",), 457),
        (("sources",), None),
        (("partition_by",), ["year", "date"]),
        (("totals", "rows"), 455),
        (("partitions", 0, "values", "month"), 0),
        (("partitions", 0, "objects", 0, "key"), "lake/../secret.parquet"),
    ],
)
def test_dataset_manifest_rejects_contract_gaps(path: tuple[str | int, ...], value: object) -> None:
    payload = copy.deepcopy(_dataset_payload())
    target: object = payload
    for part in path[:-1]:
        assert isinstance(target, (dict, list))
        target = target[part]  # type: ignore[index]
    assert isinstance(target, (dict, list))
    target[path[-1]] = value  # type: ignore[index]

    with pytest.raises((ValidationError, ValueError)):
        _load_dataset(payload)


def test_dataset_manifest_rejects_unknown_nested_fields() -> None:
    payload = _dataset_payload()
    partitions = payload["partitions"]
    assert isinstance(partitions, list)
    objects = partitions[0]["objects"]
    assert isinstance(objects, list)
    objects[0]["secret"] = "must-not-be-accepted"

    with pytest.raises(ValueError, match="extra_forbidden"):
        _load_dataset(payload)


@pytest.mark.parametrize("field", ["coverage_start", "population_count"])
def test_dataset_manifest_requires_coverage_evidence(field: str) -> None:
    payload = _dataset_payload()
    del payload[field]

    with pytest.raises((ValidationError, ValueError)):
        _load_dataset(payload)


def test_pilot_partition_layout_changes_only_with_contract_bump() -> None:
    payload = _dataset_payload()
    digest = "a" * 64
    payload["contract_version"] = 2
    payload["partition_by"] = ["date"]
    partitions = payload["partitions"]
    assert isinstance(partitions, list)
    partition = partitions[0]
    partition["values"] = {"date": "2026-08-12"}
    objects = partition["objects"]
    assert isinstance(objects, list)
    objects[0]["key"] = canonical_object_key(
        layer="l1_canonical",
        dataset="jquants.daily_bars",
        contract_version=2,
        partition_values={"date": "2026-08-12"},
        content_sha256=digest,
        partition_by=("date",),
    )

    manifest = _load_dataset(payload)

    assert manifest.contract_version == 2
    assert manifest.partition_by == ("date",)


def test_manifest_loader_rejects_duplicate_json_fields() -> None:
    raw = json.dumps(_dataset_payload())
    duplicate = raw.replace(
        '"manifest_version": 1,', '"manifest_version": 1, "manifest_version": 1,'
    )

    with pytest.raises(ValueError, match="duplicate JSON field"):
        load_manifest_json(duplicate)


def test_manifest_loader_rejects_oversized_wire_payload_before_parsing() -> None:
    payload = b"{" + (b" " * MAX_LAKE_JSON_BYTES) + b"}"

    with pytest.raises(ValueError, match="wire size limit"):
        load_manifest_json(payload)


@pytest.mark.parametrize(
    "replacement",
    [
        '"values": {"year": 2026, "year": 2026, "month": 8}',
        '"sources": [{"kind": "raw_ingest", "kind": "raw_ingest"',
    ],
)
def test_manifest_loader_rejects_duplicate_nested_json_fields(replacement: str) -> None:
    raw = json.dumps(_dataset_payload())
    if replacement.startswith('"values"'):
        duplicate = raw.replace('"values": {"year": 2026, "month": 8}', replacement)
    else:
        duplicate = raw.replace(
            '"sources": [{"kind": "raw_ingest"',
            replacement,
        )

    with pytest.raises(ValueError, match="duplicate JSON field"):
        load_manifest_json(duplicate)


def test_manifest_nested_mappings_are_immutable() -> None:
    manifest = _load_dataset(_dataset_payload())

    with pytest.raises(TypeError):
        manifest.partitions[0].values["month"] = 9  # type: ignore[index]

    release_payload = {
        "manifest_version": 1,
        "release_id": "release-1",
        "profile": "pilot",
        "created_at": "2026-08-12T13:00:00Z",
        "data_as_of": "2026-08-12",
        "datasets": {
            "jquants.daily_bars": {
                "build_id": "build-1",
                "contract_version": 1,
                "manifest_sha256": "d" * 64,
                "data_as_of": "2026-08-12",
                "coverage_status": "complete",
                "totals": {"objects": 1, "bytes": 123, "rows": 456},
            }
        },
    }
    release = load_lake_model_json(json.dumps(release_payload), ReleaseManifest)

    with pytest.raises(TypeError):
        release.datasets["other.dataset"] = release.datasets["jquants.daily_bars"]  # type: ignore[index]


def test_lake_parser_redacts_invalid_values() -> None:
    payload = _dataset_payload()
    payload["credential"] = "super-secret-value"

    with pytest.raises(ValueError, match="extra_forbidden") as captured:
        load_lake_model_json(json.dumps(payload), DatasetManifest)

    assert "extra_forbidden" in str(captured.value)
    assert "super-secret-value" not in str(captured.value)


def test_source_ref_rejects_unknown_kind_and_prefix_identity() -> None:
    payload = _dataset_payload()
    partitions = payload["partitions"]
    assert isinstance(partitions, list)
    sources = partitions[0]["sources"]
    assert isinstance(sources, list)
    sources[0]["kind"] = "legacy-magic-prefix"
    with pytest.raises(ValueError, match="union_tag_invalid"):
        _load_dataset(payload)

    prefixed = _raw_source_payload(dataset="daily_bars", ingest_id="ingest.variant")
    prefixed["source_id"] = "ingest"
    prefixed["request_start"] = date(2026, 8, 1)
    prefixed["request_end"] = date(2026, 8, 12)
    with pytest.raises(ValueError, match="key does not match source_id"):
        RawIngestSourceRef.model_validate(prefixed)


def test_release_policy_rejects_incomplete_stale_or_missing_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    datasets = tuple(
        item.model_copy(
            update={
                "coverage_start_on_or_before": date(2026, 8, 1),
                "minimum_rows": 1,
                "minimum_population_count": 1,
            }
        )
        for item in lake_models.PILOT_RELEASE_POLICY.datasets
    )
    monkeypatch.setattr(
        lake_models,
        "PILOT_RELEASE_POLICY",
        lake_models.PILOT_RELEASE_POLICY.model_copy(update={"datasets": datasets}),
    )
    manifest = _load_dataset(_dataset_payload())
    short_sale = _load_dataset(
        _dataset_payload(
            dataset="jquants.short_sale_reports",
            raw_dataset="short_sale_reports",
            data_as_of="2026-08-11",
            digest="e" * 64,
        )
    )
    manifests = {manifest.dataset: manifest, short_sale.dataset: short_sale}
    release_payload = {
        "manifest_version": 1,
        "release_id": "release-1",
        "profile": "pilot",
        "created_at": "2026-08-13T00:00:00Z",
        "data_as_of": "2026-08-11",
        "datasets": {
            item.dataset: {
                "build_id": item.build_id,
                "contract_version": item.contract_version,
                "manifest_sha256": hashlib.sha256(canonical_lake_model_bytes(item)).hexdigest(),
                "data_as_of": item.data_as_of.isoformat(),
                "coverage_status": item.coverage_status,
                "totals": item.totals.model_dump(mode="json"),
            }
            for item in manifests.values()
        },
    }
    release = load_lake_model_json(json.dumps(release_payload), ReleaseManifest)

    evaluated_at = datetime(2026, 8, 13, tzinfo=UTC)
    validate_release_policy(release, manifests, evaluated_at=evaluated_at)

    incomplete = manifest.model_copy(update={"coverage_status": "partial"})
    with pytest.raises(ValueError, match="digest does not match"):
        validate_release_policy(
            release,
            {manifest.dataset: incomplete, short_sale.dataset: short_sale},
            evaluated_at=evaluated_at,
        )
    incomplete_release_dataset = release.datasets[manifest.dataset].model_copy(
        update={
            "coverage_status": "partial",
            "manifest_sha256": hashlib.sha256(canonical_lake_model_bytes(incomplete)).hexdigest(),
        }
    )
    with pytest.raises(ValueError, match="requires complete dataset coverage"):
        validate_release_policy(
            release.model_copy(
                update={
                    "datasets": {
                        **release.datasets,
                        manifest.dataset: incomplete_release_dataset,
                    }
                }
            ),
            {manifest.dataset: incomplete, short_sale.dataset: short_sale},
            evaluated_at=evaluated_at,
        )
    with pytest.raises(ValueError, match="freshness window"):
        validate_release_policy(
            release,
            manifests,
            evaluated_at=datetime(2026, 10, 1, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="missing a required dataset"):
        validate_release_policy(
            release.model_copy(
                update={"datasets": {manifest.dataset: release.datasets[manifest.dataset]}}
            ),
            {manifest.dataset: manifest},
            evaluated_at=evaluated_at,
        )
    with pytest.raises(ValueError, match="production release policy is not configured"):
        validate_release_policy(
            release.model_copy(update={"profile": "production"}),
            manifests,
            evaluated_at=evaluated_at,
        )
    with pytest.raises(ValueError, match="cannot be after its evaluation time"):
        validate_release_policy(
            release.model_copy(update={"created_at": datetime(2026, 8, 14, tzinfo=UTC)}),
            manifests,
            evaluated_at=evaluated_at,
        )


def test_pilot_policy_rejects_a_fresh_one_day_population() -> None:
    daily = _load_dataset(
        _dataset_payload(
            coverage_start="2026-08-12",
            population_count=1,
            rows=1,
        )
    )
    short_sale = _load_dataset(
        _dataset_payload(
            dataset="jquants.short_sale_reports",
            raw_dataset="short_sale_reports",
            coverage_start="2026-08-12",
            population_count=1,
            rows=1,
            digest="e" * 64,
        )
    )
    manifests = {item.dataset: item for item in (daily, short_sale)}
    release = ReleaseManifest.model_validate(
        {
            "manifest_version": 1,
            "release_id": "one-day-release",
            "profile": "pilot",
            "created_at": datetime(2026, 8, 13, tzinfo=UTC),
            "data_as_of": date(2026, 8, 12),
            "datasets": {
                item.dataset: {
                    "build_id": item.build_id,
                    "contract_version": item.contract_version,
                    "manifest_sha256": hashlib.sha256(canonical_lake_model_bytes(item)).hexdigest(),
                    "data_as_of": item.data_as_of,
                    "coverage_status": item.coverage_status,
                    "totals": item.totals,
                }
                for item in manifests.values()
            },
        }
    )

    with pytest.raises(ValueError, match="history boundary"):
        validate_release_policy(
            release,
            manifests,
            evaluated_at=datetime(2026, 8, 13, tzinfo=UTC),
        )


def test_typed_source_refs_resolve_and_validate_digest_and_version(tmp_path: Path) -> None:
    raw_key = raw_object_key(
        provider="jquants",
        dataset="daily_bars",
        ingest_date=date(2026, 8, 12),
        ingest_id="ingest-1",
        suffix=".json.gz",
    )
    raw_path = tmp_path / raw_key
    raw_path.parent.mkdir(parents=True)
    raw_path.write_bytes(b"raw")
    raw_metadata = RawArchiveMetadata(
        metadata_version=1,
        provider="jquants",
        dataset="daily_bars",
        ingest_id="ingest-1",
        retrieved_at=datetime(2026, 8, 12, 12, tzinfo=UTC),
        retention_class="buffer",
        suffix=".json.gz",
        endpoint="https://api.jquants.com/v2/markets",
        request_start=date(2026, 8, 1),
        request_end=date(2026, 8, 12),
        object_key=raw_key,
        content_sha256=hashlib.sha256(b"raw").hexdigest(),
        bytes=3,
    )
    metadata_key = raw_metadata_object_key(raw_key=raw_key)
    metadata_path = tmp_path / metadata_key
    metadata_bytes = canonical_lake_model_bytes(raw_metadata)
    metadata_path.write_bytes(metadata_bytes)
    raw = RawIngestSourceRef(
        kind="raw_ingest",
        source_id="ingest-1",
        provider="jquants",
        dataset="daily_bars",
        request_start=date(2026, 8, 1),
        request_end=date(2026, 8, 12),
        key=raw_key,
        sha256=hashlib.sha256(b"raw").hexdigest(),
        metadata_version=1,
        metadata_key=metadata_key,
        metadata_sha256=hashlib.sha256(metadata_bytes).hexdigest(),
    )
    assert resolve_source_ref(tmp_path, raw) == raw_path

    for update in (
        {"provider": "other"},
        {"dataset": "short_sale_reports"},
        {"request_start": date(2026, 7, 1)},
        {"request_end": date(2026, 8, 31)},
    ):
        with pytest.raises(ValueError, match="metadata identity does not match"):
            resolve_source_ref(tmp_path, raw.model_copy(update=update))
    with pytest.raises(ValueError, match="request_start must not be after request_end"):
        RawIngestSourceRef.model_validate(
            {
                **raw.model_dump(mode="json"),
                "request_start": date(2026, 8, 13),
                "request_end": date(2026, 8, 12),
            }
        )

    with pytest.raises(ValueError, match="digest does not match"):
        resolve_source_ref(tmp_path, raw.model_copy(update={"sha256": "0" * 64}))
    with pytest.raises(ValueError, match="metadata identity does not match"):
        resolve_source_ref(tmp_path, raw.model_copy(update={"metadata_version": 2}))
    with pytest.raises(ValueError, match="metadata reference digest does not match"):
        resolve_source_ref(tmp_path, raw.model_copy(update={"metadata_sha256": "0" * 64}))

    metadata_path.unlink()
    with pytest.raises(ValueError, match="metadata reference does not resolve"):
        resolve_source_ref(tmp_path, raw)

    # An L1 release is a read reference, not a lineage source: reproducing it needs its
    # dataset manifests, objects, and Raw archives kept whole, and nothing here walks
    # that closure yet. A partition that claimed it would name a lineage no publisher,
    # reader, or retention plan keeps.
    with pytest.raises(ValueError, match="PartitionManifest"):
        load_lake_model_json(
            json.dumps(
                {
                    "values": {"year": 2026, "month": 8},
                    "objects": [],
                    "sources": [
                        {
                            "kind": "l1_release",
                            "source_id": "release-1",
                            "key": release_manifest_key(release_id="release-1"),
                            "sha256": "e" * 64,
                            "manifest_version": 1,
                        }
                    ],
                    "source_state_sha256": "f" * 64,
                }
            ),
            PartitionManifest,
        )


def test_key_builders_are_deterministic_and_traversal_safe() -> None:
    digest = "d" * 64
    assert canonical_object_key(
        layer="l1_canonical",
        dataset="jquants.daily_bars",
        contract_version=1,
        partition_values={"month": 8, "year": 2026},
        content_sha256=digest,
    ) == (
        f"lake/l1/canonical/jquants.daily_bars/contract=v1/year=2026/month=8/part-{digest}.parquet"
    )
    assert (
        raw_object_key(
            provider="jquants",
            dataset="daily_bars",
            ingest_date=date(2026, 8, 12),
            ingest_id="ingest-1",
            suffix=".json.gz",
        )
        == "lake/l1/raw/jquants/daily_bars/ingest_date=2026-08-12/ingest-1.json.gz"
    )
    assert (
        dataset_manifest_key(dataset="jquants.daily_bars", build_id="build-1")
        == "lake/manifests/datasets/jquants.daily_bars/build-1.json"
    )
    assert (
        release_manifest_key(release_id="release-1") == "lake/manifests/releases/l1/release-1.json"
    )
    assert current_l1_pointer_key() == "lake/pointers/l1/current.json"

    with pytest.raises(ValueError, match="path-safe"):
        dataset_manifest_key(dataset="../secret", build_id="build-1")
    with pytest.raises(ValueError, match="path-safe"):
        release_manifest_key(release_id="../../release")
    with pytest.raises(ValueError, match="partition month"):
        canonical_object_key(
            layer="l1_canonical",
            dataset="jquants.daily_bars",
            contract_version=1,
            partition_values={"year": 2026, "month": 0},
            content_sha256=digest,
        )


def test_inventory_reads_metadata_only_and_groups_valid_keys(tmp_path: Path) -> None:
    key = (
        "lake/l1/canonical/jquants.daily_bars/contract=v1/"
        f"year=2026/month=8/part-{'e' * 64}.parquet"
    )
    object_path = tmp_path / key
    object_path.parent.mkdir(parents=True)
    object_path.write_bytes(b"parquet")
    invalid = tmp_path / "outside.txt"
    invalid.write_text("ignored", encoding="utf-8")

    result = inventory(tmp_path)

    assert result["objects"] == 2
    assert result["bytes"] == 14
    assert result["areas"] == [
        {
            "prefix": "lake/l1/canonical/jquants.daily_bars",
            "objects": 1,
            "bytes": 7,
        }
    ]
    assert result["invalid_keys"] == ["outside.txt"]


def test_inventory_reports_raw_retention_class_bytes_and_age(tmp_path: Path) -> None:
    raw = b"raw-bytes"
    digest = hashlib.sha256(raw).hexdigest()
    key = raw_object_key(
        provider="jquants",
        dataset="jquants.daily_bars",
        ingest_date=date(2026, 8, 12),
        ingest_id="inventory-buffer",
        suffix=".json.gz",
    )
    object_path = tmp_path / key
    object_path.parent.mkdir(parents=True)
    object_path.write_bytes(raw)
    metadata = RawArchiveMetadata(
        metadata_version=1,
        provider="jquants",
        dataset="jquants.daily_bars",
        ingest_id="inventory-buffer",
        retrieved_at=datetime(2026, 8, 12, 12, tzinfo=UTC),
        retention_class="buffer",
        suffix=".json.gz",
        object_key=key,
        content_sha256=digest,
        bytes=len(raw),
    )
    metadata_path = tmp_path / raw_metadata_object_key(raw_key=key)
    metadata_path.write_bytes(canonical_lake_model_bytes(metadata))

    result = inventory(tmp_path)

    by_class = {item["class"]: item for item in result["raw_retention"]}
    assert by_class["buffer"]["objects"] == 1
    assert by_class["buffer"]["bytes"] == len(raw)
    assert by_class["buffer"]["oldest_retrieved_at"] is not None
    capacity = {item["class"]: item for item in result["capacity"]}
    assert capacity["raw_buffer"]["bytes"] == len(raw)
    assert capacity["raw_buffer"]["budget_exceeded"] is False
    assert result["raw_inventory_errors"] == []
    assert result["raw_unclassified"] == {"objects": 0, "bytes": 0}


def test_inventory_reports_a_raw_payload_without_its_metadata(tmp_path: Path) -> None:
    key = raw_object_key(
        provider="jquants",
        dataset="jquants.daily_bars",
        ingest_date=date(2026, 8, 12),
        ingest_id="orphan-payload",
        suffix=".json.gz",
    )
    path = tmp_path / key
    path.parent.mkdir(parents=True)
    path.write_bytes(b"orphan")

    result = inventory(tmp_path)

    assert result["raw_unclassified"] == {"objects": 1, "bytes": 6}
    assert result["raw_inventory_errors"] == [
        {"key": key, "error": "Raw object has no metadata sidecar"}
    ]


def test_validate_cli_is_read_only_and_redacts_rejected_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "manifest.json"
    payload = _dataset_payload()
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes()

    assert lake_main(["validate", "--manifest", str(path)]) == 0
    output = yaml.safe_load(capsys.readouterr().out)
    assert output["status"] == "ok"
    assert output["scope"] == "manifest_contract"
    assert path.read_bytes() == before

    payload["credential"] = "super-secret-value"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert lake_main(["validate", "--manifest", str(path)]) == 1
    error = capsys.readouterr().err
    assert "extra_forbidden" in error
    assert "super-secret-value" not in error


def test_lake_domain_is_exposed_by_the_root_cli() -> None:
    assert DOMAINS["lake"].module == "baibai_engine.market.lake.cli"


def test_inventory_reports_every_class_against_its_own_budget(tmp_path: Path) -> None:
    """A class that grows for a design reason has to be visible against its objective.

    Reporting only the Raw budget would let the published graph pass the objective it
    was sized against without anything saying so, and would hide a workspace holding a
    sealed store the size of the whole legacy database behind a figure fifty times
    larger. Workspace bytes are under no manifest, so nothing else in this report grows
    when they do.
    """

    published = tmp_path / (
        "lake/l1/canonical/jquants.daily_bars/contract=v1/year=2026/month=8/"
        f"part-{'a' * 64}.parquet"
    )
    published.parent.mkdir(parents=True)
    published.write_bytes(b"parquet")
    failed = tmp_path / "lake/staging/failed-build/part.parquet"
    failed.parent.mkdir(parents=True)
    failed.write_bytes(b"quarantined")
    staged = tmp_path / "lake/staging/snapshot-1/snapshot.sqlite"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"sealed snapshot")

    (tmp_path / ".lake-writer.lock").write_bytes(b"")

    report = inventory(tmp_path)
    capacity = {item["class"]: item for item in report["capacity"]}

    # The writer lock is an operational file beside the namespace, not an object in it.
    # Counting it makes the totals disagree with the prefix breakdown, and reporting it
    # as an invalid key makes a healthy store look corrupt.
    assert report["invalid_keys"] == []
    assert report["control_files"] == {"objects": 1, "bytes": 0}
    assert report["objects"] == 3

    assert set(capacity) == {"published", "raw_buffer", "workspace"}
    assert capacity["published"]["bytes"] == 7
    assert capacity["workspace"]["bytes"] == 26
    assert capacity["published"]["soft_budget_bytes"] == 10 * 1024**3
    assert not any(item["budget_exceeded"] for item in capacity.values())
