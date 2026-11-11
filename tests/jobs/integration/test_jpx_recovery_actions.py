"""Finite recovery orchestration; all inputs and object bytes are synthetic."""

import hashlib
import json
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from pydantic import ValidationError
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.storage.objects.r2 import LakePublishError, R2OperationStopped
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.integration import jpx_recovery as recovery
from baibai_jobs.integration.jpx_recovery_contract import RecoveryContract, decode_contract
from baibai_jobs.integration.jpx_recovery_verification import MARKET_DATASETS, row_fingerprint
from baibai_jobs.integration.workflow_inputs import main as validate_dispatch


def _payload(value):
    return json.dumps(value, sort_keys=True).encode()


def _sha(value):
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def contract_store():
    store = MemoryR2Store()
    stages, entries = [], []
    for day, role in enumerate(("recovery", "preserved", "pending"), start=1):
        for source in ("jpx-earnings", "jpx-regulations"):
            stage = f"{source}-2026-01-0{day}-10-1"
            prefix = "coordination/market-sources/" + stage
            original = (stage + " original receipt").encode()
            receipt_sha = _sha(original)
            started = f"2026-01-0{day}T00:00:00+00:00"
            manifest = _payload(
                {
                    "receipt_sha256": receipt_sha,
                    "receipt_bytes": len(original),
                    "receipt_key": prefix + "/receipts-" + receipt_sha + ".sqlite",
                    "result": {"source": source, "started_at": started, "asof": f"2026-01-0{day}"},
                }
            )
            store.store_object(prefix + "/handoff.json", manifest)
            store.store_object(prefix + "/receipts-" + receipt_sha + ".sqlite", original)
            marker = {
                "source": source,
                "started_at": started,
                "manifest_sha256": _sha(manifest),
                "status": "needs_explicit_recovery" if role == "recovery" else "integrated",
                "canonical_generations": {"lake_release_id": "before"},
                "integrated_at": "2026-01-04T00:00:00+00:00",
            }
            if role != "pending":
                store.store_object(prefix + "/integrated.json", _payload(marker))
            stages.append(
                {
                    "role": role,
                    "stage_id": stage,
                    "handoff_sha256": _sha(manifest),
                    "marker_sha256": None if role == "pending" else _sha(_payload(marker)),
                    "receipt_sha256": receipt_sha,
                    "receipt_bytes": len(original),
                }
            )
            if role == "recovery":
                entries.append(
                    {
                        "market": True,
                        "source": source,
                        "stage_id": stage,
                        "manifest_sha256": _sha(manifest),
                        "started_at": started,
                        "asof": "2026-01-01",
                        "integration": marker,
                        "explicit_jpx_recovery": True,
                    }
                )
    descriptor = {"format": "edinet-input-v1", "selection_sha256": "f" * 64}
    identity = _sha(json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode())
    plan = (
        json.dumps(
            {
                "handoffs": entries,
                "external": [],
                "external_failed": [],
                "edinet_input": {
                    "kind": "native_l1",
                    "reference": "edinet-" + identity,
                    "sha256": identity,
                    "descriptor": descriptor,
                },
            },
            indent=2,
        )
        + "\n"
    )
    proof = {"etag": "1" * 32, "sha256": "2" * 64}
    before = {name: row_fingerprint([]) for name in MARKET_DATASETS}
    value = {
        "version": 1,
        "source_commit": "a" * 40,
        "asof": "2026-01-02",
        "plan_json": plan,
        "plan_sha256": _sha(plan.encode()),
        "baseline": {
            "current": proof,
            "machine_receipt": proof,
            "market_etag": "3" * 32,
            "runs_etag": "4" * 32,
            "economic_etag": "5" * 32,
            "lake_release_id": "baseline",
            "lake_manifest_sha256": "6" * 64,
        },
        "stages": stages,
        "before": before,
        "after_jpx": {name: value for name, value in before.items() if name.startswith("jpx.")},
        "jpx_coverage_before": row_fingerprint([]),
        "jpx_coverage_after": row_fingerprint([]),
        "edinet_coverage_before": row_fingerprint([]),
        "edinet_projection_clock_updates": 6,
        "edinet_archive_count": 2,
    }
    return value, store


def test_contract_preserves_original_plan_bytes_and_rejects_changed_hash(contract_store):
    value, _ = contract_store
    payload = _payload(value)
    result = decode_contract(payload, _sha(payload))
    assert result.plan_json.encode() == value["plan_json"].encode()
    with pytest.raises(ValueError, match="digest"):
        decode_contract(payload + b" ", _sha(payload))
    value["plan_json"] += " "
    with pytest.raises(ValueError, match="original plan"):
        RecoveryContract.model_validate(value)


