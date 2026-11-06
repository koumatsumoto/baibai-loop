"""The temporary entry reconstructs a fixed plan, never substitutes lost originals."""

import hashlib
import json
import shutil
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from tests.helpers.r2_store import MemoryR2Store
from tests.jobs.maintenance.test_edinet_archive_import import AT, DAY, source, stage
from tools.diagnostics import import_edinet_archives_once as entry

from baibai_engine.data_collection.providers.edinet.archive_native import (
    HEAD_KEY,
    EdinetArchiveL1,
    NativeArchive,
)
from baibai_engine.data_collection.providers.edinet.list_native import (
    EdinetListL1,
    NativeListReceipt,
)
from baibai_engine.data_collection.providers.edinet.models import EdinetResponse
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_engine.storage.snapshots.hydrate import dehydrate_market_store
from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.lease import _save_handle, acquire


def environment(mode="plan"):
    return {
        "MODE": mode,
        "EXPECTED_CODE_SHA": "a" * 40,
        "FIXED_RELEASE": "fixed-legacy",
        "FIXED_MANIFEST": "b" * 64,
        "FIXED_MARKET": "c" * 64,
        "FIXED_LIST_HEAD": "d" * 64,
        "EXPECTED_ARCHIVE_HEAD": "absent",
        "EXPECTED_PLAN": "e" * 64 if mode in {"apply", "verify"} else "",
        "INITIALIZE_ARCHIVE_HEAD": "false",
        "IMPORT_BUCKET": "baibai-stores",
        "GITHUB_REPOSITORY": entry.REPOSITORY,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_RUN_ATTEMPT": "1",
    }


