"""Focused #1520 handoff tests use local SQLite/Parquet and existing memory R2."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.engine.test_financial_documents import documents
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.r2_store import MemoryR2Store
from tools.cutover import financial_documents as cutover

from baibai_batch.storage import publication_lease
from baibai_batch.storage.lake_publish import LakeCASConflict
from baibai_engine.market.financial_documents import normalize_financial_summary
from baibai_engine.storage.lake import writer
from baibai_engine.storage.lake.duck import lake_session
from baibai_engine.storage.lake.hydrate import hydrate_market_store
from baibai_engine.storage.lake.keys import current_l1_pointer_key
from baibai_engine.storage.lake.models import SQLiteSnapshotSourceRef, canonical_lake_model_bytes
from baibai_engine.storage.lake.objects import LakeObjectCache, LocalMirrorSource, sha256_file
from baibai_engine.storage.lake.reader import resolve_release
from baibai_engine.storage.lake.release import L1ReleasePointer, create_l1_release
from baibai_engine.storage.observations.coverage import record_source_coverage
from baibai_engine.storage.observations.lake_origin import (
    LakeStoreOrigin,
    read_lake_store_origin,
    write_lake_store_origin,
)
from baibai_engine.storage.observations.schema import (
    SQLiteSchemaError,
    open_connection,
    validate_current_schema,
)


def raw_documents():
    records = documents()
    for row, number in zip(records, cutover.RAW_SCOPE, strict=True):
        row["Code"], row["DiscNo"] = "246A0", number
    return records


def old_store(path: Path) -> Path:
    conn = open_connection(path)
    conn.execute(f"DROP TABLE {cutover.TABLE}")
    columns = writer.require_lake_dataset(cutover.FIN).columns[:30]
    ddl = ",".join(
        f"{column.name} {column.sqlite_type}" + (" NOT NULL" if not column.nullable else "")
        for column in columns
    )
    conn.execute(f"CREATE TABLE {cutover.TABLE}({ddl},PRIMARY KEY(ticker,disclosed_at))")
    rows = []
    for record in (raw_documents()[0], raw_documents()[-1]):
        summary = normalize_financial_summary(record)
        assert summary is not None
        values = [getattr(summary, column) for column in cutover.LEGACY_COLUMNS]
        rows.append(
            tuple(value.isoformat() if hasattr(value, "isoformat") else value for value in values)
        )
    placeholders = ",".join("?" for _ in columns)
    conn.executemany(f"INSERT INTO {cutover.TABLE} VALUES ({placeholders})", rows)
    # An unrelated issuer is the preservation control.
    conn.execute(
        f"INSERT INTO {cutover.TABLE}(ticker,disclosed_at,eps_ttm) VALUES ('1301','2026-08-10',25)"
    )
    record_source_coverage(
        conn,
        source=cutover.TABLE,
        operation="get_fin_summary_range",
        coverage_key="get_fin_summary_range:2026-08-10..2026-09-14",
        coverage_start="2026-08-10",
        coverage_end="2026-09-14",
        requested_start="2026-08-10",
        requested_end="2026-09-14",
        record_count=3,
        status="ok",
        params={},
    )
    conn.execute("PRAGMA user_version=31")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    narrow_release_policy(monkeypatch, datasets=(cutover.FIN,))
    source = old_store(tmp_path / "old.sqlite")
    mirror = tmp_path / "mirror"
    with cutover.legacy_reader():
        snap = writer.LegacySQLiteSnapshot(
            path=source,
            ref=SQLiteSnapshotSourceRef(
                kind="sqlite_snapshot",
                source_id=f"market-v31-{sha256_file(source)[:24]}",
                sha256=sha256_file(source),
                schema_version=31,
                captured_at=datetime(2026, 10, 14, tzinfo=UTC),
            ),
        )
        report = writer.export_legacy_sqlite(
            dataset_name=cutover.FIN,
            mirror_root=mirror,
            source_snapshot=snap,
            producer_git_commit="a" * 40,
        )
        manifest_path, release = create_l1_release(
            dataset_manifest_paths=[report.manifest_path],
            mirror_root=mirror,
            release_id="legacy-r1",
        )
    origin = LakeStoreOrigin(release.release_id, sha256_file(manifest_path))
    with closing(sqlite3.connect(source)) as conn:
        write_lake_store_origin(conn, origin)
        conn.commit()
    remote = MemoryR2Store()
    for key in cutover.MACHINE_KEYS:
        remote.store_object(key, source.read_bytes(), content_type="application/octet-stream")
    pointer = L1ReleasePointer(
        release_id=origin.release_id,
        manifest_key=manifest_path.relative_to(mirror).as_posix(),
        manifest_sha256=origin.release_manifest_sha256,
    )
    remote.store_object(
        current_l1_pointer_key(),
        canonical_lake_model_bytes(pointer),
        content_type="application/json",
    )
    generations = {key: remote.head(key).etag for key in cutover.MACHINE_KEYS}
    remote.store_object(
        "machine-manifest.json",
        json.dumps(
            {
                "schema_version": 1,
                "written_at_utc": datetime.now(UTC).isoformat(),
                "keys": generations,
            }
        ).encode(),
        content_type="application/json",
    )
    bundle = tmp_path / "bundle"
    for relative in (
        "market/market.sqlite",
        "screening/runs.sqlite",
        "market_context/indicators.sqlite",
    ):
        destination = bundle / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)
    target = tmp_path / "stage.sqlite"
    receipt = cutover.prepare(
        source,
        target,
        mirror,
        generations,
        remote.head(current_l1_pointer_key()).etag,
        raw_documents(),
        producer="a" * 40,
        bundle=bundle,
    )
    handle, _ = publication_lease.acquire(remote, "operator")
    return source, target, mirror, receipt, remote, handle


def test_exact_conversion_and_export_fresh_hydrate(stage, tmp_path: Path) -> None:
    source, target, mirror, receipt, remote, handle = stage
    assert receipt["source_tables"][cutover.TABLE]["rows"] == 3
    assert receipt["stage_tables"][cutover.TABLE]["rows"] == 5
    with closing(sqlite3.connect(source)) as conn, pytest.raises(SQLiteSchemaError):
        validate_current_schema(conn)
    cutover.publish(target, mirror, receipt, remote, handle, 1)
    assert read_lake_store_origin(target) == LakeStoreOrigin(**receipt["new_origin"])
    arrived = tmp_path / "arrived.sqlite"
    with closing(sqlite3.connect(target)) as src, closing(sqlite3.connect(arrived)) as dst:
        src.backup(dst)
        dst.execute(f"DELETE FROM {cutover.TABLE}")
        dst.commit()
    release = resolve_release(
        LocalMirrorSource(mirror),
        release_id=receipt["new_origin"]["release_id"],
        manifest_sha256=receipt["new_origin"]["release_manifest_sha256"],
    )
    with lake_session() as session:
        hydrate_market_store(
            session,
            release=release,
            cache=LakeObjectCache(root=mirror, source=LocalMirrorSource(mirror)),
            store=arrived,
            dataset_names=(cutover.FIN,),
        )
    assert cutover.fingerprints(arrived) == cutover.fingerprints(target)


def test_non31_source_and_existing_destination_are_refused(tmp_path: Path) -> None:
    source = tmp_path / "current.sqlite"
    open_connection(source).close()
    with pytest.raises(ValueError, match="exact schema 31"):
        cutover.convert(source, tmp_path / "bad.sqlite")
    old = old_store(tmp_path / "old.sqlite")
    with pytest.raises(ValueError, match="exists"):
        cutover.convert(old, source)


def test_stale_generations_and_expired_lease_do_not_switch_pointer(stage) -> None:
    _, target, mirror, receipt, remote, handle = stage
    old_pointer = remote.get_bytes(current_l1_pointer_key())
    remote.store_object(
        "indicators.sqlite", b"newer indicator data", content_type="application/octet-stream"
    )
    with pytest.raises(ValueError, match="generation changed"):
        cutover.publish(target, mirror, receipt, remote, handle, 1)
    assert remote.get_bytes(current_l1_pointer_key()) == old_pointer
    expired = publication_lease.PublicationLeaseHandle(
        handle.owner,
        handle.purpose,
        handle.etag,
        handle.acquired_at_utc - timedelta(hours=3),
        handle.expires_at_utc - timedelta(hours=3),
    )
    with pytest.raises(publication_lease.LeaseError):
        cutover.publish(target, mirror, receipt, remote, expired, 1)


def test_pointer_cas_refusal(stage) -> None:
    _, target, mirror, receipt, remote, handle = stage
    remote.conflict_pointer = True
    with pytest.raises(LakeCASConflict):
        cutover.publish(target, mirror, receipt, remote, handle, 1)
    assert read_lake_store_origin(target) == LakeStoreOrigin(**receipt["old_origin"])


def test_pointer_after_write_before_origin_is_reconciled_without_republish(
    stage, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, target, mirror, receipt, remote, handle = stage
    advance = cutover.advance_lake_store_origin

    def fail(*args, **kwargs):
        raise RuntimeError("origin update stopped")

    monkeypatch.setattr(cutover, "advance_lake_store_origin", fail)
    with pytest.raises(RuntimeError, match="origin update"):
        cutover.publish(target, mirror, receipt, remote, handle, 1)
    assert read_lake_store_origin(target) == LakeStoreOrigin(**receipt["old_origin"])
    remote_source = target.with_name("remote-old.sqlite")
    remote_source.write_bytes(remote.get_bytes("market.sqlite"))
    with closing(sqlite3.connect(remote_source)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 31
        with pytest.raises(SQLiteSchemaError):
            validate_current_schema(conn)
    monkeypatch.setattr(cutover, "advance_lake_store_origin", advance)
    writes = len(remote.put_keys)
    cutover.reconcile_origin(target, mirror, receipt, remote, handle, 1)
    assert len(remote.put_keys) == writes
    cutover.verify_machine_generation(remote, receipt)
    assert read_lake_store_origin(target) == LakeStoreOrigin(**receipt["new_origin"])
    remote_source.unlink()


def test_partial_machine_generations_are_inspected_and_never_blindly_retried(
    stage, tmp_path: Path
) -> None:
    _, target, mirror, receipt, remote, handle = stage
    cutover.publish(target, mirror, receipt, remote, handle, 1)
    # Stop after market PUT and before runs/indicators/receipt. Application never enters this set.
    remote.store_object(
        "market.sqlite", target.read_bytes(), content_type="application/octet-stream"
    )
    with pytest.raises(ValueError, match="generation changed"):
        cutover.verify_machine_generation(remote, receipt)
    writes = len(remote.put_keys)
    inspected = cutover.inspect_machine(remote, receipt, tmp_path / "recovery")
    assert not inspected["market.sqlite"]["original_generation"]
    assert inspected["runs.sqlite"]["original_generation"]
    assert inspected["indicators.sqlite"]["original_generation"]
    assert len(remote.put_keys) == writes
    assert tuple(key for key in inspected if key in cutover.MACHINE_KEYS) == cutover.MACHINE_KEYS
    assert not inspected["bundle_receipt"]["matches"]


def test_all_machine_puts_before_receipt_remains_a_recovery_boundary(stage, tmp_path: Path) -> None:
    _, target, mirror, receipt, remote, handle = stage
    cutover.publish(target, mirror, receipt, remote, handle, 1)
    for key in cutover.MACHINE_KEYS:
        remote.store_object(key, target.read_bytes(), content_type="application/octet-stream")
    report = cutover.inspect_machine(remote, receipt, tmp_path / "receipt-recovery")
    assert not report["bundle_receipt"]["matches"]
    assert all(not report[key]["original_generation"] for key in cutover.MACHINE_KEYS)
    assert (tmp_path / "receipt-recovery/machine-manifest.json").exists()
    with pytest.raises(ValueError, match="generation changed"):
        cutover.verify_machine_generation(remote, receipt)


def test_machine_handoff_refuses_changed_indicator_and_lost_run_rows(stage) -> None:
    _, target, mirror, receipt, remote, handle = stage
    cutover.publish(target, mirror, receipt, remote, handle, 1)
    bundle = target.parent / "bundle"
    with (
        closing(sqlite3.connect(target)) as src,
        closing(sqlite3.connect(bundle / "market/market.sqlite")) as dst,
    ):
        src.backup(dst)
    cutover.verify_machine_inputs(bundle, receipt)
    indicators = bundle / "market_context/indicators.sqlite"
    with closing(sqlite3.connect(indicators)) as conn:
        conn.execute(f"DELETE FROM {cutover.TABLE}")
        conn.commit()
    with pytest.raises(ValueError, match="indicator working copy changed"):
        cutover.verify_machine_inputs(bundle, receipt)
    with (
        closing(sqlite3.connect(receipt["original_runs"])) as src,
        closing(sqlite3.connect(indicators)) as dst,
    ):
        src.backup(dst)
    runs = bundle / "screening/runs.sqlite"
    with closing(sqlite3.connect(runs)) as conn:
        conn.execute(f"DELETE FROM {cutover.TABLE}")
        conn.commit()
    with pytest.raises(ValueError, match="discard an original row"):
        cutover.verify_machine_inputs(bundle, receipt)


def test_insufficient_full_push_time_budget_stops_before_publication(stage) -> None:
    _, target, mirror, receipt, remote, handle = stage
    before = remote.get_bytes(current_l1_pointer_key())
    with pytest.raises(publication_lease.LeaseError, match="insufficient lease time"):
        cutover.publish(target, mirror, receipt, remote, handle, 7201)
    assert remote.get_bytes(current_l1_pointer_key()) == before


def test_runs_no_loss_check_refuses_a_truncated_saved_baseline(stage) -> None:
    _, target, mirror, receipt, remote, handle = stage
    cutover.publish(target, mirror, receipt, remote, handle, 1)
    bundle = target.parent / "bundle"
    with (
        closing(sqlite3.connect(target)) as src,
        closing(sqlite3.connect(bundle / "market/market.sqlite")) as dst,
    ):
        src.backup(dst)
    # Deleting the same rows in both copies must not weaken the captured baseline.
    for path in (Path(receipt["original_runs"]), bundle / "screening/runs.sqlite"):
        with closing(sqlite3.connect(path)) as conn:
            conn.execute(f"DELETE FROM {cutover.TABLE}")
            conn.commit()
    with pytest.raises(ValueError, match="original runs preservation file changed"):
        cutover.verify_machine_inputs(bundle, receipt)


def test_materialize_workflow_acquires_its_own_lease_and_merge_does_not_dispatch() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github/workflows/cloud-materialize.yml").read_text()
    assert "--purpose materialize" in workflow
    assert workflow.index("--purpose materialize") < workflow.index("hydrate-market")
    assert "push:" not in workflow


@pytest.mark.parametrize("failed_key", ["runs.sqlite", "machine-manifest.json"])
def test_real_transfer_partial_push_recovers_via_same_schema_market_merge(
    stage, tmp_path: Path, failed_key: str
) -> None:
    """Real snapshot/dehydrate/merge/hydrate helpers, with only S3 CLI stubbed."""
    import shutil
    import subprocess
    import sys

    from tests.batch.test_cloud_r2_transfer import _environment, _fake_aws, _fake_repo

    from baibai_engine.storage.lake import models

    _, target, mirror, receipt, remote, handle = stage
    cutover.publish(target, mirror, receipt, remote, handle, 1)
    pointer = mirror / current_l1_pointer_key()
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_bytes(remote.get_bytes(current_l1_pointer_key()))
    root = _fake_repo(tmp_path)
    shutil.copytree(target.parent / "bundle", root / "stores")
    with (
        closing(sqlite3.connect(target)) as src,
        closing(sqlite3.connect(root / "stores/market/market.sqlite")) as dst,
    ):
        src.backup(dst)
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    state = Path(env["AWS_FAKE_STATE"])
    env["R2_LAKE_MIRROR"] = str(mirror)
    env["AWS_FAKE_EXISTING_KEY"] = ":".join(
        (*cutover.MACHINE_KEYS, current_l1_pointer_key(), "machine-manifest.json")
    )
    for key in cutover.MACHINE_KEYS:
        (state / f"put-{key}").write_bytes(remote.get_bytes(key))
    old_receipt = {"schema_version": 1, "keys": dict.fromkeys(cutover.MACHINE_KEYS, "etag-stable")}
    (state / "put-machine-manifest.json").write_text(json.dumps(old_receipt, indent=2))
    # Model a rejected PUT before committing its bytes; the existing fake otherwise
    # stores the request body even when the injected failure has no new generation.
    aws = bin_dir / "aws"
    script = aws.read_text()
    failure_start = script.index('  if [[ -n "${AWS_FAKE_FAIL_PUT_KEY:-}"')
    failure_end = script.index("  fi\n", failure_start) + len("  fi\n")
    failure = script[failure_start:failure_end]
    script = script.replace(failure, "")
    script = script.replace(
        "  # Every uploaded body is kept", failure + "  # Every uploaded body is kept"
    )
    script = script.replace(
        "  printf 'x' > \"$4\"",
        '  key="${3#s3://}"\n  key="${key#*/}"\n  cp "${AWS_FAKE_STATE}/put-${key}" "$4"',
    )
    aws.write_text(script)
    policy = tmp_path / "release-policy.json"
    policy.write_text(models.PRODUCTION_RELEASE_POLICY.model_dump_json())
    env["TRANSFER_TEST_POLICY"] = str(policy)
    env["TRANSFER_TEST_LOG"] = str(tmp_path / "real-helpers.log")
    # Keep the shell exactly as shipped. The uv adapter selects the local mirror
    # instead of R2, then executes the real Python modules in this test environment.
    (bin_dir / "uv").write_text(
        f"#!{sys.executable}\n"
        """import json,os,runpy,sys