@pytest.mark.parametrize(
    "fault",
    [
        "shell",
        "url",
        "duplicate_stage",
        "extra_handoff",
        "edinet_head",
        "missing_table",
        "replace_calendar",
    ],
)
def test_contract_refuses_scope_expansion(contract_store, fault):
    value, _ = contract_store
    if fault == "shell":
        value["command"] = "echo forbidden"
    elif fault == "url":
        value["stages"][0]["stage_id"] = "https://example.test/payload"
    elif fault == "duplicate_stage":
        value["stages"][1] = value["stages"][0]
    elif fault in {"extra_handoff", "edinet_head"}:
        plan = json.loads(value["plan_json"])
        if fault == "extra_handoff":
            plan["handoffs"].append(plan["handoffs"][0])
        else:
            plan["edinet_input"]["reference"] = "latest"
        value["plan_json"] = json.dumps(plan)
        value["plan_sha256"] = _sha(value["plan_json"].encode())
    elif fault == "missing_table":
        value["before"].pop("jpx.source_responses")
    else:
        value["after_jpx"]["jpx.earnings_calendar"] = {"count": 1, "sha256": "a" * 64}
    with pytest.raises(ValidationError):
        RecoveryContract.model_validate(value)


def test_contract_rejects_duplicate_json_keys_before_interpretation(contract_store):
    value, _ = contract_store
    payload = b'{"version":2,' + _payload(value)[1:]
    with pytest.raises(ValueError, match="duplicate"):
        decode_contract(payload, _sha(payload))


@pytest.mark.parametrize("bad", ["", "A" * 64, "../" + "f" * 61, "f" * 64 + "\n", "$(echo test)"])
def test_dispatch_shape_is_credential_free_and_finite(bad):
    assert validate_dispatch(["jpx-recovery", "--contract-sha256", bad]) == 2


def test_stage_verification_preserves_all_six_originals_and_pending_markers(
    tmp_path, contract_store
):
    value, store = contract_store
    contract = RecoveryContract.model_validate(value)
    original = {key: row.body for key, row in store.values.items()}
    recovery.verify_stages(store, contract, tmp_path)
    assert {key: row.body for key, row in store.values.items()} == original
    assert store.put_keys == []


@pytest.mark.parametrize(
    "fault", ["raw", "marker", "pending_sealed", "extra_stage", "already_recovered"]
)
def test_stage_verification_rejects_changed_or_extra_evidence(tmp_path, contract_store, fault):
    value, store = contract_store
    contract = RecoveryContract.model_validate(value)
    stage = value["stages"][0]
    prefix = "coordination/market-sources/" + stage["stage_id"]
    if fault == "raw":
        store.store_object(prefix + "/receipts-" + stage["receipt_sha256"] + ".sqlite", b"corrupt")
    elif fault == "marker":
        store.store_object(prefix + "/integrated.json", b"{}")
    elif fault == "pending_sealed":
        store.store_object(
            "coordination/market-sources/" + value["stages"][-1]["stage_id"] + "/integrated.json",
            b"{}",
        )
    elif fault == "extra_stage":
        store.store_object(
            prefix + "-extra/handoff.json", store.values[prefix + "/handoff.json"].body
        )
    else:
        store.store_object(prefix + "/recovered-" + stage["handoff_sha256"] + ".json", b"{}")
    with pytest.raises((ValueError, LakePublishError)):
        recovery.verify_stages(store, contract, tmp_path)
    assert store.put_keys == []


@pytest.fixture
def operation(tmp_path, monkeypatch, contract_store):
    value, store = contract_store
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setattr(recovery, "source_repo_root", lambda: tmp_path / "repo")
    monkeypatch.setattr(recovery, "verified_git_commit", lambda: "a" * 40)
    monkeypatch.setattr(recovery, "require_baseline", lambda *args: None)
    monkeypatch.setattr(recovery, "verify_stages", lambda *args: None)
    monkeypatch.setattr(
        recovery,
        "read_edinet_input",
        lambda *args: SimpleNamespace(archives=SimpleNamespace(archive_keys=[1, 2])),
    )
    monkeypatch.setattr(
        recovery.shutil, "disk_usage", lambda *args: SimpleNamespace(free=100 * 1024**3)
    )
    directory = tmp_path / "private"
    directory.mkdir()
    return recovery.Recovery(store, directory, "b" * 64, RecoveryContract.model_validate(value))


def test_all_phases_are_finite_and_readback_precedes_seal(operation, monkeypatch):
    calls = []

    def call(phase):
        calls.append(phase)
        if phase == "apply":
            (operation.integration / "applied.json").write_text("{}")

    for phase in recovery.PHASES:
        monkeypatch.setattr(operation, phase, lambda phase=phase: call(phase))
    operation.run()
    assert calls == list(recovery.PHASES)
    assert (
        calls.index("apply")
        < calls.index("models")
        < calls.index("push")
        < calls.index("readback")
        < calls.index("seal")
        < calls.index("release")
    )
    assert (operation.integration / "plan.json").read_text() == operation.contract.plan_json
    writes = operation.store.put_keys
    assert len(writes) == 1 + 2 * len(recovery.PHASES)
    assert all("/attempts/" in key and "/123-1/" in key for key in writes)
    with pytest.raises(ValueError, match="already attempted"):
        operation.run()


