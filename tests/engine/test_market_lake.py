from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from baibai_engine.cli import DOMAINS
from baibai_engine.market.lake.cli import main as lake_main
from baibai_engine.market.lake.inventory import inventory
from baibai_engine.market.lake.keys import (
    canonical_object_key,
    current_l1_pointer_key,
    dataset_manifest_key,
    raw_object_key,
    release_manifest_key,
)
from baibai_engine.market.lake.models import (
    DatasetManifest,
    ReleaseManifest,
    load_manifest_json,
)


def _dataset_payload() -> dict[str, object]:
    digest = "a" * 64
    key = canonical_object_key(
        layer="l1_canonical",
        dataset="jquants.daily_bars",
        contract_version=1,
        partition_values={"month": 8, "year": 2026},
        content_sha256=digest,
    )
    return {
        "manifest_version": 1,
        "dataset": "jquants.daily_bars",
        "layer": "l1_canonical",
        "contract_version": 1,
        "build_id": "20260812T123456Z-58d3057a-build",
        "source_ingest_ids": ["20260812T120000Z-ingest"],
        "source_release_ids": [],
        "producer_git_commit": "b" * 40,
        "transform_fingerprint": f"sha256:{'c' * 64}",
        "created_at": "2026-08-12T12:34:56Z",
        "data_as_of": "2026-08-12",
        "partition_by": ["year", "month"],
        "partitions": [
            {
                "values": {"year": 2026, "month": 8},
                "source_ingest_ids": ["20260812T120000Z-ingest"],
                "source_state_sha256": "b" * 64,
                "objects": [
                    {
                        "key": key,
                        "etag": '"etag"',
                        "sha256": digest,
                        "bytes": 123,
                        "rows": 456,
                        "min_key": ["2026-08-01", "1301"],
                        "max_key": ["2026-08-12", "9999"],
                    }
                ],
            }
        ],
        "totals": {"objects": 1, "bytes": 123, "rows": 456},
    }


def _load_dataset(payload: dict[str, object]) -> DatasetManifest:
    manifest = load_manifest_json(json.dumps(payload))
    assert isinstance(manifest, DatasetManifest)
    return manifest


def test_dataset_manifest_is_strict_and_round_trips() -> None:
    payload = _dataset_payload()
    manifest = _load_dataset(payload)

    reparsed = DatasetManifest.model_validate_json(manifest.model_dump_json())

    assert reparsed == manifest
    assert manifest.model_dump(mode="json") == payload


def _release_payload() -> dict[str, object]:
    return {
        "manifest_version": 1,
        "release_id": "20260812T130000Z-release",
        "created_at": "2026-08-12T13:00:00Z",
        "data_as_of": "2026-08-12",
        "datasets": {
            "jquants.daily_bars": {
                "build_id": "20260812T123456Z-58d3057a-build",
                "contract_version": 1,
                "manifest_sha256": "d" * 64,
            }
        },
    }


def test_release_manifest_is_strict_and_round_trips() -> None:
    payload = _release_payload()

    manifest = load_manifest_json(json.dumps(payload))

    assert isinstance(manifest, ReleaseManifest)
    assert ReleaseManifest.model_validate_json(manifest.model_dump_json()) == manifest


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
        (("source_ingest_ids",), None),
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

    with pytest.raises(ValidationError):
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
