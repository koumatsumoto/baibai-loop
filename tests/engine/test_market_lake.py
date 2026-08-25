from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from tests.helpers.l1_release import stored_release_source
from tests.helpers.lake_policy import narrow_release_policy

from baibai_engine.cli import DOMAINS
from baibai_engine.market.lake import models as lake_models
from baibai_engine.market.lake.cli import main as lake_main
from baibai_engine.market.lake.inventory import inventory
from baibai_engine.market.lake.keys import (
    canonical_object_key,
    current_l1_pointer_key,
    dataset_manifest_key,
    release_manifest_key,
)
from baibai_engine.market.lake.models import (
    MAX_LAKE_JSON_BYTES,
    DatasetManifest,
    PartitionManifest,
    ReleaseManifest,
    canonical_lake_model_bytes,
    load_lake_model_json,
    load_manifest_json,
    validate_release_policy,
)
from baibai_engine.market.lake.sources import resolve_source_ref


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
                "sources": [_snapshot_source_payload()],
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
        "profile": "production",
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


def test_release_manifest_is_strict_and_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    narrow_release_policy(monkeypatch)
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


@pytest.mark.parametrize("field", ["coverage_start"])
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
        "manifest_version": 1,
        "release_id": "release-1",
        "profile": "production",
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
        "manifest_version": 1,
        "release_id": "release-1",
        "profile": "production",
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

    validate_release_policy(release, manifests)

    # A dataset with no population to count reports none, and the model accepts that
    # because it cannot tell "inapplicable" from "omitted". The floor is what refuses
    # it: a profile that measures a population against a dataset that does not report
    # one has to stop rather than skip the check it was configured to make.
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


def test_pilot_policy_rejects_a_fresh_one_day_population(monkeypatch: pytest.MonkeyPatch) -> None:
    narrow_release_policy(monkeypatch, relax_floors=False)
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
            "profile": "production",
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

    # A store holding one day is refused on the size of what it holds. The history
    # question needs something already published to compare against, so it is asked
    # separately below rather than inferred from a date written into the policy.
    with pytest.raises(ValueError, match="below the profile floor"):
        validate_release_policy(
            release,
            manifests,
        )

    with pytest.raises(ValueError, match="the serving release already covers"):
        validate_release_policy(
            release,
            manifests,
            published_coverage_start={"jquants.daily_bars": date(2016, 8, 1)},
        )


def test_a_retained_reference_resolves_only_to_its_own_immutable_bytes(tmp_path: Path) -> None:
    """`L1ReleaseSourceRef` is the only kind that names a key, so it is the only kind
    this resolution has to answer for — and it answers by the digest, not by the name."""

    path, reference = stored_release_source(tmp_path)

    assert resolve_source_ref(tmp_path, reference) == path

    with pytest.raises(ValueError, match="digest does not match"):
        resolve_source_ref(tmp_path, reference.model_copy(update={"sha256": "0" * 64}))
    path.unlink()
    with pytest.raises(ValueError, match="does not resolve inside the lake mirror"):
        resolve_source_ref(tmp_path, reference)


def test_a_release_whose_closure_is_incomplete_does_not_resolve(tmp_path: Path) -> None:
    """The manifest is the root of a graph, not the graph. A reference that resolved on
    the root alone would claim a readable fixed release while its rows are absent."""

    _, reference = stored_release_source(tmp_path)
    objects = sorted((tmp_path / "lake/l1/canonical").rglob("*.parquet"))
    assert objects, "the fixture must publish at least one object to remove"
    objects[0].unlink()

    with pytest.raises(ValueError, match="does not resolve inside the lake mirror"):
        resolve_source_ref(tmp_path, reference)


def test_a_release_whose_object_bytes_changed_does_not_resolve(tmp_path: Path) -> None:
    """Presence is not the claim; the digest the manifest published is."""

    _, reference = stored_release_source(tmp_path)
    objects = sorted((tmp_path / "lake/l1/canonical").rglob("*.parquet"))
    objects[0].write_bytes(b"different-bytes-same-name")

    with pytest.raises(ValueError, match="digest does not match"):
        resolve_source_ref(tmp_path, reference)