@pytest.mark.parametrize(
    ("failed_phase", "error", "release_safe"),
    [
        ("apply", ValueError("local verification"), True),
        ("prepare", subprocess.CalledProcessError(1, "private-transfer"), False),
        ("apply", R2OperationStopped("403"), False),
        ("models", ValueError("publication uncertain"), False),
        ("push", LakePublishError("unknown PUT"), False),
        ("readback", ValueError("rows differ"), False),
        ("seal", LakePublishError("unknown receipt PUT"), False),
    ],
)
def test_failure_never_replays_or_runs_later_phase(
    operation, monkeypatch, failed_phase, error, release_safe
):
    calls = []

    def call(phase):
        calls.append(phase)
        if phase == failed_phase:
            raise error
        if phase == "apply":
            (operation.integration / "applied.json").write_text("{}")

    for phase in recovery.PHASES:
        monkeypatch.setattr(operation, phase, lambda phase=phase: call(phase))
    monkeypatch.setattr(operation, "release_lease", lambda: calls.append("safe-failure-release"))
    with pytest.raises(type(error)):
        operation.run()
    expected = list(recovery.PHASES[: recovery.PHASES.index(failed_phase) + 1])
    assert calls == expected + (["safe-failure-release"] if release_safe else [])
    assert not (operation.directory / (failed_phase + "-succeeded.json")).exists()


def test_exact_apply_check_is_required_before_table_acceptance(operation, monkeypatch):
    monkeypatch.setattr(operation, "held", lambda: None)
    monkeypatch.setattr(recovery, "edinet_coverage_rows", lambda *args: [])
    monkeypatch.setattr(recovery.sources, "apply", lambda *args, **kwargs: {})
    monkeypatch.setattr(recovery.sources, "main", lambda args: 1)
    monkeypatch.setattr(
        operation, "require_tables", lambda: pytest.fail("failed apply cannot advance")
    )
    with pytest.raises(ValueError, match="check failed"):
        operation.apply()


def test_apply_clears_only_list_cache_against_original_fixed_input(operation, monkeypatch):
    calls = []
    fixed_lists = object()
    monkeypatch.setattr(operation, "held", lambda: None)
    monkeypatch.setattr(recovery, "edinet_coverage_rows", lambda *args: [])
    monkeypatch.setattr(recovery, "verify_edinet_coverage", lambda *args, **kwargs: 6)
    monkeypatch.setattr(recovery.sources, "main", lambda args: calls.append("check") or 0)
    monkeypatch.setattr(
        recovery.sources,
        "apply",
        lambda *args, **kwargs: {
            "handoffs": [
                {"entry": entry, "applied": True, "failed": False, "recovery": False}
                for entry in operation.contract.plan["handoffs"]
            ],
            "edinet_input": operation.contract.plan["edinet_input"],
        },
    )
    monkeypatch.setattr(operation, "require_tables", lambda: calls.append("tables"))

    def read_fixed(store, saved):
        assert saved.model_dump(mode="json") == operation.contract.plan["edinet_input"]
        calls.append("original-fixed-input")
        return SimpleNamespace(lists=fixed_lists)

    monkeypatch.setattr(recovery, "read_edinet_input", read_fixed)

    def clear(fixed, path):
        assert fixed is fixed_lists
        assert path == operation.market
        calls.append("verified-cache-clear")

    monkeypatch.setattr(recovery, "clear_verified_receipt_cache", clear)
    operation.apply()
    assert calls == ["check", "tables", "original-fixed-input", "verified-cache-clear"]


def test_success_receipts_preserve_reconciliation_evidence_before_models(operation, monkeypatch):
    operation.prior = {"generation_ids": ["prior"], "generations": {"prior": {"proof": "old"}}}
    applied = {"jpx_publication": {"jpx.source_bundles": {"sha256": "a" * 64}}}
    for phase in recovery.PHASES:

        def execute(phase=phase):
            if phase == "apply":
                (operation.integration / "applied.json").write_text(json.dumps(applied))
            if phase == "models":
                for name in ("prepare-succeeded.json", "apply-succeeded.json"):
                    assert operation.receipt_prefix + name in operation.store.values

        monkeypatch.setattr(operation, phase, execute)
    operation.run()
    prepared = json.loads(
        operation.store.values[operation.receipt_prefix + "prepare-succeeded.json"].body
    )
    source = json.loads(
        operation.store.values[operation.receipt_prefix + "apply-succeeded.json"].body
    )
    assert prepared["prior_generations"] == operation.prior
    assert source["applied"] == applied


