"""Both full and planning inputs use the same acquisition and recovery policy."""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.r2_store import MemoryR2Store

import baibai_engine.data_collection.acquisition.refresh as acquisition
from baibai_engine.data_collection.acquisition.policy import (
    acquisition_groups,
    explicit_recovery_ranges,
    recent_start,
    recovery_ranges,
)
from baibai_engine.data_collection.catalog import load_sources, require_source
from baibai_engine.data_collection.receipts.native_sources import store
from baibai_engine.data_collection.receipts.native_sources.documents import (
    PMI_INPUT_TABLE,
    SourceDocument,
    read_document_metadata,
    read_documents,
    store_document,
)
from baibai_engine.data_collection.receipts.native_sources.handoff import export_receipts
from baibai_engine.data_collection.receipts.native_sources.records import store_snapshot
from baibai_jobs.acquisition.economic_staging import load_handoff, save_handoff
from baibai_jobs.acquisition.source_inputs import input_key, load_input, publish_inputs
from baibai_jobs.integration.economic_handoff import acquire

DAY = date(2026, 11, 6)
WHEN = datetime(2026, 11, 6, 1, tzinfo=UTC)
CASES = (
    ("economic-spglobal_pmi", "spglobal-jp-pmi-mfg", "pmi-acquisition-input-v1", PMI_INPUT_TABLE),
    (
        "economic-umich_sca",
        "umich-sca-ics",
        "umich-acquisition-input-v1",
        "economic_umich_input_days",
    ),
    ("economic-ecb_fx", "ecb-fx", "ecb-fx-acquisition-input-v1", "economic_ecb_input_days"),
    (
        "economic-mof_jgb",
        "mof-jgbcm-all",
        "mof-jgb-acquisition-input-v1",
        "economic_mof_jgb_input_days",
    ),
    (
        "economic-frb_h15",
        "fed-h15",
        "frb-h15-acquisition-input-v1",
        "economic_frb_h15_input_days",
    ),
    (
        "economic-multpl",
        "multpl-shiller-pe",
        "multpl-acquisition-input-v1",
        "economic_multpl_shiller_pe_input_days",
    ),
    (
        "economic-yahoo",
        "yahoo-gc-f",
        "yahoo-acquisition-input-v1",
        "economic_yahoo_gc_f_input_days",
    ),
)


def _document(source, month, when=WHEN, *, phase="final"):
    return SourceDocument(
        source_id=source,
        request_key=month.isoformat(),
        source_url=require_source(source).source_url,
        content_type="application/pdf",
        fetched_at=when,
        payload=b"synthetic original PDF",
        metadata={"observed_at": month.isoformat(), "phase": phase},
    )


def _seed(path, source):
    first = recent_start(require_source(source), DAY) - timedelta(days=20)
    with closing(store.initialize_database(path)) as connection, connection:
        for start, end, status in (
            (first, first + timedelta(days=3), "ok"),
            (first + timedelta(days=4), first + timedelta(days=12), "failed"),
            (first - timedelta(days=900), first - timedelta(days=100), "failed"),
        ):
            store.record_source_run(
                connection,
                source_id=source,
                start=start,
                end=end,
                started_at=WHEN + timedelta(seconds=(start - first).days),
                status=status,
                record_count=0,
            )
        if source.startswith("spglobal-"):
            store_document(connection, _document(source, first.replace(day=1)))
        else:
            for offset, records in ((5, []), (8, [{"value": "old original"}])):
                store_snapshot(
                    connection,
                    source_id=source,
                    observed_at=first + timedelta(days=offset),
                    fetched_at=WHEN,
                    records=records,
                )
    return first


def _planning_copy(canonical, target, case):
    """Construct the reviewed v1 wire contract, not a new production exporter."""
    _, source, _, table = case
    export_receipts(canonical, target, source_ids=[], since=WHEN)
    with (
        closing(sqlite3.connect(canonical)) as received,
        closing(sqlite3.connect(target)) as output,
        output,
    ):
        for metadata in ("economic_source_runs", *store.CONFIRMATION_TABLES):
            rows = received.execute(f"SELECT * FROM {metadata} WHERE source_id=?", (source,))
            placeholders = ",".join("?" for _ in rows.description)
            output.executemany(f"INSERT INTO {metadata} VALUES ({placeholders})", rows)
        if source.startswith("spglobal-"):
            output.execute(
                f"CREATE TABLE {table}(source_id TEXT NOT NULL, request_key TEXT NOT NULL, "
                "fetched_at TEXT NOT NULL, source_url TEXT NOT NULL, content_type TEXT NOT NULL, "
                "metadata TEXT NOT NULL, PRIMARY KEY(source_id, request_key))"
            )
            output.executemany(
                f"INSERT INTO {table} VALUES (?,?,?,?,?,?)",
                received.execute(
                    "SELECT source_id,request_key,fetched_at,source_url,content_type,metadata "
                    "FROM economic_source_documents WHERE source_id=?",
                    (source,),
                ),
            )
        else:
            scopes = (
                [
                    (item.source_id, "economic_" + item.source_id.replace("-", "_") + "_input_days")
                    for item in load_sources().values()
                    if item.provider == require_source(source).provider
                ]
                if source.startswith(("yahoo-", "multpl-"))
                else [(source, table)]
            )
            for owner, table in scopes:
                output.execute(f"CREATE TABLE {table}(observed_at TEXT NOT NULL PRIMARY KEY)")
                output.executemany(
                    f"INSERT INTO {table} VALUES (?)",
                    received.execute(
                        "SELECT DISTINCT observed_at FROM economic_source_records WHERE source_id=?",
                        (owner,),
                    ),
                )