def test_a_release_whose_dataset_manifest_was_replaced_does_not_resolve(tmp_path: Path) -> None:
    """The release pins each dataset manifest by digest, so republishing that key under
    the same build id is what the digest is there to refuse."""

    _, reference = stored_release_source(tmp_path)
    manifests = sorted((tmp_path / "lake/manifests/datasets").rglob("*.json"))
    manifests[0].write_text('{"manifest_version": 1}', encoding="utf-8")

    with pytest.raises(ValueError, match="digest does not match"):
        resolve_source_ref(tmp_path, reference)


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
            "prefix": "lake/l1/canonical/jquants.daily_bars",
            "objects": 1,
            "bytes": 7,
        }
    ]
    assert result["invalid_keys"] == []


def test_inventory_reports_an_unsafe_key_inside_the_namespace(tmp_path: Path) -> None:
    """Narrowing the walk to the namespace must not stop it reporting a bad key in it."""

    stray = tmp_path / "lake" / "l1" / "canonical" / "jquants daily bars" / "part.parquet"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"parquet")

    result = inventory(tmp_path)

    assert result["objects"] == 1
    assert result["invalid_keys"] == ["lake/l1/canonical/jquants daily bars/part.parquet"]
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
    assert DOMAINS["lake"].module == "baibai_engine.market.lake.cli"