from pathlib import Path
args=sys.argv[1:]
assert args.pop(0)=='run'
with Path(os.environ['TRANSFER_TEST_LOG']).open('a') as log:
 log.write(json.dumps(args)+'\\n')
if args[:3]==['python','-m','baibai_batch.validation.repository_layout']:
 sys.exit(0) # isolated shell root intentionally contains no application store
if args[:3]==['baibai-engine','lake','dehydrate'] or args[:3]==['baibai-engine','lake','hydrate']:
 from baibai_engine.storage.lake import models
 models.PRODUCTION_RELEASE_POLICY=models.ReleasePolicy.model_validate_json(Path(os.environ['TRANSFER_TEST_POLICY']).read_text())
 args=args[2:]
 i=args.index('--bucket');del args[i:i+2]
 from baibai_engine.storage.lake.write_cli import main
 sys.exit(main(args))
assert args[0]=='python'
if args[1]=='-c':
 exec(args[2]);sys.exit(0)
assert args[1]=='-m'
sys.argv=args[2:]
runpy.run_module(args[2],run_name='__main__')
"""
    )
    script_path = root / "batch/scripts/r2_transfer.sh"

    def run(action: str, **overrides: str):
        return subprocess.run(
            [script_path, action],
            cwd=root,
            env={**env, **overrides},
            capture_output=True,
            text=True,
            check=False,
        )

    partial = run("push-machine", AWS_FAKE_FAIL_PUT_KEY=failed_key)
    assert partial.returncode != 0, partial.stdout
    with closing(sqlite3.connect(state / "put-market.sqlite")) as conn:
        validate_current_schema(conn)
        assert conn.execute(f"SELECT COUNT(*) FROM {cutover.TABLE}").fetchone()[0] == 0
    assert read_lake_store_origin(state / "put-market.sqlite") == LakeStoreOrigin(
        **receipt["new_origin"]
    )
    assert json.loads((state / "put-machine-manifest.json").read_text()) == old_receipt
    retry = run("push-machine")
    assert retry.returncode != 0
    assert "changed on R2 after the pull" in retry.stderr
    refused = run("pull-machine")
    assert refused.returncode != 0
    assert "receipt" in refused.stderr
    recovery = run("push-market")
    assert recovery.returncode == 0, recovery.stderr
    verified = run("pull-machine")
    assert verified.returncode == 0, verified.stderr
    assert "machine bundle receipt matches" in verified.stdout
    hydrated = run("hydrate-market")
    assert hydrated.returncode == 0, hydrated.stderr
    assert cutover.fingerprints(root / "stores/market/market.sqlite") == cutover.fingerprints(
        target
    )
    calls = Path(env["TRANSFER_TEST_LOG"]).read_text()
    assert "baibai_batch.storage.merge_market_store" in calls
    assert calls.count('"dehydrate"') == 2
    assert '"hydrate"' in calls
    assert "--if-match" in log.read_text()
    assert "baibai.sqlite" not in log.read_text()
    assert (state / "put-machine-manifest.json").is_file()