def fixture(tmp_path, monkeypatch, *, schema=50, dehydrated=True):
    database, release = source(tmp_path)
    if dehydrated:
        dehydrate_market_store(database, catalog=LAKE_DATASETS, release=release)
    store = MemoryR2Store()
    store.store_object("market.sqlite", database.read_bytes())
    lists = EdinetListL1(store, tmp_path / "lists")
    lists.freeze(False)
    lists.publish([NativeListReceipt.from_response(DAY, EdinetResponse(AT, b"list"))])
    stage(
        tmp_path, store, "edinet-facts-old", [("S100OLD", 1, DAY, b"stage original")], schema=schema
    )
    lease, _ = acquire(store, "operator")
    handle = tmp_path / "lease.json"
    _save_handle(handle, lease)
    work = tmp_path / "fixed-work"
    monkeypatch.setattr(entry, "WORK", work)
    monkeypatch.setattr(entry, "RESERVE_BYTES", 0)
    monkeypatch.setattr(entry, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(entry, "resolve_current_release", lambda *_, **__: release)
    monkeypatch.setattr(entry, "check_github", lambda *_: None)

    @contextmanager
    def local_lake(**_):
        with lake_session() as session:
            yield session, LakeObjectCache(work / "cache", LocalMirrorSource(tmp_path / "mirror"))

    monkeypatch.setattr(entry, "open_lake", local_lake)
    request = replace(
        entry.Request.from_env(environment()),
        manifest=release.manifest_sha256,
        market=hashlib.sha256(database.read_bytes()).hexdigest(),
        lists=lists.freeze().identity,
    )
    return store, request, handle, work


@pytest.mark.parametrize("schema", [48, 49, 50])
def test_plan_apply_and_verify_reconstruct_exact_plan_with_all_old_schema_stages(
    tmp_path, monkeypatch, schema
):
    store, request, handle, work = fixture(tmp_path, monkeypatch, schema=schema)
    preserved = {key: item.body for key, item in store.values.items()}
    report = {}
    entry.execute(request, handle, report, lambda: None)
    assert report["status"] == "planned_not_imported"
    assert HEAD_KEY not in store.values
    digest = report["plan_sha256"]
    shutil.rmtree(work)
    report = {}
    before_apply = dict(store.values)
    apply = replace(request, mode="apply", plan=digest, initialize=True)
    entry.execute(apply, handle, report, lambda: None)
    assert report["verified"] is True
    assert report["status"] == "verified_not_cut_over"
    owner = EdinetArchiveL1(store, tmp_path / "readback")
    assert owner.freeze().read_xbrl_zip("S100OLD") == b"stage original"
    assert owner.freeze().read_xbrl_zip("S100ONE") == b"first original"
    assert all(store.values[key].body == value for key, value in preserved.items())
    assert all(
        key.startswith("l1/edinet/archives/v1/") for key in set(store.values) - set(before_apply)
    )
    target = report["observed"]["native_archives"]["wire_sha256"]
    shutil.rmtree(work)
    puts = list(store.put_keys)
    entry.execute(
        replace(request, mode="verify", archives=target, plan=digest), handle, {}, lambda: None
    )
    assert store.put_keys == puts


@pytest.mark.parametrize("change", ["missing", "new", "plan_hash"])
def test_changed_plan_stops_before_any_native_initialization(tmp_path, monkeypatch, change):
    store, request, handle, work = fixture(tmp_path, monkeypatch)
    report = {}
    entry.execute(request, handle, report, lambda: None)
    digest = report["plan_sha256"]
    shutil.rmtree(work)
    if change == "missing":
        for key in tuple(store.values):
            if key.startswith("coordination/market-sources/edinet-facts-"):
                del store.values[key]
    elif change == "new":
        stage(tmp_path, store, "edinet-facts-new", [("S100NEW", 1, DAY, b"new")])
    else:
        digest = "0" * 64
    before = dict(store.values)
    with pytest.raises(entry.ImportGuardError, match="reconstructed plan differs"):
        entry.execute(
            replace(request, mode="apply", plan=digest, initialize=True), handle, {}, lambda: None
        )
    assert store.values == before
    assert HEAD_KEY not in store.values


@pytest.mark.parametrize("orphan", [True, False])
def test_unmanifested_receipt_blocks_even_in_a_valid_stage_directory(tmp_path, monkeypatch, orphan):
    store, request, handle, _ = fixture(tmp_path, monkeypatch)
    name = "orphan" if orphan else "old"
    key = f"coordination/market-sources/edinet-facts-{name}/receipts-{'f' * 64}.sqlite"
    store.store_object(key, b"another retained raw")
    before = dict(store.values)
    with pytest.raises(entry.ImportGuardError, match="unaccounted old receipts"):
        entry.execute(request, handle, {}, lambda: None)
    assert store.values == before


def test_existing_canonical_raw_is_not_overwritten_by_hydrate(tmp_path, monkeypatch):
    store, request, handle, _ = fixture(tmp_path, monkeypatch, dehydrated=False)
    hydrate = Mock(side_effect=AssertionError("must not replace canonical raw"))
    monkeypatch.setattr(entry, "hydrate_market_store", hydrate)
    before = dict(store.values)
    with pytest.raises(entry.ImportGuardError, match="nonempty canonical originals"):
        entry.execute(request, handle, {}, lambda: None)
    hydrate.assert_not_called()
    assert store.values == before


def test_changed_target_after_verify_is_not_reported_as_verified(tmp_path, monkeypatch):
    store, request, handle, work = fixture(tmp_path, monkeypatch)
    report = {}
    entry.execute(request, handle, report, lambda: None)
    digest = report["plan_sha256"]
    shutil.rmtree(work)
    verify = entry.verify_import

    def move(owner, *, directory):
        verify(owner, directory=directory)
        EdinetArchiveL1(store, tmp_path / "other-writer").publish(
            [
                NativeArchive.from_response("S100LATER", 1, EdinetResponse(AT, b"later raw")),
            ]
        )

    monkeypatch.setattr(entry, "verify_import", move)
    report = {}
    with pytest.raises(entry.ImportGuardError, match="head changed after"):
        entry.execute(
            replace(request, mode="apply", plan=digest, initialize=True),
            handle,
            report,
            lambda: None,
        )
    assert report["native_write_may_have_occurred"] is True
    assert not report.get("verified")


def test_inspect_never_downloads_originals_or_initializes(tmp_path, monkeypatch):
    store, request, handle, _ = fixture(tmp_path, monkeypatch)
    store.download_file = Mock(side_effect=AssertionError("inspect is metadata-only"))
    puts = list(store.put_keys)
    report = {}
    entry.execute(replace(request, mode="inspect"), handle, report, lambda: None)
    assert report["status"] == "observed_not_drained"
    assert store.put_keys == puts
    store.download_file.assert_not_called()


@pytest.mark.parametrize("field", ["FIXED_MARKET", "EXPECTED_PLAN"])
def test_apply_and_verify_require_prior_plan_and_actual_canonical_digest(field):
    for mode in ("apply", "verify"):
        env = environment(mode)
        env[field] = ""
        with pytest.raises(entry.ImportGuardError, match="preceding canonical and plan"):
            entry.Request.from_env(env)


@pytest.mark.parametrize(
    "change",
    [
        {"GITHUB_RUN_ATTEMPT": "2"},
        {"MODE": "verify"},
        {"EXPECTED_ARCHIVE_HEAD": "f" * 64},
    ],
)
def test_initialize_is_explicit_first_attempt_only(change):
    env = environment("apply") | {"INITIALIZE_ARCHIVE_HEAD": "true"} | change
    with pytest.raises(entry.ImportGuardError, match="initialization requires"):
        entry.Request.from_env(env)


def test_drain_requires_disabled_and_no_active_old_runs(monkeypatch):
    env = environment()
    request = entry.Request.from_env(env)
    calls = []

    def api(path):
        calls.append(path)
        if path == "git/ref/heads/main":
            return {"object": {"sha": request.code}}
        if "/runs?" in path:
            return {"total_count": 1 if "status=waiting" in path else 0}
        return {"state": "disabled_manually"}

    monkeypatch.setattr(entry, "_api", api)
    with pytest.raises(entry.ImportGuardError, match="not drained"):
        entry.check_github(request, env)
    assert any("status=waiting" in item for item in calls)
    monkeypatch.setattr(
        entry,
        "_api",
        lambda path: (
            {"object": {"sha": request.code}} if path.startswith("git/") else {"state": "active"}
        ),
    )
    with pytest.raises(entry.ImportGuardError, match="stay disabled"):
        entry.check_github(request, env)


def test_capacity_failure_after_prepare_still_has_no_native_writes(tmp_path, monkeypatch):
    store, request, handle, _ = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        entry,
        "inspect_prepared_capacity",
        lambda *_, **__: {
            "free_bytes": 1,
            "native_body_write_peak_bytes": 2,
        },
    )
    before = dict(store.values)
    report = {}
    with pytest.raises(entry.ImportGuardError, match="additional room"):
        entry.execute(request, handle, report, lambda: None)
    assert len(report["plan_sha256"]) == 64
    assert store.values == before