def _upload(remote, directory, case, *, planning):
    group, _, receipt_format, _ = case
    generations = {"economic_etag": "fixed-canonical"}
    report = {"group": group, "canonical_generations": generations}
    (directory / "result.json").write_text(json.dumps(report))
    stage = group + ("-planning" if planning else "-original")
    # A legacy wire fixture; production's group-aware writer now emits planning.
    save_handoff(remote, stage, directory, namespace="source-inputs")
    key = f"coordination/source-inputs/{stage}/handoff.json"
    manifest = json.loads(remote.get_bytes(key))
    if planning:
        manifest["planning_key"] = manifest.pop("receipt_key")
        manifest["receipt_format"] = receipt_format
        remote.store_object(key, json.dumps(manifest).encode())
    remote.store_object(
        input_key(group),
        json.dumps({"stage_id": stage, "canonical_generations": generations}).encode(),
    )
    return stage, key, manifest


def _plans(path, source):
    with closing(store.open_read_only_connection(path)) as connection:
        definition = require_source(source)
        return (
            recovery_ranges(connection, definition, DAY),
            explicit_recovery_ranges(connection, definition, DAY),
        )


@pytest.mark.parametrize("case", CASES)
def test_old_new_old_inputs_preserve_plans_and_original_bytes(tmp_path, case):
    group, source, _, _ = case
    canonical = tmp_path / "canonical.sqlite"
    first = _seed(canonical, source)
    before = canonical.read_bytes()
    original = tmp_path / "original"
    original.mkdir()
    export_receipts(
        canonical,
        original / "receipts.sqlite",
        source_ids=[source],
        since=WHEN.min.replace(tzinfo=UTC),
    )
    original_bytes = (original / "receipts.sqlite").read_bytes()
    planning = tmp_path / "planning"
    planning.mkdir()
    _planning_copy(canonical, planning / "receipts.sqlite", case)
    remote = MemoryR2Store()
    for number, (directory, is_planning) in enumerate(
        ((original, False), (planning, True), (original, False))
    ):
        _upload(remote, directory, case, planning=is_planning)
        output = tmp_path / f"loaded-{number}.sqlite"
        load_input(remote, group, output, tmp_path / f"read-{number}")
        assert output.read_bytes() == (directory / "receipts.sqlite").read_bytes()
        assert _plans(output, source) == _plans(canonical, source)
    assert canonical.read_bytes() == before
    # A new real receipt supplements, rather than replaces, fixed-seed coverage.
    for path in (canonical, planning / "receipts.sqlite"):
        with closing(store.open_connection(path)) as connection, connection:
            if source.startswith("spglobal-"):
                store_document(
                    connection,
                    _document(
                        source, first.replace(day=1), WHEN + timedelta(hours=1), phase="flash"
                    ),
                )
            else:
                store_snapshot(
                    connection,
                    source_id=source,
                    observed_at=first + timedelta(days=6),
                    fetched_at=WHEN + timedelta(hours=1),
                    records=[],
                )
    assert _plans(planning / "receipts.sqlite", source) == _plans(canonical, source)
    assert original.joinpath("receipts.sqlite").read_bytes() == original_bytes


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("destination", ["other_group", "handoff", "market", "ambiguous"])
def test_planning_format_cannot_be_read_as_another_scope(tmp_path, case, destination):
    group, source, _, _ = case
    canonical = tmp_path / "canonical.sqlite"
    _seed(canonical, source)
    directory = tmp_path / "planning"
    directory.mkdir()
    _planning_copy(canonical, directory / "receipts.sqlite", case)
    remote = MemoryR2Store()
    stage, key, manifest = _upload(remote, directory, case, planning=True)
    options = {"namespace": "source-inputs", "input_group": group}
    if destination == "other_group":
        options["input_group"] = "economic-fred_csv"
    elif destination == "handoff":
        options["namespace"] = "economic-sources"
        remote.store_object(
            f"coordination/economic-sources/{stage}/handoff.json", json.dumps(manifest).encode()
        )
    elif destination == "market":
        options["market"] = True
    else:
        manifest["receipt_key"] = manifest["planning_key"]
        remote.store_object(key, json.dumps(manifest).encode())
    remote.get_keys.clear()
    with pytest.raises(ValueError, match="planning input"):
        load_handoff(remote, stage, tmp_path / "rejected", **options)
    assert not (tmp_path / "rejected").exists()
    assert not any(key.endswith(".sqlite") for key in remote.get_keys)


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("invalid", ["table", "original"])
def test_planning_input_rejects_incompatible_contents(tmp_path, case, invalid):
    group, source, _, table = case
    canonical = tmp_path / "canonical.sqlite"
    _seed(canonical, source)
    directory = tmp_path / "planning"
    directory.mkdir()
    _planning_copy(canonical, directory / "receipts.sqlite", case)
    with closing(store.open_connection(directory / "receipts.sqlite")) as connection, connection:
        if invalid == "table":
            connection.execute(f"DROP TABLE {table}")
        elif source.startswith("spglobal-"):
            store_document(connection, _document(source, DAY.replace(day=1)))
        else:
            store_snapshot(
                connection, source_id=source, observed_at=DAY, fetched_at=WHEN, records=[]
            )
    remote = MemoryR2Store()
    _upload(remote, directory, case, planning=True)
    with pytest.raises(ValueError, match=r"table|original|bodies"):
        load_input(remote, group, tmp_path / "rejected.sqlite", tmp_path / "download")
    assert not (tmp_path / "rejected.sqlite").exists()


