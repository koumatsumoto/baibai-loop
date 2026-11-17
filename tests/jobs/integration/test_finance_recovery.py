"""Finite inputs and failure boundaries for the exceptional schema52 recovery."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from compression import zstd
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.objects.r2 import LakeCASConflict, R2OperationStopped
from baibai_jobs.integration import finance_recovery as driver
from baibai_jobs.integration import finance_recovery_contract as contracts
from baibai_jobs.integration import finance_recovery_evidence as evidence
from baibai_jobs.integration.jpx_recovery_verification import MARKET_DATASETS


class Store(MemoryR2Store):
    def download_file(self, key, path, *, expect_bytes=None, max_bytes=None):
        body = self.values[key].body
        assert expect_bytes == len(body)
        assert max_bytes == len(body)
        path.write_bytes(body)
        self.get_keys.append(key)

    def close(self):
        pass


def fingerprint(n=1):
    return {"count": n, "sha256": "a" * 64}


def value():
    descriptor = {"format": "edinet-input-v1", "selection_sha256": "b" * 64}
    sha = hashlib.sha256(canonical_json(descriptor).encode()).hexdigest()
    ids = [f"generation-{i}" for i in range(13)]
    proof = {
        "sha256": "c" * 64,
        "size": 1,
        "decoded_sha256": "c" * 64,
        "decoded_size": 1,
        "encoding": "raw",
    }
    return {
        "version": 1,
        "asof": "2026-11-16",
        "market_schema": 52,
        "runs_schema": 8,
        "baseline": {
            "current": {"etag": "d" * 32, "sha256": "e" * 64},
            "machine_receipt": {"etag": "f" * 32, "sha256": "a" * 64},
            "market_etag": "b" * 32,
            "runs_etag": "c" * 32,
            "lake_release_id": "old-release",
            "lake_manifest_sha256": "d" * 64,
        },
        "files": {
            name: dict(proof) for name in contracts.REQUIRED_FILES | contracts.INTEGRATION_FILES
        },
        "market_tables": {name: fingerprint() for name in MARKET_DATASETS},
        "old_metrics": fingerprint(111213),
        "target_metrics": fingerprint(3974),
        "legacy_runs": {name: fingerprint() for name in ("screening_run", "stock_analysis")},
        "prior_generations": {
            "generation_ids": ids,
            "generations": {
                name: {
                    table: fingerprint()
                    for table in ("model_generation", "generated_model", "screening_selection")
                }
                for name in ids
            },
        },
        "edinet_input": {
            "kind": "native_l1",
            "reference": "edinet-" + sha,
            "sha256": sha,
            "descriptor": descriptor,
        },
    }


def test_unreviewed_contract_is_rejected_by_default():
    assert len(contracts.APPROVED_CONTRACTS) == 1
    payload = json.dumps(value()).encode()
    with pytest.raises(ValueError, match="allowlist"):
        contracts.decode_contract(payload, hashlib.sha256(payload).hexdigest())


def test_approved_digest_and_exact_input_scope(monkeypatch):
    payload = json.dumps(value()).encode()
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(contracts, "APPROVED_CONTRACTS", frozenset({digest}))
    assert contracts.decode_contract(payload, digest).asof == "2026-11-16"
    with pytest.raises(ValueError, match="digest"):
        contracts.decode_contract(payload + b" ", digest)


@pytest.mark.parametrize("fault", ["path", "extra", "missing", "raw", "count", "prior", "edinet"])
def test_contract_rejects_expanded_or_changed_scope(fault):
    data = value()
    if fault == "path":
        data["files"]["../../secrets"] = data["files"].pop("market.sqlite")
    elif fault == "extra":
        data["command"] = "anything"
    elif fault == "missing":
        del data["files"]["integration/receipt-00016/result.json"]
    elif fault == "raw":
        data["files"]["market.sqlite"]["decoded_sha256"] = "d" * 64
    elif fault == "count":
        data["target_metrics"]["count"] = 3970
    elif fault == "prior":
        data["prior_generations"]["generation_ids"][1] = "generation-0"
    else:
        data["edinet_input"]["descriptor"]["selection_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="validation error"):
        contracts.FinanceContract.model_validate(data)


@pytest.mark.parametrize("encoding", ["raw", "zstd"])
def test_fixed_file_download_verifies_wire_and_decoded_identities(tmp_path, encoding):
    body = b"fixed input content" * 10
    wire = body if encoding == "raw" else zstd.compress(body)
    proof = contracts.InputFile(
        sha256=hashlib.sha256(wire).hexdigest(),
        size=len(wire),
        encoding=encoding,
        decoded_sha256=hashlib.sha256(body).hexdigest(),
        decoded_size=len(body),
    )
    store = Store()
    store.store_object(proof.key, wire)
    target = tmp_path / "fixed"
    driver.fetch_input(store, proof, target)
    assert target.read_bytes() == body
    assert not target.with_name("fixed.wire").exists()
    assert store.put_keys == []


def test_decompression_bomb_stops_at_contract_size(tmp_path):
    body = b"x" * 100_000
    wire = zstd.compress(body)
    proof = contracts.InputFile(
        sha256=hashlib.sha256(wire).hexdigest(),
        size=len(wire),
        encoding="zstd",
        decoded_sha256=hashlib.sha256(body).hexdigest(),
        decoded_size=10,
    )
    store = Store()
    store.store_object(proof.key, wire)
    with pytest.raises(ValueError, match="exceeds"):
        driver.fetch_input(store, proof, tmp_path / "fixed")
    assert not store.put_keys


def test_immutable_pair_snapshot_is_fully_recoverable(tmp_path):
    store = Store()
    body = tmp_path / "runs.sqlite"
    body.write_bytes(b"durable snapshot")
    proof = evidence.immutable_file(store, body, tmp_path)
    assert store.values[proof["key"]].body == body.read_bytes()
    assert store.get_keys == [proof["key"]]
    assert not list(tmp_path.glob("*.readback"))
    assert evidence.immutable_file(store, body, tmp_path) == proof
    assert len(store.put_keys) == 1


def test_unknown_evidence_put_is_not_retried(tmp_path):
    store = Store()
    path = tmp_path / "runs"
    path.write_bytes(b"snapshot")
    original = store.put_file

    def lost_ack(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("connection lost after PUT")

    store.put_file = lost_ack
    with pytest.raises(OSError, match="connection lost"):
        evidence.immutable_file(store, path, tmp_path)
    assert len(store.put_keys) == 1
    assert store.get_keys == []
    assert path.read_bytes() == b"snapshot"


def test_evidence_corrupt_readback_never_acknowledged(tmp_path):
    store = Store()
    source = tmp_path / "runs"
    source.write_bytes(b"snapshot")

    def bad_read(key, path, **kwargs):
        path.write_bytes(b"corrupt")

    store.download_file = bad_read
    with pytest.raises(ValueError, match="readback"):
        evidence.immutable_file(store, source, tmp_path)


def operation(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(driver, "source_repo_root", lambda: tmp_path)
    monkeypatch.setattr(driver, "verified_git_commit", lambda: "f" * 40)
    return driver.FinanceRecovery(
        Store(), tmp_path, "a" * 64, contracts.FinanceContract.model_validate(value())
    )


def test_complete_state_machine_order(tmp_path, monkeypatch):
    op = operation(tmp_path, monkeypatch)
    events = []
    monkeypatch.setattr(op, "record", lambda phase, state, **kw: events.append((phase, state)))
    for phase in driver.PHASES:
        monkeypatch.setattr(op, phase, lambda p=phase: events.append((p, "execute")))
    op.run()
    assert events == [("attempt", "started")] + [
        (phase, state) for phase in driver.PHASES for state in ("started", "execute", "succeeded")
    ]


@pytest.mark.parametrize("failed", driver.PHASES[:-1])
@pytest.mark.parametrize("error", [ValueError, OSError, LakeCASConflict, R2OperationStopped])
def test_failure_never_pushes_or_releases_automatically(tmp_path, monkeypatch, failed, error):
    op = operation(tmp_path, monkeypatch)
    events = []
    monkeypatch.setattr(op, "record", lambda *a, **kw: None)
    for phase in driver.PHASES:

        def step(p=phase):
            events.append(p)
            if p == failed:
                raise error("injected")

        monkeypatch.setattr(op, phase, step)
    with pytest.raises(error):
        op.run()
    assert events == list(driver.PHASES[: driver.PHASES.index(failed) + 1])
    assert "release" not in events


def test_capacity_is_real_free_space_before_acquire(tmp_path, monkeypatch):
    op = operation(tmp_path, monkeypatch)
    monkeypatch.setattr(
        driver.shutil, "disk_usage", lambda _: SimpleNamespace(free=driver.MINIMUM_FREE_BYTES - 1)
    )
    with pytest.raises(ValueError, match="capacity"):
        op.capacity()
    assert not op.store.put_keys


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GITHUB_RUN_ATTEMPT", "2"),
        ("GITHUB_REF", "refs/heads/other"),
        ("GITHUB_EVENT_NAME", "pull_request"),
    ],
)
def test_untrusted_or_rerun_stops_before_credentials(tmp_path, monkeypatch, name, value):
    for k, v in {
        "GITHUB_ACTIONS": "true",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REPOSITORY": "koumatsumoto/baibai-loop",
        "GITHUB_SHA": "c" * 40,
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv(name, value)
    monkeypatch.setattr(driver, "verified_git_commit", lambda: "c" * 40)
    monkeypatch.setattr(driver, "Boto3R2Store", lambda **kw: pytest.fail("credentials accessed"))
    assert driver.main(["--contract-sha256", "a" * 64, "--directory", str(tmp_path / "run")]) == 1
    assert not (tmp_path / "run").exists()


def test_transfer_hook_is_opt_in_and_rejects_arbitrary_prefix(tmp_path, monkeypatch):
    monkeypatch.delenv("FINANCE_RECOVERY_EVIDENCE_PREFIX", raising=False)
    evidence.persist_transfer_record(tmp_path, tmp_path / "absent")
    monkeypatch.setenv("FINANCE_RECOVERY_EVIDENCE_PREFIX", "other/private/")
    with pytest.raises(ValueError, match="prefix"):
        evidence.persist_transfer_record(tmp_path, tmp_path / "absent")


def pointer_ready(op):
    pointer = {"release_id": "new-release", "manifest_key": "unused", "manifest_sha256": "e" * 64}
    body = json.dumps(pointer).encode()
    receipt = {
        "pointer_sha256": hashlib.sha256(body).hexdigest(),
        "generation_id": "models-new-release",
        "contract_sha256": op.digest,
        "release_id": "new-release",
        "manifest_sha256": "e" * 64,
        "selection_id": "selection-new",
        "expected_previous_pointer_etag": op.contract.baseline.current.etag,
    }
    op.notice = {
        "models_ready": True,
        "generation_id": "models-new-release",
        "selection_id": "selection-new",
        "lake": {"release_id": "new-release", "release_manifest_sha256": "e" * 64},
    }
    op.store.store_object(op.prepared_key, json.dumps(receipt).encode())
    op.store.store_object(driver.current_release_pointer_key(), body)


def test_own_pointer_requires_durable_contract_bound_exact_winner(tmp_path, monkeypatch):
    op = operation(tmp_path, monkeypatch)
    pointer_ready(op)
    op.own_pointer()
    op.store.store_object(driver.current_release_pointer_key(), b'{"release_id":"old-winner"}')
    with pytest.raises(ValueError, match="exact durable"):
        op.own_pointer()


def test_missing_durable_checkpoint_blocks_pair(tmp_path, monkeypatch):
    op = operation(tmp_path, monkeypatch)
    pointer_ready(op)
    del op.store.values[op.prepared_key]
    op.local_proof = {"accepted": True}
    monkeypatch.setattr(op, "held", lambda *a: None)
    monkeypatch.setattr(op, "transfer", lambda *a, **k: pytest.fail("pair write attempted"))
    with pytest.raises(ValueError, match="checkpoint absent"):
        op.push()


def test_prepared_contract_cannot_be_reexecuted(tmp_path, monkeypatch):
    op = operation(tmp_path, monkeypatch)
    pointer_ready(op)
    with pytest.raises(ValueError, match="already prepared"):
        op.run()
    assert not op.store.put_keys


def test_pair_snapshot_and_ack_receipts_are_private_and_synchronous(tmp_path, monkeypatch):
    store = Store()
    monkeypatch.setattr(evidence, "Boto3R2Store", lambda **k: store)
    prefix = contracts.PREFIX + "a" * 64 + "/attempts/123-1/"
    monkeypatch.setenv("FINANCE_RECOVERY_EVIDENCE_PREFIX", prefix)
    monkeypatch.setenv("R2_STOP_ON_REJECTION", "1")
    body = tmp_path / "runs.sqlite"
    body.write_bytes(b"committed runs")
    record = tmp_path / "runs.sqlite.snapshot_ready.json"
    record.write_text(json.dumps({"phase": "snapshot_ready", "body": body.name}))
    evidence.persist_transfer_record(tmp_path, record)
    receipt = json.loads(store.values[prefix + "pair/" + record.name].body)
    assert store.values[receipt["durable_body"]["key"]].body == body.read_bytes()
    ack = tmp_path / "runs.sqlite.put_ack.json"
    ack.write_text(json.dumps({"phase": "put_ack", "put_response_etag": "e" * 32}))
    evidence.persist_transfer_record(tmp_path, ack)
    assert (
        json.loads(store.values[prefix + "pair/" + ack.name].body)["put_response_etag"] == "e" * 32
    )
    with pytest.raises(LakeCASConflict):
        evidence.persist_transfer_record(tmp_path, ack)


def test_workflow_has_no_unconditional_release_or_public_artifacts():
    text = (Path(__file__).parents[3] / ".github/workflows/cloud-finance-recovery.yml").read_text()
    assert "contents: read" in text
    assert "cancel-in-progress: false" in text
    assert "refs/heads/main" in text
    assert "upload-artifact" not in text
    assert "always()" not in text
    assert "lifecycle" not in text.lower()
    assert "source apply" not in text


def test_preparation_failure_does_not_claim_publication(tmp_path, monkeypatch):
    op = operation(tmp_path, monkeypatch)
    monkeypatch.setattr(op, "prepare", lambda: (_ for _ in ()).throw(ValueError("capacity")))
    with pytest.raises(ValueError, match="capacity"):
        op.run()
    assert contracts.PREFIX + op.digest + "/attempt.json" not in op.store.values
    assert op.prefix + "prepare-failed.json" in op.store.values
    assert not op.handle_path.exists()


def test_publication_claim_occurs_after_final_input_and_capacity_gates(tmp_path, monkeypatch):
    op = operation(tmp_path, monkeypatch)
    seen = []
    monkeypatch.setattr(op, "baseline", lambda: seen.append("baseline"))
    monkeypatch.setattr(op, "capacity", lambda: seen.append("capacity"))
    original = driver.immutable_json

    def record_claim(*args):
        seen.append("claim")
        original(*args)

    monkeypatch.setattr(driver, "immutable_json", record_claim)

    def stop_after_claim(*args):
        seen.append("acquire")
        raise ValueError("stop before lease write")

    monkeypatch.setattr(driver.lease, "acquire", stop_after_claim)
    with pytest.raises(ValueError, match="stop before lease"):
        op.acquire()
    assert seen == ["baseline", "capacity", "claim", "acquire"]
    assert contracts.PREFIX + op.digest + "/attempt.json" in op.store.values