def test_inventory_reports_every_class_against_its_own_budget(tmp_path: Path) -> None:
    """A class that grows for a design reason has to be visible against its objective.

    Reporting one figure would hide a workspace holding a sealed store the size of the
    whole legacy database behind the published graph's own objective. Workspace bytes
    are under no manifest, so nothing else in this report grows when they do.
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

    assert set(capacity) == {"published", "workspace"}
    assert capacity["published"]["bytes"] == 7
    assert capacity["workspace"]["bytes"] == 26
    assert capacity["published"]["soft_budget_bytes"] == 10 * 1024**3
    assert not any(item["budget_exceeded"] for item in capacity.values())


def test_every_lake_dataset_states_a_release_policy() -> None:
    """policy を持たない dataset は、床も鮮度も無いまま production release へ入る。

    `ReleaseDatasetPolicy` は release が dataset を受け入れる条件 — 最小行数・履歴の床・
    公表からの経過 — を述べる唯一の場所で、名前が挙がっていない dataset には何も課されない。
    lake 側の定義だけを足して policy を忘れると、その dataset は「検査を通った」ではなく
    「検査の対象ですらない」状態で publish される。どの build log にも現れない差である。

    `required` は dataset ごとに違ってよい。公表制度の開始を待つ `all_issues_daily_margin`
    は False である。ここが要求するのは policy が存在することだけである。
    """

    from baibai_engine.market.lake.datasets import LAKE_DATASETS
    from baibai_engine.market.lake.models import PRODUCTION_RELEASE_POLICY

    stated = {item.dataset for item in PRODUCTION_RELEASE_POLICY.datasets}

    assert stated == set(LAKE_DATASETS)


def test_a_replaced_snapshot_carries_no_history_floor() -> None:
    """置き換わる view に開始日を固定すると、source が約束していない履歴を課す。

    `jquants.earnings_calendar` の SQLite table は fetch ごとに DELETE されて入れ直る
    forward calendar なので、最古の行は取引所がまだ公表している範囲そのものである。
    2026-08-17 の日次バッチは、snapshot が 2026-06-19 始まりから 2026-07-03 始まりへ
    進んだだけで停止した。
    """

    from baibai_engine.market.lake.models import PRODUCTION_RELEASE_POLICY

    policy = {item.dataset: item for item in PRODUCTION_RELEASE_POLICY.datasets}

    assert policy["jquants.earnings_calendar"].carries_history is False
    # 蓄積する dataset は床を持ち続ける。免除は snapshot に限る。
    assert policy["jquants.daily_bars"].carries_history is True
    assert policy["jquants.short_sale_reports"].carries_history is True


def test_only_a_replaced_table_is_exempt_from_the_history_floor() -> None:
    """免除の判定基準は「fetch が table を丸ごと置き換えるか」で、code から決まる。

    範囲指定なしの `DELETE FROM <table>` はその table を snapshot にする — 行は蓄積せず、
    最古の行も件数も source が今公表している範囲そのものになる。日付で絞った delete は
    冪等な upsert で、行は蓄積するので 観測の 0.95 倍という床は時間とともに余裕が広がる。

    将来 snapshot 型の dataset が増えたとき、履歴の床を付けたまま入ると年に一度止まる。
    """

    import re

    from baibai_engine.market.lake.datasets import LAKE_DATASETS
    from baibai_engine.market.lake.models import PRODUCTION_RELEASE_POLICY

    # The whole screening package, not just its cache: the writers of the two
    # operator-derived datasets live beside it, and a scan bounded by directory would
    # call their wholesale replacement an accumulation.
    screening = Path(__file__).resolve().parents[2] / "engine/src/baibai_engine/screening"
    replaced = {
        match.group(1)
        for path in screening.rglob("*.py")
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
        "jquants.earnings_calendar",
    }
    assert exempt == snapshot_datasets


def test_the_seasonal_calendar_floor_clears_its_measured_trough() -> None:
    """季節性を持つ量の床は、観測した谷の下に無ければならない。

    先 68 日窓 — この snapshot の幅 — に決算を announce する社数は、実開示 2023-08 以降の
    週次 149 標本で 978〜4,699・中央 3,985 と 5 倍近く動く。谷は 3 年連続で 11 月中旬に
    来る。旧床 3,232 は一度の観測 3,403 の 0.95 倍で、 32% の窓を割っており、健全な publish
    を毎秋拒否していた。床が守るのは「calendar の一部しか返さなかった fetch」なので、
    観測最小の半分に置く。
    """

    from baibai_engine.market.lake.models import PRODUCTION_RELEASE_POLICY

    policy = {item.dataset: item for item in PRODUCTION_RELEASE_POLICY.datasets}
    calendar = policy["jquants.earnings_calendar"]
    measured_trough = 978

    assert calendar.minimum_rows < measured_trough
    assert calendar.minimum_population_count is not None
    assert calendar.minimum_population_count < measured_trough
    # 空の fetch を通してしまう床では意味がない。
    assert calendar.minimum_rows > measured_trough // 4


@pytest.mark.parametrize(("rows", "accepted"), [(1_100, True), (400, False)])
def test_a_forward_only_calendar_uses_its_policy_floor_not_the_previous_snapshot(
    monkeypatch: pytest.MonkeyPatch, rows: int, accepted: bool
) -> None:
    """免除は宣言だけでなく、publish が実際に通るところまで成立していなければならない。

    先だけを持つ calendar の最古の行は今日より後ろにある。2026-08-17 の日次バッチは
    `coverage starts 2026-07-03, later than the profile boundary 2026-06-19` で止まり、
    その日の L1 release が 3 回とも publish されなかった。境界を持つ dataset では同じ
    manifest が今も拒まれることを併せて固定し、通ったのが検査の不在ではないことを示す。
    """

    calendar = next(
        item
        for item in lake_models.PRODUCTION_RELEASE_POLICY.datasets
        if item.dataset == "jquants.earnings_calendar"
    )
    # The floors are what this test asserts against, so they are not relaxed.
    narrow_release_policy(monkeypatch, datasets=("jquants.earnings_calendar",), relax_floors=False)
    payload = _dataset_payload(
        dataset="jquants.earnings_calendar",
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
        layer="l1_canonical",
        dataset="jquants.earnings_calendar",
        contract_version=1,
        partition_values={"year": 2026},
        content_sha256="a" * 64,
    )
    manifest = _load_dataset(payload)
    manifests = {manifest.dataset: manifest}
    release = load_lake_model_json(
        json.dumps(
            {
                "manifest_version": 1,
                "release_id": "release-calendar",
                "profile": "production",
                "created_at": "2026-08-13T00:00:00Z",
                "data_as_of": "2026-08-12",
                "datasets": {
                    manifest.dataset: {
                        "build_id": manifest.build_id,
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
    if not accepted:
        with pytest.raises(ValueError, match="below the profile floor"):
            validate_release_policy(release, manifests)
        return

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
            published_coverage_start={"jquants.earnings_calendar": date(2026, 6, 19)},
        )
