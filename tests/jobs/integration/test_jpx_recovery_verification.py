"""Recovery compares complete saved bytes and only accepts real projection clocks."""

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.lake_release import market_store
from tests.helpers.model_generation import empty_selection, generation, publish_generation
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_engine.storage.snapshots.release import create_release
from baibai_engine.storage.snapshots.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.sqlite.source_cache.schema import SQLiteSchemaError
from baibai_jobs.integration import jpx_recovery_verification as verification
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.policy import release_policy_for_profile
from baibai_jobs.publication.snapshot_release import publish_release

STAMP = datetime(2026, 10, 25, tzinfo=UTC)


def test_fingerprints_preserve_raw_bytes_duplicate_rows_and_not_row_order():
    rows = [("a", b"raw\x00", None), ("a", b"raw\x00", None), ("b", b"other", 1)]
    expected = verification.row_fingerprint(rows)
    assert expected["count"] == 3
    assert expected == verification.row_fingerprint(reversed(rows))
    assert expected != verification.row_fingerprint(rows[1:])
    assert expected != verification.row_fingerprint([*rows[:2], ("b", b"other!", 1)])
    encoded = ["a", {"bytes_sha256": hashlib.sha256(b"raw\x00").hexdigest(), "size": 4}, None]
    row_hash = hashlib.sha256(
        json.dumps(encoded, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    assert (
        verification.row_fingerprint([rows[0]])["sha256"]
        == hashlib.sha256((row_hash + "\n").encode()).hexdigest()
    )


def test_market_snapshot_covers_exact_owner_tables_and_refuses_old_schema(tmp_path):
    database = market_store(tmp_path / "market.sqlite")
    snapshot = verification.snapshot_market(database)
    assert len(snapshot) == 12
    assert len(verification.EDINET_DATASETS) == 6
    assert snapshot.keys() == verification.MARKET_DATASETS.keys()
    assert verification.coverage(database, "edinet")["count"] == 0
    assert verification.edinet_coverage_rows(database) == []
    with pytest.raises(ValueError, match="unsupported"):
        verification.coverage(database, "sql")
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version=50")
    with pytest.raises(SQLiteSchemaError, match="schema"):
        verification.snapshot_market(database)


@pytest.mark.parametrize(
    "change", ["removed", "added", "semantic", "old_clock", "future_clock", "duplicate"]
)
def test_edinet_coverage_rejects_semantic_loss_and_fabricated_clocks(change):
    before = [{"source": "edinet_metrics", "fetched_at_utc": STAMP.isoformat(), "count": 3}]
    after = [{**before[0], "fetched_at_utc": (STAMP + timedelta(seconds=2)).isoformat()}]
    if change == "removed":
        after.clear()
    elif change == "added":
        after.append({**after[0], "source": "edinet_extra"})
    elif change == "semantic":
        after[0]["count"] = 2
    elif change == "old_clock":
        after[0]["fetched_at_utc"] = (STAMP - timedelta(seconds=1)).isoformat()
    elif change == "future_clock":
        after[0]["fetched_at_utc"] = (STAMP + timedelta(seconds=5)).isoformat()
    else:
        after.append(after[0])
    with pytest.raises(ValueError, match=r"coverage|clock|generation|membership|inputs"):
        verification.verify_edinet_coverage(
            before,
            after,
            started_at=STAMP + timedelta(seconds=1),
            finished_at=STAMP + timedelta(seconds=3),
        )


def test_edinet_coverage_allows_only_actual_forward_projection_clock():
    before = [{"source": "edinet_metrics", "fetched_at_utc": STAMP.isoformat(), "count": 3}]
    assert (
        verification.verify_edinet_coverage(before, before, started_at=STAMP, finished_at=STAMP)
        == 0
    )
    verification.verify_edinet_coverage(
        before,
        [{**before[0], "fetched_at_utc": (STAMP + timedelta(seconds=1)).isoformat()}],
        started_at=STAMP,
        finished_at=STAMP + timedelta(seconds=2),
    )


@pytest.fixture
def model_case(tmp_path, monkeypatch):
    path = tmp_path / "runs.sqlite"
    for index in range(5):
        identity = f"old-{index}"
        publish_generation(generation().model_copy(update={"generation_id": identity}), path=path)
        ScreeningSelectionStore(path).publish(f"selection-{index}", empty_selection(identity))
    prior = verification.prior_generations(path)
    edinet = InputSnapshot(
        kind="native_l1",
        reference="edinet-fixed",
        sha256=hashlib.sha256(b"{}").hexdigest(),
        descriptor={},
    )
    fresh = generation().model_copy(
        update={
            "generation_id": "models-new",
            "inputs": {
                "market": InputSnapshot(kind="lake_release", reference="new", sha256="b" * 64),
                "edinet": edinet,
            },
        }
    )
    publish_generation(fresh, path=path)
    ScreeningSelectionStore(path).publish("new-selection", empty_selection(fresh.generation_id))
    notice = {
        "generation_id": fresh.generation_id,
        "selection_id": "new-selection",
        "model_count": len(fresh.models),
        "asof": fresh.as_of.isoformat(),
        "lake": {"release_id": "new", "release_manifest_sha256": "b" * 64},
    }
    monkeypatch.setattr(verification, "_native_inputs", lambda *_: {"checked": True})
    return path, prior, notice, edinet.model_dump(mode="json")


def test_all_prior_generations_and_selections_must_remain_byte_equal(model_case):
    path, prior, notice, edinet = model_case
    assert len(prior["generation_ids"]) == 5
    result = verification.verify_models(path, prior, notice, edinet, STAMP, STAMP, MemoryR2Store())
    assert result["tables"]["generated_model"]["count"] == 3
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM screening_selection WHERE generation_id='old-0'")
    with pytest.raises(ValueError, match="prior"):
        verification.verify_models(path, prior, notice, edinet, STAMP, STAMP, MemoryR2Store())


@pytest.mark.parametrize(
    "change", ["extra_generation", "clock", "market", "edinet", "model_count", "selection"]
)
def test_new_model_acceptance_is_bound_to_notice_fixed_inputs_and_clock(model_case, change):
    path, prior, notice, edinet = model_case
    start, end = STAMP, STAMP
    if change == "extra_generation":
        publish_generation(
            generation().model_copy(update={"generation_id": "unrelated"}), path=path
        )
    elif change == "clock":
        start = end = STAMP + timedelta(seconds=1)
    elif change == "market":
        notice["lake"]["release_manifest_sha256"] = "c" * 64
    elif change == "edinet":
        edinet["sha256"] = "d" * 64
    elif change == "model_count":
        notice["model_count"] += 1
    else:
        notice["selection_id"] = "other"
    with pytest.raises(ValueError, match=r"coverage|clock|generation|membership|inputs"):
        verification.verify_models(path, prior, notice, edinet, start, end, MemoryR2Store())


def test_native_inputs_read_every_fixed_root_and_keep_credit_interval_semantics(monkeypatch):
    inputs = {}
    for name in verification._NATIVE_INPUTS:
        owner = verification.importlib.import_module(
            "baibai_engine.machine_analysis.generation." + name + "_inputs"
        )
        retained = name in {"margin_alerts", "margin_interest", "short_sale_reports"}
        day = "2026-10-26" if retained else "2026-10-23"
        descriptor = {
            "format": owner.INPUT_FORMAT,
            "head_sha256": "a" * 64,
            "selection_sha256": "b" * 64,
            "as_of": day,
            "summary": {
                "selection_start": "2026-10-01",
                "selection_end": day,
                "evaluated_at": STAMP.isoformat(),
            },
        }
        inputs[name] = InputSnapshot(
            kind="native_l1",
            reference=name,
            sha256=hashlib.sha256(canonical_json(descriptor).encode()).hexdigest(),
            descriptor=descriptor,
        )
    calls = []

    def interval(store, descriptor, *, contract):
        calls.append(contract)
        return date(2026, 10, 1), date.fromisoformat(descriptor["as_of"])

    monkeypatch.setattr(verification, "full_day_input_interval", interval)
    metadata = SimpleNamespace(inputs=inputs, as_of=date(2026, 10, 23), evaluation_time=STAMP)
    result = verification._native_inputs(metadata, MemoryR2Store())
    assert len(calls) == len(result) == 7
    assert result["margin_interest"]["end"] == "2026-10-26"
    descriptor = inputs["master"].descriptor
    descriptor["summary"]["snapshot_date"] = "2026-10-24"
    inputs["master"] = InputSnapshot(
        kind="native_l1",
        reference="master",
        sha256=hashlib.sha256(canonical_json(descriptor).encode()).hexdigest(),
        descriptor=descriptor,
    )
    with pytest.raises(ValueError, match="future master"):
        verification._native_inputs(metadata, MemoryR2Store())


@pytest.fixture
def published_lake(tmp_path, monkeypatch):
    names = ("jquants.daily_bars", *verification.MARKET_DATASETS)
    narrow_release_policy(monkeypatch, datasets=names, carries_history=None)
    store = MemoryR2Store()
    database = market_store(tmp_path / "market.sqlite")
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        for dataset in verification.MARKET_DATASETS.values():
            values = []
            for column in dataset.columns:
                value = {"TEXT": "value", "INTEGER": 1, "REAL": 1.0, "BLOB": b"original"}[
                    column.sqlite_type
                ]
                if column.name == dataset.date_column:
                    value = "2026-10-23"
                elif column.name == "availability":
                    value = "available"
                values.append(value)
            connection.execute(
                f"INSERT INTO {dataset.sqlite_table} ({','.join(column.name for column in dataset.columns)}) "
                f"VALUES ({','.join('?' for _ in values)})",
                values,
            )
            connection.execute(
                "INSERT OR REPLACE INTO source_coverage VALUES (?,?,?,?,?,?,?,?)",
                (
                    dataset.coverage_source_name,
                    "fixture",
                    "2026-10-23",
                    "2026-10-23",
                    STAMP.isoformat(),
                    1,
                    "ok",
                    None,
                ),
            )
    mirror = tmp_path / "publication"
    with sealed_sqlite_snapshot(sqlite_path=database, mirror_root=mirror) as snapshot:
        manifests = [
            export_dataset_snapshot(
                dataset=LAKE_DATASETS[name],
                mirror_root=mirror,
                source_snapshot=snapshot,
                producer_git_commit="a" * 40,
            ).manifest_path
            for name in names
        ]
    manifest, _ = create_release(
        dataset_manifest_paths=manifests,
        mirror_root=mirror,
        release_id="recovered",
        catalog=LAKE_DATASETS,
        policy=release_policy_for_profile("production"),
    )
    publish_release(mirror_root=mirror, release_manifest_path=manifest, store=store)
    expected = {
        "lake_release_id": "recovered",
        "lake_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
    }
    return store, expected, verification.snapshot_market(database)


def test_lake_readback_fetches_fresh_bytes_and_compares_all_tables(tmp_path, published_lake):
    store, generation, tables = published_lake
    assert verification.verify_lake(store, tmp_path / "readback", generation, tables) == tables
    with pytest.raises(FileExistsError):
        verification.verify_lake(store, tmp_path / "readback", generation, tables)
    tables["jpx.source_bundles"] = {"count": 1, "sha256": "0" * 64}
    with pytest.raises(ValueError, match="values differ"):
        verification.verify_lake(store, tmp_path / "bad-values", generation, tables)
    with pytest.raises(ValueError, match="current differs"):
        verification.verify_lake(
            store,
            tmp_path / "bad-current",
            {**generation, "lake_manifest_sha256": "f" * 64},
            tables,
        )


def test_lake_readback_rejects_corrupt_raw_object_bytes(tmp_path, published_lake):
    store, generation, tables = published_lake
    key = next(
        key for key in store.values if "jpx.source_responses" in key and key.endswith(".parquet")
    )
    store.store_object(key, b"corrupted original bytes")
    writes_before = list(store.put_keys)
    with pytest.raises(LakePublishError, match="digest mismatch"):
        verification.verify_lake(store, tmp_path / "corrupt-readback", generation, tables)
    assert store.put_keys == writes_before


def test_prior_generation_missing_or_extra_selection_cannot_be_hidden(model_case):
    path, prior, notice, edinet = model_case
    ScreeningSelectionStore(path).publish(
        "extra-new-selection", empty_selection(notice["generation_id"])
    )
    with pytest.raises(ValueError, match="Selection membership"):
        verification.verify_models(path, prior, notice, edinet, STAMP, STAMP, MemoryR2Store())
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM generated_model WHERE generation_id='old-0'")
    with pytest.raises(ValueError, match="prior"):
        verification.verify_models(path, prior, notice, edinet, STAMP, STAMP, MemoryR2Store())


def test_lake_readback_detects_pointer_change_after_full_verification(
    tmp_path, monkeypatch, published_lake
):
    store, generation, tables = published_lake
    original = verification._serving_pointer
    reads = 0

    def changing_pointer(store):
        nonlocal reads
        reads += 1
        return original(store) if reads == 1 else SimpleNamespace(pointer=None)

    monkeypatch.setattr(verification, "_serving_pointer", changing_pointer)
    with pytest.raises(ValueError, match="current changed"):
        verification.verify_lake(store, tmp_path / "changing-readback", generation, tables)