def test_private_library_stdout_stderr_and_error_never_reach_public_summary(
    tmp_path, monkeypatch, capsys
):
    for key, value in environment("inspect").items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(entry, "check_github", lambda *_: None)
    private, summary = tmp_path / "private.log", tmp_path / "summary.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "entry",
            "run",
            "--private-log",
            str(private),
            "--summary",
            str(summary),
            "--lease-handle",
            str(tmp_path / "lease"),
        ],
    )

    def fail(*_):
        print("private raw stdout canary")
        print("private raw stderr canary", file=sys.stderr)
        raise ValueError("private raw exception canary")

    monkeypatch.setattr(entry, "execute", fail)
    assert entry.main() == 1
    public = capsys.readouterr()
    assert "canary" not in public.out + public.err + summary.read_text()
    assert "stdout canary" in private.read_text()
    assert "stderr canary" in private.read_text()
    assert private.stat().st_mode & 0o777 == 0o600
    assert json.loads(summary.read_bytes())["error_type"] == "ValueError"


def test_workflow_is_manual_only_has_existing_locks_and_no_public_raw_artifact():
    root = Path(__file__).resolve().parents[2]
    raw = (root / ".github/workflows/import-edinet-archives-once.yml").read_text()
    workflow = yaml.safe_load(raw)
    assert set(workflow.get("on", workflow.get(True))) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    assert workflow["concurrency"]["group"] == "common-source-integration"
    job = workflow["jobs"]["migrate"]
    assert job["concurrency"]["group"] == "source-acquire-edinet"
    assert job["timeout-minutes"] < 120
    assert job["env"]["R2_STORES_BUCKET"] == "${{ inputs.bucket }}"
    assert "--bucket" not in raw
    assert "upload-artifact" not in raw
    assert "DISCORD" not in raw
    assert "EDINET_API" not in raw
    assert 'cat "$log"' not in raw
    assert "steps.lease-acquire.outputs.acquired == 'true'" in raw
    assert '--public-fd 3 3>&1 >>"$log" 2>&1' in raw


def test_shell_capture_covers_native_file_descriptors_too(tmp_path):
    private, summary = tmp_path / "capture.log", tmp_path / "summary.json"
    code = """
import os, sys
from tools.diagnostics import import_edinet_archives_once as entry
entry.check_github = lambda *_: None
def fail(*_):
    print('python-private-canary')
    os.write(1, b'native-stdout-private-canary\\n')
    os.write(2, b'native-stderr-private-canary\\n')
    raise ValueError('exception-private-canary')
entry.execute = fail
raise SystemExit(entry.main())
"""
    import os

    result = subprocess.run(
        [
            "bash",
            "-c",
            'umask 077; "$@" 3>&1 >>"$CAPTURE" 2>&1',
            "bash",
            sys.executable,
            "-c",
            code,
            "run",
            "--private-log",
            str(private),
            "--summary",
            str(summary),
            "--lease-handle",
            str(tmp_path / "lease"),
            "--public-fd",
            "3",
        ],
        env=os.environ | environment("inspect") | {"CAPTURE": str(private)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "canary" not in result.stdout + result.stderr + summary.read_text()
    assert "native-stdout-private-canary" in private.read_text()
    assert "native-stderr-private-canary" in private.read_text()
    assert json.loads(result.stdout)["status"] == "failed"
    assert private.stat().st_mode & 0o777 == 0o600