@pytest.mark.parametrize("hours", [-1, 0, 1])
def test_pmi_planning_metadata_yields_to_same_or_newer_real_receipt(tmp_path, hours):
    case = CASES[0]
    source = case[1]
    canonical = tmp_path / "canonical.sqlite"
    _seed(canonical, source)
    target = tmp_path / "planning.sqlite"
    _planning_copy(canonical, target, case)
    with closing(store.open_connection(target)) as connection, connection:
        seed = next(read_document_metadata(connection, source))
        real = replace(
            _document(source, date.fromisoformat(seed.request_key)),
            fetched_at=WHEN + timedelta(hours=hours),
            source_url=seed.source_url + "?corrected",
            metadata={"phase": "flash"},
        )
        store_document(connection, real)
        chosen = next(read_document_metadata(connection, source))
        assert chosen.source_url == (real.source_url if hours >= 0 else seed.source_url)
        assert not hasattr(chosen, "payload")
        assert list(read_documents(connection, source)) == [real]


@pytest.mark.parametrize("case", CASES)
def test_acquisition_exports_only_actual_receipts_from_planning_seed(tmp_path, monkeypatch, case):
    group, source, _, table = case
    canonical = tmp_path / "canonical.sqlite"
    _seed(canonical, source)
    target = tmp_path / "planning.sqlite"
    _planning_copy(canonical, target, case)
    seen = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return (WHEN + timedelta(hours=1)).astimezone(tz or UTC)

    monkeypatch.setattr("baibai_jobs.integration.economic_handoff.datetime", Clock)
    monkeypatch.setattr(acquisition, "datetime", Clock)

    def documents(definition, *, existing, **kwargs):
        seen.extend(existing.values())
        assert all(not hasattr(item, "payload") for item in existing.values())
        yield _document(source, DAY.replace(day=1), WHEN + timedelta(hours=1))

    monkeypatch.setattr(acquisition, "fetch_source_documents", documents)
    monkeypatch.setattr(
        acquisition, "fetch_native_records", lambda *_args, **_kwargs: {DAY: [{"received": "now"}]}
    )
    packet = tmp_path / "packet"
    assert acquire(group.removeprefix("economic-"), source, DAY, packet, target) == 0
    with closing(store.open_read_only_connection(packet / "receipts.sqlite")) as connection:
        assert (
            connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone()
            is None
        )
        original_table = (
            "economic_source_documents"
            if source.startswith("spglobal-")
            else "economic_source_records"
        )
        assert connection.execute(f"SELECT COUNT(*) FROM {original_table}").fetchone()[0] > 0
    if source.startswith("spglobal-"):
        assert seen


def test_only_registered_groups_publish_planning_envelopes(tmp_path):
    canonical = tmp_path / "canonical.sqlite"
    for case in CASES:
        _seed(canonical, case[1])
    before = canonical.read_bytes()
    remote = MemoryR2Store()
    groups = acquisition_groups()
    assert len(groups) == 15
    for group in groups:
        name = "economic-" + group
        report = publish_inputs(
            remote,
            market=tmp_path / "unused",
            economic=canonical,
            identity="reader-only",
            asof=DAY,
            generations={"economic_etag": "fixed"},
            only_group=name,
        )
        assert report == {"updated": [name], "failed": []}
        manifest = json.loads(
            remote.get_bytes(f"coordination/source-inputs/{name}-reader-only/handoff.json")
        )
        planning_formats = {case[0]: case[2] for case in CASES}
        if name in planning_formats:
            assert manifest["receipt_format"] == planning_formats[name]
            assert "planning_key" in manifest
            assert "receipt_key" not in manifest
        else:
            assert "receipt_key" in manifest
            assert "planning_key" not in manifest
            assert "receipt_format" not in manifest
        assert "format" not in manifest
    assert canonical.read_bytes() == before
    assert not any(key.startswith("l1/") for key in remote.put_keys)
