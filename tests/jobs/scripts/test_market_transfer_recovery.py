"""Current-schema machine transfer recovery using real snapshot and L1 helpers."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest
from tests.helpers.financial_documents import documents
from tests.helpers.lake_policy import narrow_release_policy

from baibai_engine.machine_analysis.observations.jquants_writer import store_jquants_fin_summaries
from baibai_engine.storage.snapshots.keys import current_release_pointer_key
from baibai_engine.storage.snapshots.models import canonical_lake_model_bytes
from baibai_engine.storage.snapshots.release import LakeReleasePointer, create_release
from baibai_engine.storage.snapshots.sources import sha256_file
from baibai_engine.storage.snapshots.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    LakeStoreOrigin,
    read_lake_store_origin,
    write_lake_store_origin,
)
from baibai_engine.storage.sqlite.source_cache.schema import validate_current_schema
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.policy import release_policy_for_profile

FIN = "jquants.fin_summaries"
TABLE = "jquants_fin_summaries"
MACHINE_KEYS = ("market.sqlite", "runs.sqlite", "indicators.sqlite")


def table_contents(path: Path) -> dict[str, list[tuple]]:
    """Compare every stored row, without SQLite page-layout or ordering differences."""
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        result = {}
        for (table,) in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            rows = conn.execute(f"SELECT * FROM {quoted}").fetchall()  # nosec B608
            result[table] = sorted(rows, key=repr)
        return result


@pytest.fixture
def published_market(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    narrow_release_policy(monkeypatch, datasets=(FIN,))
    store = tmp_path / "market.sqlite"
    store_jquants_fin_summaries(
        store,
        documents(),
        requested_start=date(2026, 8, 10),
        requested_end=date(2026, 9, 14),
    )
    mirror = tmp_path / "mirror"
    with sealed_sqlite_snapshot(
        sqlite_path=store, mirror_root=mirror, snapshot_id="transfer-test"
    ) as snapshot:
        report = export_dataset_snapshot(
            dataset=LAKE_DATASETS[FIN],
            mirror_root=mirror,
            source_snapshot=snapshot,
            producer_git_commit="a" * 40,
        )
    manifest_path, release = create_release(
        dataset_manifest_paths=[report.manifest_path],
        mirror_root=mirror,
        release_id="transfer-release",
        catalog=LAKE_DATASETS,
        policy=release_policy_for_profile("production"),
    )
    origin = LakeStoreOrigin(release.release_id, sha256_file(manifest_path))
    with closing(sqlite3.connect(store)) as conn:
        write_lake_store_origin(conn, origin)
        conn.commit()
    pointer = LakeReleasePointer(
        release_id=origin.release_id,
        manifest_key=manifest_path.relative_to(mirror).as_posix(),
        manifest_sha256=origin.release_manifest_sha256,
    )
    pointer_path = mirror / current_release_pointer_key()
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.write_bytes(canonical_lake_model_bytes(pointer))
    return store, mirror, origin


@pytest.mark.parametrize("failed_key", ["runs.sqlite", "machine-manifest.json"])
def test_real_transfer_partial_push_recovers_via_same_schema_market_merge(
    published_market, tmp_path: Path, failed_key: str
) -> None:
    """Real snapshot/dehydrate/merge/hydrate helpers, with only S3 CLI stubbed."""
    import shutil
    import subprocess
    import sys

    from tests.helpers.r2_transfer import _environment, _fake_aws, _fake_repo

    from baibai_jobs.publication import policy as snapshot_policy

    target, mirror, origin = published_market
    root = _fake_repo(tmp_path)
    for relative in (
        "market/market.sqlite",
        "screening/runs.sqlite",
        "market_context/indicators.sqlite",
    ):
        destination = root / "stores" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(target, destination)
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    state = Path(env["AWS_FAKE_STATE"])
    env["R2_LAKE_MIRROR"] = str(mirror)
    env["AWS_FAKE_EXISTING_KEY"] = ":".join(
        (*MACHINE_KEYS, current_release_pointer_key(), "machine-manifest.json")
    )
    for key in MACHINE_KEYS:
        (state / f"put-{key}").write_bytes(target.read_bytes())
    old_receipt = {"schema_version": 1, "keys": dict.fromkeys(MACHINE_KEYS, "etag-stable")}
    (state / "put-machine-manifest.json").write_text(json.dumps(old_receipt, indent=2))
    # Real snapshot helpers require stored bytes, not synthetic downloads for absent keys.
    aws = bin_dir / "aws"
    script = aws.read_text()
    script = script.replace(
        "  printf 'x' > \"$4\"",
        '  key="${3#s3://}"\n  key="${key#*/}"\n  cp "${AWS_FAKE_STATE}/put-${key}" "$4"',
    )
    aws.write_text(script)
    policy = tmp_path / "release-policy.json"
    policy.write_text(snapshot_policy.PRODUCTION_RELEASE_POLICY.model_dump_json())
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
if args[:3]==['python','-m','baibai_jobs.observability.repository_layout']:
 sys.exit(0) # isolated shell root intentionally contains no application store
if args[:3]==['baibai-jobs','snapshots','dehydrate'] or args[:3]==['baibai-jobs','snapshots','hydrate']:
 from baibai_engine.storage.snapshots import models
 from baibai_jobs.publication import policy as snapshot_policy
 snapshot_policy.PRODUCTION_RELEASE_POLICY=models.ReleasePolicy.model_validate_json(Path(os.environ['TRANSFER_TEST_POLICY']).read_text())
 args=args[2:]
 i=args.index('--bucket');del args[i:i+2]
 from baibai_jobs.publication.snapshot_write_cli import main
 sys.exit(main(args))
assert args[0]=='python'
if args[1]=='-c':
 exec(args[2]);sys.exit(0)
assert args[1]=='-m'
sys.argv=args[2:]
runpy.run_module(args[2],run_name='__main__')
"""
    )
    script_path = root / "jobs/scripts/r2_transfer.sh"

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
        assert conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] == 0
    assert read_lake_store_origin(state / "put-market.sqlite") == origin
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
    assert table_contents(root / "stores/market/market.sqlite") == table_contents(target)
    calls = Path(env["TRANSFER_TEST_LOG"]).read_text()
    assert "baibai_jobs.integration.source_cache_merge" in calls
    assert calls.count('"dehydrate"') == 2
    assert '"hydrate"' in calls
    assert "--if-match" in log.read_text()
    assert "baibai.sqlite" not in log.read_text()
    assert (state / "put-machine-manifest.json").is_file()