@pytest.mark.parametrize("fault", ["source_commit", "rejection"])
def test_entry_stops_before_lease_with_safe_public_diagnostics(
    tmp_path, monkeypatch, contract_store, fault
):
    value, store = contract_store
    for key, entry in {
        "GITHUB_ACTIONS": "true",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_RUN_ID": "123",
        "GITHUB_REPOSITORY": "koumatsumoto/baibai-loop",
        "GITHUB_SHA": "a" * 40,
    }.items():
        monkeypatch.setenv(key, entry)
    monkeypatch.setattr(recovery, "verified_git_commit", lambda: "a" * 40)
    monkeypatch.setattr(recovery, "Boto3R2Store", lambda **kwargs: store)
    store.close = lambda: None
    directory = tmp_path / "operation"
    if fault == "source_commit":
        value["source_commit"] = "c" * 40
    payload = _payload(value)
    digest = _sha(payload)
    store.store_object(recovery.PREFIX + "contracts/" + digest + ".json", payload)
    if fault == "rejection":

        def reject(*args):
            (directory / "rejected").write_text("429\n")
            raise R2OperationStopped("private exception text must not reach summary")

        monkeypatch.setattr(recovery, "read_private", reject)
    assert recovery.main(["--contract-sha256", digest, "--directory", str(directory)]) == (
        75 if fault == "rejection" else 1
    )
    summary = json.loads((directory / "stopped.json").read_text())
    assert summary["phase"] == "validation"
    assert summary["lease_may_be_held"] is False
    assert summary["http_status"] == (429 if fault == "rejection" else None)
    assert set(summary) == {
        "phase",
        "state",
        "error_type",
        "rejection_latched",
        "http_status",
        "automatic_retry",
        "lease_may_be_held",
    }
    assert "private exception" not in json.dumps(summary)
    assert store.put_keys == []


def test_binary_readback_checks_entire_bytes_and_rejects_non_sqlite(tmp_path):
    store = MemoryR2Store()
    source = tmp_path / "source.sqlite"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE value (n INTEGER)")
        conn.execute("INSERT INTO value VALUES (1)")
    store.store_object("runs.sqlite", source.read_bytes())
    # The production reader's bounded download also passes max_bytes.
    original = store.download_file
    store.download_file = lambda key, path, **kwargs: original(
        key, path, expect_bytes=kwargs["expect_bytes"]
    )
    recovery.binary_readback(
        store, "runs.sqlite", tmp_path / "good.sqlite", store.head("runs.sqlite").etag
    )
    store.store_object("runs.sqlite", b"not SQLite")
    with pytest.raises(sqlite3.DatabaseError):
        recovery.binary_readback(
            store, "runs.sqlite", tmp_path / "bad.sqlite", store.head("runs.sqlite").etag
        )


def test_hydrate_and_push_cannot_freeze_a_new_edinet_list_head(tmp_path):
    market = tmp_path / "market.sqlite"
    with closing(open_connection(market)):
        pass
    recovery.require_empty_list_cache(market)
    with closing(open_connection(market)) as connection, connection:
        connection.execute(
            "INSERT INTO edinet_list_responses VALUES (?,?,?,?)",
            (
                "saved",
                "2026-01-01",
                "2026-01-01T00:00:00+00:00",
                b"private original",
            ),
        )
    with pytest.raises(ValueError, match="empty verified EDINET"):
        recovery.require_empty_list_cache(market)


def test_workflow_has_no_public_private_evidence_or_unrelated_publishers():
    root = Path(__file__).resolve().parents[3]
    path = root / ".github/workflows/cloud-jpx-recovery.yml"
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert set(workflow["on"]["workflow_dispatch"]["inputs"]) == {"contract_sha256"}
    job = workflow["jobs"]["recover"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["concurrency"] == {
        "group": "cloud-publish",
        "queue": "max",
        "cancel-in-progress": "false",
    }
    assert int(job["timeout-minutes"]) == 210
    assert workflow["permissions"] == {"contents": "read"}
    steps = job["steps"]
    privileged = [step for step in steps if "R2_ACCESS_KEY_ID" in step.get("env", {})]
    assert len(privileged) == 1
    assert privileged[0] == steps[-1]
    assert 'test "$GITHUB_RUN_ATTEMPT" = 1' in steps[1]["run"]
    assert 'jpx-recovery-private.log" 2>&1' in privileged[0]["run"]
    text = path.read_text()
    assert not any(
        word in text
        for word in (
            "upload-artifact",
            "economic_sources",
            "source-inputs",
            "portfolio",
            "provider",
        )
    )
