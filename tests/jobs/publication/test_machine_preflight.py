"""The two read admissions (coherent or forward-resume) do not authorize writes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_engine.storage.snapshots.keys import (
    current_release_pointer_key,
    dataset_manifest_key,
    release_manifest_key,
)
from baibai_engine.storage.snapshots.models import DatasetManifest, canonical_lake_model_bytes
from baibai_engine.storage.snapshots.reader import (
    LakeReadError,
    accepted_dataset,
    resolve_release,
)
from baibai_engine.storage.snapshots.release import LakeReleasePointer, create_release
from baibai_engine.storage.snapshots.sources import sha256_file
from baibai_engine.storage.snapshots.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    LakeStoreOrigin,
    write_lake_store_origin,
)
from baibai_engine.storage.sqlite.source_cache.schema import (
    SQLITE_SCHEMA_VERSION,
    SQLiteSchemaError,
    open_connection,
)
from baibai_jobs.publication import machine_preflight
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.lease import _save_handle, acquire, release
from baibai_jobs.publication.machine_bundle import read_published_machine
from baibai_jobs.publication.machine_origin import origin_fields
from baibai_jobs.publication.policy import release_policy_for_profile

METRICS = "edinet.metrics"
OPTIONAL_HISTORY = "edinet.debt_schedule"
AT = datetime(2026, 11, 16, tzinfo=UTC)


@pytest.fixture
def pulled(tmp_path, monkeypatch):
    narrow_release_policy(monkeypatch, datasets=(METRICS, OPTIONAL_HISTORY), carries_history=None)
    market = tmp_path / "market.sqlite"
    mirror = tmp_path / "mirror"
    remote = MemoryR2Store()
    source = tmp_path / "seal-source.sqlite"
    with closing(open_connection(source)) as db, db:
        db.execute(
            "INSERT INTO edinet_debt_schedule VALUES "
            "('0000','D-FIXTURE','2026-11-15T12:00:00Z','2026-11-15','debt','current','issuer',"
            "'2026-09-30','consolidated','borrowings',0,12,1,'JPY','fixture')"
        )

    def publish(
        name,
        *,
        created_at=AT,
        schema_version=SQLITE_SCHEMA_VERSION,
        observation_days=("2026-11-05", "2026-11-15"),
        keep_optional_history=True,
        legacy_metrics=False,
    ):
        # The same EDINET wire contract can have been exported by another SQLite version.
        with closing(sqlite3.connect(source)) as db, db:
            db.execute(f"PRAGMA user_version={schema_version}")
            db.execute("DELETE FROM edinet_metrics")
            db.executemany(
                "INSERT INTO edinet_metrics(asof_date,ticker,fcf_ttm) VALUES(?,'0000',2)",
                [(day,) for day in observation_days],
            )
        catalog = LAKE_DATASETS
        export_source = source
        if legacy_metrics:
            current = catalog[METRICS]
            legacy = max(current.legacy_read_contracts, key=lambda item: item.contract_version)
            catalog = {**catalog, METRICS: legacy}
            export_source = tmp_path / f"{name}.sqlite"
            with (
                closing(sqlite3.connect(source)) as live,
                closing(sqlite3.connect(export_source)) as historical,
            ):
                live.backup(historical)
                previous_columns = {column.name for column in legacy.columns}
                for column in current.columns:
                    if column.name not in previous_columns:
                        historical.execute(
                            f'ALTER TABLE edinet_metrics DROP COLUMN "{column.name}"'
                        )
        with monkeypatch.context() as writer_version:
            writer_version.setattr(
                "baibai_engine.storage.snapshots.writer.SQLITE_SCHEMA_VERSION", schema_version
            )
            with sealed_sqlite_snapshot(sqlite_path=export_source, mirror_root=mirror) as snapshot:
                datasets = (METRICS, OPTIONAL_HISTORY) if keep_optional_history else (METRICS,)
                reports = [
                    export_dataset_snapshot(
                        dataset=catalog[dataset],
                        mirror_root=mirror,
                        producer_git_commit="a" * 40,
                        source_snapshot=snapshot,
                        build_id=f"{name}-{dataset.split('.')[-1]}-{schema_version}",
                        created_at=created_at,
                    )
                    for dataset in datasets
                ]
        path, manifest = create_release(
            catalog=catalog,
            policy=release_policy_for_profile("production"),
            dataset_manifest_paths=[report.manifest_path for report in reports],
            mirror_root=mirror,
            release_id=name,
            created_at=created_at,
        )
        key = release_manifest_key(release_id=name)
        assert path.relative_to(mirror).as_posix() == key
        remote.store_object(key, path.read_bytes())
        for report in reports:
            remote.store_object(
                dataset_manifest_key(
                    dataset=report.manifest.dataset, build_id=report.manifest.build_id
                ),
                report.manifest_path.read_bytes(),
            )
        return name, manifest, sha256_file(path)

    def set_pointer(name, digest):
        pointer = LakeReleasePointer(
            release_id=name,
            manifest_key=release_manifest_key(release_id=name),
            manifest_sha256=digest,
        )
        remote.store_object(current_release_pointer_key(), canonical_lake_model_bytes(pointer))

    name, _, digest = publish("previous", schema_version=SQLITE_SCHEMA_VERSION - 1)
    supplied = LakeStoreOrigin(name, digest)
    with closing(open_connection(market)) as db, db:
        write_lake_store_origin(db, supplied)
    set_pointer(name, digest)
    remote.store_object("market.sqlite", market.read_bytes())
    remote.store_object("runs.sqlite", b"same runs")
    receipt = {
        "schema_version": 1,
        "keys": {key: remote.values[key].etag for key in ("market.sqlite", "runs.sqlite")},
        "daily_publication": origin_fields("12345-1", supplied),
    }
    remote.store_object("machine-manifest.json", json.dumps(receipt).encode())
    return market, remote, receipt, publish, set_pointer


def test_preflight_reads_one_exact_header_and_no_machine_or_data_objects(pulled):
    market, remote, receipt, _, _ = pulled
    before = sha256_file(market)
    assert not machine_preflight.check_daily_input(
        remote, market=market, receipt=json.dumps(receipt).encode()
    )
    assert remote.head_keys == [current_release_pointer_key()]
    assert remote.get_keys == [
        current_release_pointer_key(),
        release_manifest_key(release_id="previous"),
    ]
    assert remote.put_keys == []
    assert sha256_file(market) == before


@pytest.mark.parametrize(
    "case", ["obsolete", "changed-shape", "no-origin", "bad-receipt", "wrong-origin"]
)
def test_preflight_stops_before_lake_access_on_wrong_local_input(pulled, case):
    market, remote, receipt, _, _ = pulled
    with closing(sqlite3.connect(market)) as db, db:
        if case == "obsolete":
            db.execute(f"PRAGMA user_version={SQLITE_SCHEMA_VERSION - 1}")
        elif case == "changed-shape":
            db.execute("DROP TABLE source_coverage")
        elif case == "no-origin":
            db.execute("DELETE FROM lake_store_origin")
    if case == "bad-receipt":
        receipt["schema_version"] = 9
    if case == "wrong-origin":
        receipt["daily_publication"]["release_manifest_sha256"] = "9" * 64
    before = sha256_file(market)
    with pytest.raises(
        (SQLiteSchemaError, LakePublishError, machine_preflight.MachinePreflightError)
    ):
        machine_preflight.check_daily_input(
            remote, market=market, receipt=json.dumps(receipt).encode()
        )
    assert remote.put_keys == remote.get_keys == remote.head_keys == []
    assert sha256_file(market) == before


@pytest.mark.parametrize("is_daily_receipt", [True, False])
def test_pointer_only_partial_recovery_keeps_strict_source_seal_read(pulled, is_daily_receipt):
    market, remote, receipt, publish, set_pointer = pulled
    name, _, digest = publish("forward", created_at=AT + timedelta(seconds=1))
    set_pointer(name, digest)
    before = sha256_file(market)
    # The new daily entry can hydrate forward from the still-coherent old pair.
    if not is_daily_receipt:
        del receipt["daily_publication"]
    payload = json.dumps(receipt).encode()
    remote.store_object("machine-manifest.json", payload)
    assert machine_preflight.check_daily_input(remote, market=market, receipt=payload)
    if is_daily_receipt:
        # Strict source seals still refuse the original unfinished daily pair.
        with pytest.raises(LakePublishError, match="differs from the current lake release"):
            read_published_machine(remote)
    assert remote.put_keys == []
    assert sha256_file(market) == before


@pytest.mark.parametrize("stale", ["paired", "forward-origin", "forward-target"])
def test_readable_historical_edinet_metrics_do_not_authorize_daily(pulled, stale):
    market, remote, receipt, publish, set_pointer = pulled

    class FixedMetadata:
        read_bytes = remote.get_bytes

    newer = AT + timedelta(seconds=1)
    name, _, digest = publish(
        "historic-metrics",
        created_at=newer if stale == "forward-target" else AT,
        schema_version=SQLITE_SCHEMA_VERSION
        if stale == "forward-target"
        else SQLITE_SCHEMA_VERSION - 1,
        legacy_metrics=True,
    )
    historical = resolve_release(FixedMetadata(), name, manifest_sha256=digest)
    current = LAKE_DATASETS[METRICS]
    assert accepted_dataset(historical, current).contract_version < current.contract_version

    if stale != "forward-target":
        supplied = LakeStoreOrigin(name, digest)
        with closing(sqlite3.connect(market)) as db, db:
            write_lake_store_origin(db, supplied)
        remote.store_object("market.sqlite", market.read_bytes())
        receipt["keys"]["market.sqlite"] = remote.values["market.sqlite"].etag
        receipt["daily_publication"] = origin_fields("12345-1", supplied)
    payload = json.dumps(receipt).encode()
    remote.store_object("machine-manifest.json", payload)
    if stale == "forward-origin":
        name, _, digest = publish("forward-current", created_at=newer)
    set_pointer(name, digest)

    before = sha256_file(market)
    with pytest.raises(machine_preflight.MachinePreflightError, match="current EDINET metrics"):
        machine_preflight.check_daily_input(remote, market=market, receipt=payload)
    assert not remote.put_keys
    assert sha256_file(market) == before


@pytest.mark.parametrize(
    "case", ["older", "new-writer", "same-id", "different-hash", "wrong-contract", "absent"]
)
def test_fakes_or_downgrades_cannot_start_the_forward_mode(pulled, case):
    market, remote, receipt, publish, set_pointer = pulled
    begin = AT + timedelta(seconds=1) if case != "older" else AT - timedelta(seconds=1)
    schema = SQLITE_SCHEMA_VERSION + (case == "new-writer")
    name, header, digest = publish(
        "reused" if case == "same-id" else "new", created_at=begin, schema_version=schema
    )
    if case in {"same-id", "wrong-contract"}:
        if case == "same-id":
            name = "previous"
            header = header.model_copy(update={"release_id": name})
        else:
            wrong = header.datasets[METRICS].model_copy(update={"contract_version": 999})
            header = header.model_copy(update={"datasets": {METRICS: wrong}})
        body = canonical_lake_model_bytes(header)
        digest = hashlib.sha256(body).hexdigest()
        remote.store_object(release_manifest_key(release_id=name), body)
    if case == "different-hash":
        digest = "9" * 64
    if case == "absent":
        del remote.values[release_manifest_key(release_id=name)]
    set_pointer(name, digest)
    with pytest.raises(
        (machine_preflight.MachinePreflightError, LakeReadError, ValueError, KeyError)
    ):
        machine_preflight.check_daily_input(
            remote, market=market, receipt=json.dumps(receipt).encode()
        )
    assert remote.put_keys == []


@pytest.mark.parametrize(
    ("observations", "error", "diagnostic"),
    [
        (
            ("2026-11-05", "2026-11-10"),
            machine_preflight.MachinePreflightError,
            "narrow the retained",
        ),
        (("2026-11-10", "2026-11-15"), LakeReadError, "coverage starts .*later"),
    ],
    ids=["older-watermark", "lost-history-floor"],
)
def test_freshly_repacked_older_content_is_not_a_forward_repair(
    pulled, observations, error, diagnostic
):
    market, remote, receipt, publish, set_pointer = pulled
    name, _, digest = publish(
        "fresh-clock-old-data", created_at=AT + timedelta(seconds=1), observation_days=observations
    )
    set_pointer(name, digest)
    before = sha256_file(market)
    with pytest.raises(error, match=diagnostic):
        machine_preflight.check_daily_input(
            remote, market=market, receipt=json.dumps(receipt).encode()
        )
    assert remote.put_keys == []
    assert sha256_file(market) == before


def test_forward_admission_refuses_to_lose_previously_served_optional_history(pulled):
    market, remote, receipt, publish, set_pointer = pulled
    name, _, digest = publish(
        "repacked-without-debt", created_at=AT + timedelta(seconds=1), keep_optional_history=False
    )
    set_pointer(name, digest)
    before = sha256_file(market)
    with pytest.raises(
        LakeReadError, match="historical dataset served previously cannot disappear"
    ):
        machine_preflight.check_daily_input(
            remote, market=market, receipt=json.dumps(receipt).encode()
        )
    assert remote.put_keys == []
    assert sha256_file(market) == before


def test_forward_admission_requires_operational_policy_not_just_the_header(pulled, monkeypatch):
    market, remote, receipt, publish, set_pointer = pulled
    name, header, _ = publish("forward", created_at=AT + timedelta(seconds=1))
    entry = header.datasets[METRICS]
    key = dataset_manifest_key(dataset=METRICS, build_id=entry.build_id)
    rejected = DatasetManifest.model_validate_json(remote.values[key].body).model_copy(
        update={"coverage_status": "partial"}
    )
    dataset_body = canonical_lake_model_bytes(rejected)
    remote.store_object(key, dataset_body)
    entry = entry.model_copy(
        update={
            "coverage_status": "partial",
            "manifest_sha256": hashlib.sha256(dataset_body).hexdigest(),
        }
    )
    release_body = canonical_lake_model_bytes(
        header.model_copy(update={"datasets": {METRICS: entry}})
    )
    remote.store_object(release_manifest_key(release_id=name), release_body)
    set_pointer(name, hashlib.sha256(release_body).hexdigest())
    policy = release_policy_for_profile("production")
    required = next(rule for rule in policy.datasets if rule.dataset == METRICS).model_copy(
        update={"require_complete_coverage": True}
    )
    monkeypatch.setattr(
        machine_preflight,
        "release_policy_for_profile",
        lambda _: policy.model_copy(update={"datasets": (required,)}),
    )
    with pytest.raises(LakeReadError, match="requires complete"):
        machine_preflight.check_daily_input(
            remote, market=market, receipt=json.dumps(receipt).encode()
        )
    assert remote.put_keys == []


def test_preflight_cli_requires_the_held_publication_lease_before_reading_market(
    pulled, tmp_path, monkeypatch, capsys
):
    market, remote, receipt, _, _ = pulled
    monkeypatch.setattr(remote, "close", lambda: None, raising=False)
    monkeypatch.setattr(machine_preflight, "Boto3R2Store", lambda **_: remote)
    local_receipt = tmp_path / "private-receipt.json"
    local_receipt.write_text(json.dumps(receipt))
    local_handle = tmp_path / "handle.json"
    handle, _ = acquire(remote, "daily")
    _save_handle(local_handle, handle)
    args = [
        "--receipt",
        str(local_receipt),
        "--market",
        str(market),
        "--lease-handle",
        str(local_handle),
        "--bucket",
        "offline-fixture",
    ]
    assert machine_preflight.main(args) == 0
    assert capsys.readouterr().out.strip() == "daily machine preflight: paired"
    release(remote, handle)
    remote.get_keys.clear()
    remote.put_keys.clear()
    assert machine_preflight.main(args) == 1
    failure = capsys.readouterr().err.strip()
    assert failure == "daily machine preflight: stopped (LeaseError)"
    assert current_release_pointer_key() not in remote.get_keys
    assert not remote.put_keys
