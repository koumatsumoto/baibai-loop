from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from tests.helpers.lake_policy import narrow_release_policy

from baibai_engine.cli import DOMAINS
from baibai_engine.storage.lake import models as lake_models
from baibai_engine.storage.lake.cli import main as lake_main
from baibai_engine.storage.lake.datasets import LAKE_DATASETS
from baibai_engine.storage.lake.inventory import inventory
from baibai_engine.storage.lake.keys import (
    LakeLayer,
    canonical_object_key,
    current_release_pointer_key,
    dataset_manifest_key,
    release_manifest_key,
)
from baibai_engine.storage.lake.models import (
    MAX_LAKE_JSON_BYTES,
    DatasetManifest,
    PartitionManifest,
    ReleaseManifest,
    canonical_lake_model_bytes,
    load_lake_model_json,
    load_manifest_json,
    validate_release_policy,
)
from baibai_engine.storage.lake.objects import LocalMirrorSource
from baibai_engine.storage.lake.reader import LakeReadError, accepted_dataset, resolve_release


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
    data_as_of: str = "2026-08-12",
    digest: str = "a" * 64,
    coverage_start: str = "2026-08-01",
    population_count: int | None = 100,
    rows: int = 456,
) -> dict[str, object]:
    contract = LAKE_DATASETS[dataset]
    version = contract.contract_version
    partition_values = {"year": 2026}
    if contract.partition_grain == "month":
        partition_values["month"] = 8
    key = canonical_object_key(
        layer=contract.layer,
        dataset=dataset,
        contract_version=version,
        partition_values=partition_values,
        partition_by=contract.partition_by,
        content_sha256=digest,
    )
    return {
        "manifest_version": 2,
        "dataset": dataset,
        "layer": contract.layer,
        "contract_version": version,
        "build_id": "20260812T123456Z-58d3057a-build",
        "sources": [],
        "producer_git_commit": "b" * 40,
        "created_at": "2026-08-12T12:34:56Z",
        "coverage_start": coverage_start,
        "data_as_of": data_as_of,
        "population_count": population_count,
        "coverage_status": "complete",
        "partition_by": list(contract.partition_by),
        "partitions": [
            {
                "values": partition_values,
                "sources": [_snapshot_source_payload()],
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
    manifest = _load_dataset(payload)

    reparsed = load_lake_model_json(manifest.model_dump_json(), DatasetManifest)

    assert reparsed == manifest
    assert manifest.model_dump(mode="json") == payload


def test_edinet_list_responses_manifest_round_trips_as_optional_l1() -> None:
    contract = LAKE_DATASETS["edinet.list_responses"]
    payload = _dataset_payload(dataset=contract.name, population_count=None)
    manifest = _load_dataset(payload)
    assert manifest.layer == "l1_source"
    assert manifest.contract_version == contract.contract_version
    assert manifest.partition_by == ("year",)
    assert manifest.population_count is None
    assert _load_dataset(manifest.model_dump(mode="json")) == manifest
    policy = next(
        item
        for item in lake_models.PRODUCTION_RELEASE_POLICY.datasets
        if item.dataset == contract.name
    )
    assert not policy.required
    assert policy.carries_history


@pytest.mark.parametrize(
    ("dataset", "wrong_layer"),
    [
        ("jquants.daily_bars", "l1_source"),
        ("jquants.fin_summaries", "l1_source"),
        ("jquants.master_snapshots", "l1_source"),
        ("jquants.source_pages", "l2_processed"),
    ],
)
def test_dataset_manifest_rejects_wrong_contract_layer_with_valid_object_key(
    dataset: str, wrong_layer: LakeLayer
) -> None:
    payload = _dataset_payload(dataset=dataset)
    payload["layer"] = wrong_layer
    partitions = payload["partitions"]
    assert isinstance(partitions, list)
    partition = partitions[0]
    assert isinstance(partition, dict)
    objects = partition["objects"]
    assert isinstance(objects, list)
    lake_object = objects[0]
    assert isinstance(lake_object, dict)
    assert isinstance(partition["values"], dict)
    assert isinstance(lake_object["sha256"], str)
    lake_object["key"] = canonical_object_key(
        layer=wrong_layer,
        dataset=dataset,
        contract_version=LAKE_DATASETS[dataset].contract_version,
        partition_values=partition["values"],
        partition_by=LAKE_DATASETS[dataset].partition_by,
        content_sha256=lake_object["sha256"],
    )

    with pytest.raises(ValueError, match="dataset layer does not match its current contract"):
        DatasetManifest.model_validate_json(json.dumps(payload))


def _release_payload() -> dict[str, object]:
    return {
        "manifest_version": 2,
        "release_id": "20260812T130000Z-release",
        "profile": "production",
        "created_at": "2026-08-12T13:00:00Z",
        "data_as_of": "2026-08-12",
        "datasets": {
            "jquants.daily_bars": {
                "build_id": "20260812T123456Z-58d3057a-build",
                "layer": LAKE_DATASETS["jquants.daily_bars"].layer,
                "contract_version": LAKE_DATASETS["jquants.daily_bars"].contract_version,
                "manifest_sha256": "d" * 64,
                "data_as_of": "2026-08-12",
                "coverage_status": "complete",
                "totals": {"objects": 1, "bytes": 123, "rows": 456},
            }
        },
    }


def test_release_manifest_is_strict_and_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    narrow_release_policy(monkeypatch)
    payload = _release_payload()

    manifest = load_manifest_json(json.dumps(payload))

    assert isinstance(manifest, ReleaseManifest)
    assert load_lake_model_json(manifest.model_dump_json(), ReleaseManifest) == manifest


def test_release_policy_rejects_inventory_layer_mismatching_valid_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    narrow_release_policy(monkeypatch, datasets=("jquants.daily_bars",))
    manifest = _load_dataset(_dataset_payload())
    payload = _release_payload()
    datasets = payload["datasets"]
    assert isinstance(datasets, dict)
    entry = datasets[manifest.dataset]
    assert isinstance(entry, dict)
    entry["manifest_sha256"] = hashlib.sha256(canonical_lake_model_bytes(manifest)).hexdigest()
    valid = load_lake_model_json(json.dumps(payload), ReleaseManifest)
    validate_release_policy(valid, {manifest.dataset: manifest})

    entry["layer"] = "l1_source"
    wrong_layer = load_lake_model_json(json.dumps(payload), ReleaseManifest)
    with pytest.raises(ValueError, match="release dataset inventory does not match its manifest"):
        validate_release_policy(wrong_layer, {manifest.dataset: manifest})


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


@pytest.mark.parametrize("field", ["coverage_start"])
def test_dataset_manifest_requires_coverage_evidence(field: str) -> None:
    payload = _dataset_payload()
    del payload[field]

    with pytest.raises((ValidationError, ValueError)):
        _load_dataset(payload)


def test_pilot_partition_layout_changes_only_with_contract_bump() -> None:
    payload = _dataset_payload()
    digest = "a" * 64
    next_version = LAKE_DATASETS["jquants.daily_bars"].contract_version + 1
    payload["contract_version"] = next_version
    payload["partition_by"] = ["date"]
    partitions = payload["partitions"]
    assert isinstance(partitions, list)
    partition = partitions[0]
    partition["values"] = {"date": "2026-08-12"}
    objects = partition["objects"]
    assert isinstance(objects, list)
    objects[0]["key"] = canonical_object_key(
        layer="l2_processed",
        dataset="jquants.daily_bars",
        contract_version=next_version,
        partition_values={"date": "2026-08-12"},
        content_sha256=digest,
        partition_by=("date",),
    )

    manifest = _load_dataset(payload)

    assert manifest.contract_version == next_version
    assert manifest.partition_by == ("date",)


def test_manifest_loader_rejects_duplicate_json_fields() -> None:
    raw = json.dumps(_dataset_payload())
    duplicate = raw.replace(
        '"manifest_version": 2,', '"manifest_version": 2, "manifest_version": 2,'
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
        '"sources": [{"kind": "sqlite_snapshot", "kind": "sqlite_snapshot"',
    ],
)
def test_manifest_loader_rejects_duplicate_nested_json_fields(replacement: str) -> None:
    raw = json.dumps(_dataset_payload())
    if replacement.startswith('"values"'):
        duplicate = raw.replace('"values": {"year": 2026, "month": 8}', replacement)
    else:
        duplicate = raw.replace(
            '"sources": [{"kind": "sqlite_snapshot"',
            replacement,
        )

    with pytest.raises(ValueError, match="duplicate JSON field"):
        load_manifest_json(duplicate)


def test_manifest_nested_mappings_are_immutable(monkeypatch: pytest.MonkeyPatch) -> None:
    narrow_release_policy(monkeypatch)
    manifest = _load_dataset(_dataset_payload())

    with pytest.raises(TypeError):
        manifest.partitions[0].values["month"] = 9  # type: ignore[index]

    release_payload = {
        "manifest_version": 2,
        "release_id": "release-1",
        "profile": "production",
        "created_at": "2026-08-12T13:00:00Z",
        "data_as_of": "2026-08-12",
        "datasets": {
            "jquants.daily_bars": {
                "build_id": "build-1",
                "layer": LAKE_DATASETS["jquants.daily_bars"].layer,
                "contract_version": LAKE_DATASETS["jquants.daily_bars"].contract_version,
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


def test_release_policy_rejects_incomplete_or_missing_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    narrow_release_policy(monkeypatch)
    manifest = _load_dataset(_dataset_payload())
    short_sale = _load_dataset(
        _dataset_payload(
            dataset="jquants.short_sale_reports",
            data_as_of="2026-08-11",
            digest="e" * 64,
        )
    )
    manifests = {manifest.dataset: manifest, short_sale.dataset: short_sale}
    release_payload = {
        "manifest_version": 2,
        "release_id": "release-1",
        "profile": "production",
        "created_at": "2026-08-13T00:00:00Z",
        "data_as_of": "2026-08-11",
        "datasets": {
            item.dataset: {
                "build_id": item.build_id,
                "layer": item.layer,
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

    validate_release_policy(release, manifests)

    # A ticker-based contract must report its actual population count. This is
    # distinct from requiring a historical number of tickers: None means that a
    # population is inapplicable, not that its declared count was omitted.
    unpopulated = manifest.model_copy(update={"population_count": None})
    unpopulated_release = release.model_copy(
        update={
            "datasets": {
                **release.datasets,
                manifest.dataset: release.datasets[manifest.dataset].model_copy(
                    update={
                        "manifest_sha256": hashlib.sha256(
                            canonical_lake_model_bytes(unpopulated)
                        ).hexdigest()
                    }
                ),
            }
        }
    )
    with pytest.raises(ValueError, match="does not report the population"):
        validate_release_policy(
            unpopulated_release,
            {manifest.dataset: unpopulated, short_sale.dataset: short_sale},
        )

    incomplete = manifest.model_copy(update={"coverage_status": "partial"})
    with pytest.raises(ValueError, match="digest does not match"):
        validate_release_policy(
            release,
            {manifest.dataset: incomplete, short_sale.dataset: short_sale},
        )
    incomplete_release_dataset = release.datasets[manifest.dataset].model_copy(
        update={
            "coverage_status": "partial",
            "manifest_sha256": hashlib.sha256(canonical_lake_model_bytes(incomplete)).hexdigest(),
        }
    )
    with pytest.raises(ValueError, match="but its profile requires"):
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
        )
    with pytest.raises(ValueError, match="missing a required dataset"):
        validate_release_policy(
            release.model_copy(
                update={"datasets": {manifest.dataset: release.datasets[manifest.dataset]}}
            ),
            {manifest.dataset: manifest},
        )
    # `model_copy` does not revalidate, so this is how a manifest carrying a profile
    # no policy is registered for reaches the gate: it must refuse rather than fall
    # back to the one policy that does exist.
    with pytest.raises(ValueError, match="release policy is not configured for profile"):
        validate_release_policy(
            release.model_copy(update={"profile": "retired"}),
            manifests,
        )


@pytest.mark.parametrize(("rows", "population", "accepted"), [(1, 1, True), (0, None, False)])
def test_new_generation_uses_declared_observations_not_historical_size(
    monkeypatch: pytest.MonkeyPatch,
    rows: int,
    population: int | None,
    accepted: bool,
) -> None:
    narrow_release_policy(monkeypatch, carries_history=None)
    daily = _load_dataset(
        _dataset_payload(
            coverage_start="2026-08-12",
            population_count=population,
            rows=rows,
        )
    )
    short_sale = _load_dataset(
        _dataset_payload(
            dataset="jquants.short_sale_reports",
            coverage_start="2026-08-12",
            population_count=population,
            rows=rows,
            digest="e" * 64,
        )
    )
    manifests = {item.dataset: item for item in (daily, short_sale)}
    release = ReleaseManifest.model_validate(
        {
            "manifest_version": 2,
            "release_id": "one-day-release",
            "profile": "production",
            "created_at": datetime(2026, 8, 13, tzinfo=UTC),
            "data_as_of": date(2026, 8, 12),
            "datasets": {
                item.dataset: {
                    "build_id": item.build_id,
                    "layer": item.layer,
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

    # A new generation declares its actual scope. Old accumulated counts do not
    # prove a source promise, but empty or contradictory observations are invalid.
    if not accepted:
        with pytest.raises(ValueError, match="contain observations"):
            validate_release_policy(release, manifests)
        return
    validate_release_policy(release, manifests)

    with pytest.raises(ValueError, match="the serving release already covers"):
        validate_release_policy(
            release,
            manifests,
            published_coverage_start={"jquants.daily_bars": date(2016, 8, 1)},
        )


def test_a_release_reference_is_not_admissible_as_partition_lineage() -> None:
    # An L1 release is a read reference, not a lineage source: reproducing it needs its
    # dataset manifests and objects kept whole, and nothing here walks that closure yet.
    # A partition that claimed it would name a lineage no publisher, reader, or
    # retention plan keeps.
    with pytest.raises(ValueError, match="PartitionManifest"):
        load_lake_model_json(
            json.dumps(
                {
                    "values": {"year": 2026, "month": 8},
                    "objects": [],
                    "sources": [
                        {
                            "kind": "lake_release",
                            "source_id": "release-1",
                            "key": release_manifest_key(release_id="release-1"),
                            "sha256": "e" * 64,
                            "manifest_version": 2,
                        }
                    ],
                }
            ),
            PartitionManifest,
        )


def test_key_builders_are_deterministic_and_traversal_safe() -> None:
    digest = "d" * 64
    assert canonical_object_key(
        layer="l2_processed",
        dataset="jquants.daily_bars",
        contract_version=1,
        partition_values={"month": 8, "year": 2026},
        content_sha256=digest,
    ) == (
        f"lake/l2/processed/jquants.daily_bars/contract=v1/year=2026/month=8/part-{digest}.parquet"
    )
    assert (
        dataset_manifest_key(dataset="jquants.daily_bars", build_id="build-1")
        == "lake/manifests/datasets/jquants.daily_bars/build-1.json"
    )
    assert (
        release_manifest_key(release_id="release-1")
        == "lake/manifests/releases/market/release-1.json"
    )
    assert current_release_pointer_key() == "lake/pointers/market/current.json"

    with pytest.raises(ValueError, match="path-safe"):
        dataset_manifest_key(dataset="../secret", build_id="build-1")
    with pytest.raises(ValueError, match="path-safe"):
        release_manifest_key(release_id="../../release")
    with pytest.raises(ValueError, match="partition month"):
        canonical_object_key(
            layer="l2_processed",
            dataset="jquants.daily_bars",
            contract_version=1,
            partition_values={"year": 2026, "month": 0},
            content_sha256=digest,
        )


def test_inventory_reads_metadata_only_and_groups_valid_keys(tmp_path: Path) -> None:
    key = (
        "lake/l2/processed/jquants.daily_bars/contract=v1/"
        f"year=2026/month=8/part-{'e' * 64}.parquet"
    )
    object_path = tmp_path / key
    object_path.parent.mkdir(parents=True)
    object_path.write_bytes(b"parquet")
    # The mirror root is also where the operator keeps stores and backups. Those are not
    # lake objects at all, so they belong in neither the totals nor the invalid keys.
    (tmp_path / "market").mkdir()
    (tmp_path / "market" / "market.sqlite").write_bytes(b"store")
    (tmp_path / "outside.txt").write_text("ignored", encoding="utf-8")

    result = inventory(tmp_path)

    assert result["objects"] == 1
    assert result["bytes"] == 7
    assert result["areas"] == [
        {
            "prefix": "lake/l2/processed/jquants.daily_bars",
            "objects": 1,
            "bytes": 7,
        }
    ]
    assert result["invalid_keys"] == []


def test_inventory_reports_an_unsafe_key_inside_the_namespace(tmp_path: Path) -> None:
    """Narrowing the walk to the namespace must not stop it reporting a bad key in it."""

    stray = tmp_path / "lake" / "l2" / "processed" / "jquants daily bars" / "part.parquet"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"parquet")

    result = inventory(tmp_path)

    assert result["objects"] == 1
    assert result["invalid_keys"] == ["lake/l2/processed/jquants daily bars/part.parquet"]
    assert result["areas"] == []


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
    assert DOMAINS["lake"].module == "baibai_engine.storage.lake.cli"


def test_inventory_reports_every_class_against_its_own_budget(tmp_path: Path) -> None:
    """A class that grows for a design reason has to be visible against its objective.

    Reporting one figure would hide a workspace holding a sealed store the size of the
    whole legacy database behind the published graph's own objective. Workspace bytes
    are under no manifest, so nothing else in this report grows when they do.
    """

    published = tmp_path / (
        "lake/l2/processed/jquants.daily_bars/contract=v1/year=2026/month=8/"
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

    assert set(capacity) == {"published", "workspace"}
    assert capacity["published"]["bytes"] == 7
    assert capacity["workspace"]["bytes"] == 26
    assert capacity["published"]["soft_budget_bytes"] == 10 * 1024**3
    assert not any(item["budget_exceeded"] for item in capacity.values())


def test_every_lake_dataset_states_a_release_policy() -> None:
    """Every current dataset must state its publication policy.

    Unknown datasets are rejected; this catches omissions in the declared inventory.
    Freshness is evaluated by consumers, not by the release policy.
    """

    from baibai_engine.storage.lake.datasets import LAKE_DATASETS
    from baibai_engine.storage.lake.models import PRODUCTION_RELEASE_POLICY

    stated = {item.dataset for item in PRODUCTION_RELEASE_POLICY.datasets}

    assert stated == set(LAKE_DATASETS)


def test_a_replaced_snapshot_carries_no_history_floor() -> None:
    """置き換わる view に開始日を固定すると、source が約束していない履歴を課す。

    `jpx.earnings_calendar` の SQLite table は fetch ごとに DELETE されて入れ直る
    forward calendar なので、最古の行は取引所がまだ公表している範囲そのものである。
    2026-08-17 の日次バッチは、snapshot が 2026-06-19 始まりから 2026-07-03 始まりへ
    進んだだけで停止した。
    """

    from baibai_engine.storage.lake.models import PRODUCTION_RELEASE_POLICY

    policy = {item.dataset: item for item in PRODUCTION_RELEASE_POLICY.datasets}

    assert policy["jpx.earnings_calendar"].carries_history is False
    # 蓄積する dataset は床を持ち続ける。免除は snapshot に限る。
    assert policy["jquants.daily_bars"].carries_history is True
    assert policy["jquants.short_sale_reports"].carries_history is True


def test_only_a_replaced_table_is_exempt_from_the_history_floor() -> None:
    """免除の判定基準は「fetch が table を丸ごと置き換えるか」で、code から決まる。

    範囲指定なしの `DELETE FROM <table>` はその table を snapshot にする — 行は蓄積せず、
    最古の行も件数も source が今公表している範囲そのものになる。日付で絞った delete は
    冪等な upsert で、通常公開では保存済みの履歴開始日と比較する。

    将来 snapshot 型の dataset が増えたときも、過去 snapshot の履歴開始日を要求しない。
    この検査は現在の SQL リテラル表記を対象とし、動的 SQL の全経路を解析するものではない。
    """

    import re

    from baibai_engine.storage.lake.datasets import LAKE_DATASETS
    from baibai_engine.storage.lake.models import PRODUCTION_RELEASE_POLICY

    # Source writers live in market; inspect the owner of both snapshot datasets.
    market = Path(__file__).resolve().parents[2] / "engine/src/baibai_engine/market"
    replaced = {
        match.group(1)
        for source_root in (market, market.parent / "storage" / "observations")
        for path in source_root.rglob("*.py")
        for match in re.finditer(r"DELETE FROM (\w+)\s*(?:\"|')", path.read_text(encoding="utf-8"))
    }
    snapshot_datasets = {
        dataset.name for dataset in LAKE_DATASETS.values() if dataset.sqlite_table in replaced
    }
    exempt = {
        item.dataset for item in PRODUCTION_RELEASE_POLICY.datasets if not item.carries_history
    }

    assert snapshot_datasets == {
        "edinet.tender_offer_exit_values",
        "jpx.earnings_calendar",
    }
    assert exempt == snapshot_datasets


@pytest.mark.parametrize("rows", [1_100, 400, 1])
def test_a_forward_only_calendar_accepts_small_complete_schedules_not_a_history_floor(
    monkeypatch: pytest.MonkeyPatch, rows: int
) -> None:
    """免除は宣言だけでなく、release policy の検証が通ることを確認する。

    先だけを持つ calendar の最古の行は今日より後ろにある。2026-08-17 の日次バッチは
    `coverage starts 2026-07-03, later than the profile boundary 2026-06-19` で止まり、
    その日の L1 release が 3 回とも publish されなかった。境界を持つ dataset では同じ
    manifest が今も拒まれることを併せて固定し、通ったのが検査の不在ではないことを示す。
    """

    calendar = next(
        item
        for item in lake_models.PRODUCTION_RELEASE_POLICY.datasets
        if item.dataset == "jpx.earnings_calendar"
    )
    # Keep the production history behavior while narrowing only the fixture's datasets.
    narrow_release_policy(monkeypatch, datasets=("jpx.earnings_calendar",), carries_history=None)
    payload = _dataset_payload(
        dataset="jpx.earnings_calendar",
        coverage_start="2026-07-03",
        population_count=rows,
        rows=rows,
    )
    # 決算 calendar は年で切る。contract version が partition の形を固定しているので、
    # 既定の year+month のままでは manifest 自体が読めない。
    payload["partition_by"] = ["year"]
    partitions = payload["partitions"]
    assert isinstance(partitions, list)
    partitions[0]["values"] = {"year": 2026}
    objects = partitions[0]["objects"]
    assert isinstance(objects, list)
    objects[0]["key"] = canonical_object_key(
        layer="l2_processed",
        dataset="jpx.earnings_calendar",
        contract_version=1,
        partition_values={"year": 2026},
        content_sha256="a" * 64,
    )
    manifest = _load_dataset(payload)
    manifests = {manifest.dataset: manifest}
    release = load_lake_model_json(
        json.dumps(
            {
                "manifest_version": 2,
                "release_id": "release-calendar",
                "profile": "production",
                "created_at": "2026-08-13T00:00:00Z",
                "data_as_of": "2026-08-12",
                "datasets": {
                    manifest.dataset: {
                        "build_id": manifest.build_id,
                        "layer": manifest.layer,
                        "contract_version": manifest.contract_version,
                        "manifest_sha256": hashlib.sha256(
                            canonical_lake_model_bytes(manifest)
                        ).hexdigest(),
                        "data_as_of": manifest.data_as_of.isoformat(),
                        "coverage_status": manifest.coverage_status,
                        "totals": manifest.totals.model_dump(mode="json"),
                    }
                },
            }
        ),
        ReleaseManifest,
    )
    validate_release_policy(release, manifests)

    # Not the shared narrowing: this half turns the dataset under test into one that
    # accumulates, which is the state the floor applies to.
    monkeypatch.setattr(
        lake_models,
        "PRODUCTION_RELEASE_POLICY",
        lake_models.PRODUCTION_RELEASE_POLICY.model_copy(
            update={"datasets": (calendar.model_copy(update={"carries_history": True}),)}
        ),
    )
    with pytest.raises(ValueError, match="the serving release already covers"):
        validate_release_policy(
            release,
            manifests,
            published_coverage_start={"jpx.earnings_calendar": date(2026, 6, 19)},
        )


@pytest.mark.parametrize("carries_history", [True, False])
@pytest.mark.parametrize(
    "dataset", ["tradingview.forecast_snapshots", "edinet.tender_offer_exit_values"]
)
def test_optional_published_history_cannot_disappear(monkeypatch, carries_history, dataset):
    narrow_release_policy(
        monkeypatch, datasets=("jquants.daily_bars", dataset), carries_history=carries_history
    )
    policy = lake_models.PRODUCTION_RELEASE_POLICY
    assert not next(item for item in policy.datasets if item.dataset == dataset).required
    bars = _load_dataset(_dataset_payload())
    tv = _load_dataset(_dataset_payload(dataset=dataset))
    payload = _release_payload()
    payload["datasets"]["jquants.daily_bars"]["manifest_sha256"] = hashlib.sha256(
        canonical_lake_model_bytes(bars)
    ).hexdigest()
    without_tv = load_lake_model_json(json.dumps(payload), ReleaseManifest)
    validate_release_policy(without_tv, {bars.dataset: bars})
    previous = {dataset: date(2026, 8, 1)}
    if carries_history:
        with pytest.raises(
            ValueError, match="historical dataset served previously cannot disappear"
        ):
            validate_release_policy(
                without_tv, {bars.dataset: bars}, published_coverage_start=previous
            )
    else:
        validate_release_policy(without_tv, {bars.dataset: bars}, published_coverage_start=previous)
    payload["datasets"][dataset] = {
        "build_id": tv.build_id,
        "layer": tv.layer,
        "contract_version": tv.contract_version,
        "data_as_of": tv.data_as_of.isoformat(),
        "coverage_status": tv.coverage_status,
        "totals": tv.totals.model_dump(mode="json"),
        "manifest_sha256": hashlib.sha256(canonical_lake_model_bytes(tv)).hexdigest(),
    }
    with_tv = load_lake_model_json(json.dumps(payload), ReleaseManifest)
    validate_release_policy(
        with_tv, {bars.dataset: bars, dataset: tv}, published_coverage_start=previous
    )


def test_release_requires_the_current_dataset_contract(monkeypatch) -> None:
    narrow_release_policy(monkeypatch, datasets=("jquants.daily_bars",))
    manifest = _load_dataset(_dataset_payload())
    payload = _release_payload()
    datasets = payload["datasets"]
    assert isinstance(datasets, dict)
    datasets[manifest.dataset]["manifest_sha256"] = hashlib.sha256(
        canonical_lake_model_bytes(manifest)
    ).hexdigest()
    release = load_lake_model_json(json.dumps(payload), ReleaseManifest)
    validate_release_policy(release, {manifest.dataset: manifest})

    other_version = manifest.model_copy(update={"contract_version": manifest.contract_version + 1})
    with pytest.raises(ValueError, match="does not match current contract"):
        validate_release_policy(release, {manifest.dataset: other_version})


@pytest.mark.parametrize(
    ("legacy_dataset", "old_version"),
    [
        ("jquants.master_snapshots", 2),
        ("jquants.fin_summaries", 4),
    ],
)
def test_mixed_jquants_release_rejects_previous_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy_dataset: str, old_version: int
) -> None:
    names = ("jquants.master_snapshots", "jquants.fin_summaries")
    narrow_release_policy(monkeypatch, datasets=names)
    manifests = {name: _load_dataset(_dataset_payload(dataset=name)) for name in names}

    def release_for(items: dict[str, DatasetManifest]) -> ReleaseManifest:
        payload = _release_payload()
        payload["datasets"] = {
            name: {
                "build_id": manifest.build_id,
                "layer": manifest.layer,
                "contract_version": manifest.contract_version,
                "manifest_sha256": hashlib.sha256(canonical_lake_model_bytes(manifest)).hexdigest(),
                "data_as_of": manifest.data_as_of.isoformat(),
                "coverage_status": manifest.coverage_status,
                "totals": manifest.totals.model_dump(mode="json"),
            }
            for name, manifest in items.items()
        }
        return load_lake_model_json(json.dumps(payload), ReleaseManifest)

    validate_release_policy(release_for(manifests), manifests)
    old_payload = _dataset_payload(dataset=legacy_dataset)
    old_payload["contract_version"] = old_version
    partitions = old_payload["partitions"]
    assert isinstance(partitions, list)
    partition = partitions[0]
    assert isinstance(partition, dict)
    objects = partition["objects"]
    assert isinstance(objects, list)
    lake_object = objects[0]
    assert isinstance(lake_object, dict)
    assert isinstance(partition["values"], dict)
    assert isinstance(lake_object["sha256"], str)
    lake_object["key"] = canonical_object_key(
        layer="l2_processed",
        dataset=legacy_dataset,
        contract_version=old_version,
        partition_values=partition["values"],
        partition_by=LAKE_DATASETS[legacy_dataset].partition_by,
        content_sha256=lake_object["sha256"],
    )
    mixed = {**manifests, legacy_dataset: _load_dataset(old_payload)}
    with pytest.raises(ValueError, match="does not match current contract"):
        validate_release_policy(release_for(mixed), mixed)
    release = release_for(mixed)
    for name, manifest in mixed.items():
        path = tmp_path / dataset_manifest_key(dataset=name, build_id=manifest.build_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical_lake_model_bytes(manifest))
    release_bytes = canonical_lake_model_bytes(release)
    release_path = tmp_path / release_manifest_key(release_id=release.release_id)
    release_path.parent.mkdir(parents=True, exist_ok=True)
    release_path.write_bytes(release_bytes)
    fixed = resolve_release(
        LocalMirrorSource(tmp_path),
        release.release_id,
        manifest_sha256=hashlib.sha256(release_bytes).hexdigest(),
    )
    with pytest.raises(
        LakeReadError,
        match=f"this reader accepts only v{LAKE_DATASETS[legacy_dataset].contract_version}",
    ):
        accepted_dataset(fixed, legacy_dataset)


def test_recomputed_exit_proxies_do_not_require_historical_conclusions() -> None:
    policy = next(
        item
        for item in lake_models.PRODUCTION_RELEASE_POLICY.datasets
        if item.dataset == "edinet.tender_offer_exit_values"
    )
    assert not policy.required
    assert not policy.carries_history
