"""Single trusted Actions attempt; unknown outcomes require reconciliation, never retry."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess  # nosec B404
import tempfile
from contextlib import closing, suppress
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from compression import zstd

from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialPolicy,
)
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.json import read_private
from baibai_engine.storage.objects.r2 import Boto3R2Store, R2OperationStopped
from baibai_engine.storage.paths import INDICATORS_DB_PATH, MARKET_DB_PATH, RUNS_DB_PATH
from baibai_engine.storage.snapshots.identity import source_repo_root, verified_git_commit
from baibai_engine.storage.snapshots.keys import current_release_pointer_key
from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    LakeStoreOrigin,
    read_lake_store_origin,
)
from baibai_jobs.integration import models, sources
from baibai_jobs.integration.edinet_metric_acceptance import verify_acceptance
from baibai_jobs.integration.finance_recovery_contract import (
    PREFIX,
    FinanceContract,
    InputFile,
    decode_contract,
)
from baibai_jobs.integration.finance_recovery_evidence import digest_file, immutable_json
from baibai_jobs.integration.jpx_recovery import small_object
from baibai_jobs.integration.jpx_recovery_verification import (
    prior_generations,
    row_fingerprint,
    snapshot_market,
    verify_lake,
    verify_models,
)
from baibai_jobs.publication import lease

GIB = 1024**3
# Free space remaining after installed fixed inputs, not total disk capacity.
# Covers full market backup+VACUUM/journal, 4GB spill, durable copy readback,
# compact runs/evidence/readback and a separate >8GB operating reserve.
MINIMUM_FREE_BYTES = 32 * GIB
PHASES = ("prepare", "acquire", "models", "push", "readback", "seal", "release")


def sqlite_check(path: Path, schema: int) -> None:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        if (
            connection.execute("PRAGMA user_version").fetchone()[0] != schema
            or connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]
            or connection.execute("PRAGMA foreign_key_check").fetchone() is not None
        ):
            raise ValueError("fixed SQLite schema or integrity differs")


def metric_fingerprints(path: Path, asof: str) -> dict[str, Any]:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        return {
            label: row_fingerprint(
                connection.execute(
                    "SELECT * FROM edinet_metrics WHERE asof_date " + comparison + " ?",  # nosec B608
                    (asof,),
                )
            )
            for label, comparison in (("old", "<"), ("target", "="))
        }


def legacy_fingerprints(path: Path) -> dict[str, Any]:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        return {
            table: row_fingerprint(connection.execute("SELECT * FROM " + table))  # nosec B608
            for table in ("screening_run", "stock_analysis")
        }


def fetch_input(store: Boto3R2Store, proof: InputFile, destination: Path) -> None:
    if destination.exists():
        raise ValueError("fixed input destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    wire = destination.with_name(destination.name + ".wire")
    before = store.head(proof.key)
    if before is None or before.size != proof.size or before.metadata.get("sha256") != proof.sha256:
        raise ValueError("fixed input metadata differs")
    store.download_file(proof.key, wire, expect_bytes=proof.size, max_bytes=proof.size)
    if digest_file(wire) != proof.sha256 or store.head(proof.key) != before:
        raise ValueError("fixed input changed or failed full readback")
    if proof.encoding == "raw":
        wire.replace(destination)
    else:
        count = 0
        with zstd.open(wire, "rb") as source, destination.open("xb") as output:
            while chunk := source.read(8 * 1024 * 1024):
                count += len(chunk)
                if count > proof.decoded_size:
                    raise ValueError("decoded input exceeds approved size")
                output.write(chunk)
        wire.unlink()
    if (
        destination.stat().st_size != proof.decoded_size
        or digest_file(destination) != proof.decoded_sha256
    ):
        raise ValueError("decoded input identity differs")


class FinanceRecovery:
    def __init__(
        self, store: Boto3R2Store, directory: Path, digest: str, contract: FinanceContract
    ) -> None:
        self.store, self.directory, self.digest, self.contract = store, directory, digest, contract
        self.root = source_repo_root()
        self.market, self.runs, self.economic = (
            self.root / name for name in (MARKET_DB_PATH, RUNS_DB_PATH, INDICATORS_DB_PATH)
        )
        self.integration = directory / "integration"
        self.handle_path = directory / "lease.json"
        self.mirror = directory / "mirror"
        self.prefix = PREFIX + digest + "/attempts/" + os.environ["GITHUB_RUN_ID"] + "-1/"
        self.phase = "preflight"
        self.notice: dict[str, Any] = {}
        self.local_proof: dict[str, Any] = {}
        self.published: dict[str, Any] = {}
        self.model_started = self.model_finished = datetime.now(UTC)
        self.conditions = ""
        self.prepared_key = PREFIX + digest + "/prepared.json"

    def record(self, phase: str, state: str, **details: Any) -> None:
        value = {
            "phase": phase,
            "state": state,
            "at": datetime.now(UTC).isoformat(),
            "run_id": os.environ["GITHUB_RUN_ID"],
            "job": os.environ.get("GITHUB_JOB", "recover"),
            "attempt": 1,
            "commit": verified_git_commit(),
            "contract_sha256": self.digest,
            **details,
        }
        name = phase + "-" + state + ".json"
        write_json_atomic(self.directory / name, value)
        immutable_json(self.store, self.prefix + name, value)

    def baseline(self) -> None:
        baseline = self.contract.baseline
        for key, proof in (
            (current_release_pointer_key(), baseline.current),
            ("machine-manifest.json", baseline.machine_receipt),
        ):
            if small_object(self.store, key)[1] != proof.model_dump():
                raise ValueError("fixed baseline pointer or machine manifest changed")
        for key, etag in (
            ("market.sqlite", baseline.market_etag),
            ("runs.sqlite", baseline.runs_etag),
        ):
            remote = self.store.head(key)
            if remote is None or remote.etag != etag:
                raise ValueError("fixed machine generation changed")
        if sources.canonical_generations(self.store) != {
            "lake_release_id": baseline.lake_release_id,
            "lake_manifest_sha256": baseline.lake_manifest_sha256,
        }:
            raise ValueError("baseline machine pair is not coherent")

    def capacity(self, *, extra: int = 0, required: int = MINIMUM_FREE_BYTES) -> dict[str, int]:
        paths = [self.root, self.directory, Path(os.environ["TMPDIR"])]
        devices = {path.stat().st_dev for path in paths}
        if len(devices) != 1:
            raise ValueError("recovery budget requires one measured scratch filesystem")
        free = shutil.disk_usage(self.root).free
        if free < required + extra:
            raise ValueError("insufficient measured recovery scratch capacity")
        return {
            "device": paths[0].stat().st_dev,
            "free_bytes": free,
            "required_free_bytes": required + extra,
        }

    def held(self, remaining: int = 0) -> None:
        handle = lease._load_handle(self.handle_path)
        lease.verify_held(self.store, handle)
        if (handle.expires_at_utc - datetime.now(UTC)).total_seconds() < remaining:
            raise ValueError("insufficient publication time remaining")

    def transfer(self, action: str, *, evidence: bool = False) -> None:
        if action not in {"pull-machine", "push-machine"}:
            raise ValueError("unsupported finance transfer")
        args = ["bash", "jobs/scripts/r2_transfer.sh", action]
        if evidence:
            args += ["--evidence-dir", str(self.directory / "pair-evidence")]
        subprocess.run(
            args,
            cwd=self.root,
            check=True,
            env={  # nosec B603 B607
                **os.environ,
                "PUBLICATION_LEASE_HANDLE": str(self.handle_path),
                "FINANCE_RECOVERY_EVIDENCE_PREFIX": self.prefix if evidence else "",
            },
        )

    def require_financials(self) -> None:
        observed = metric_fingerprints(self.market, self.contract.asof)
        if observed != {
            "old": self.contract.old_metrics.model_dump(),
            "target": self.contract.target_metrics.model_dump(),
        }:
            raise ValueError("full financial rows changed")
        if snapshot_market(self.market) != {
            key: value.model_dump() for key, value in self.contract.market_tables.items()
        }:
            raise ValueError("accepted non-native market dataset changed")

    def prepare(self) -> None:
        self.baseline()
        if any(path.exists() for path in (self.market, self.runs, self.economic)):
            raise ValueError("fresh isolated checkout required")
        extra = sum(item.decoded_size for item in self.contract.files.values())
        extra += max(item.size for item in self.contract.files.values())
        self.capacity(extra=extra)
        # Pull fresh remote sidecars first; the sealed market retains the exact old origin.
        self.transfer("pull-machine")
        self.baseline()
        if digest_file(self.runs) != self.contract.files["runs.sqlite"].decoded_sha256:
            raise ValueError("fresh remote runs differs from approved13-generation baseline")
        for name, proof in self.contract.files.items():
            if name == "runs.sqlite":
                if self.runs.stat().st_size != proof.decoded_size:
                    raise ValueError("fixed runs size differs")
                continue
            target = self.directory / name
            fetch_input(self.store, proof, target)
            if name in {"market.sqlite", "economic.sqlite"}:
                installed = self.market if name == "market.sqlite" else self.economic
                installed.parent.mkdir(parents=True, exist_ok=True)
                target.replace(installed)
        sqlite_check(self.market, 52)
        sqlite_check(self.runs, 8)
        wanted = self.contract.baseline
        if read_lake_store_origin(self.market) != LakeStoreOrigin(
            wanted.lake_release_id, wanted.lake_manifest_sha256
        ):
            raise ValueError("sealed market origin differs from fixed old publication")
        if prior_generations(self.runs) != self.contract.prior_generations:
            raise ValueError("prior generation full rows differ")
        if legacy_fingerprints(self.runs) != {
            k: v.model_dump() for k, v in self.contract.legacy_runs.items()
        }:
            raise ValueError("legacy rows differ")
        self.require_financials()
        plan = json.loads((self.integration / "plan.json").read_bytes())
        report = json.loads((self.integration / "applied.json").read_bytes())
        if (
            plan["edinet_input"] != self.contract.edinet_input
            or report["edinet_input"] != self.contract.edinet_input
        ):
            raise ValueError("fixed EDINET identity differs")
        if sources.main(["check", "--directory", str(self.integration)]) != 0:
            raise ValueError("completed source report is not acceptable")
        self.conditions = (self.directory / "conditions.json").read_text()
        verify_acceptance(
            raw=self.conditions, report=report, database=self.market, store=self.store
        )
        self.baseline()
        self.capacity()

    def acquire(self) -> None:
        self.baseline()
        self.capacity()
        # Input validation and the complete disk budget precede the irreversible
        # publication claim. A claim is never cleared to retry a lost attempt.
        immutable_json(
            self.store,
            PREFIX + self.digest + "/attempt.json",
            {
                "contract_sha256": self.digest,
                "run_id": os.environ["GITHUB_RUN_ID"],
                "attempt": 1,
                "commit": verified_git_commit(),
            },
        )
        handle, _ = lease.acquire(self.store, "daily")
        lease._save_handle(self.handle_path, handle)
        self.held(210 * 60)
        self.baseline()
        self.record(
            "lease",
            "held",
            handle_sha256=digest_file(self.handle_path),
            owner=handle.owner,
            expires=handle.expires_at_utc.isoformat(),
        )

    def models(self) -> None:
        from baibai_jobs.publication.model_checkpoint import ModelPublicationCheckpoint

        self.held(180 * 60)
        self.baseline()
        self.capacity()
        self.model_started = datetime.now(UTC)
        checkpoint = ModelPublicationCheckpoint(
            contract_sha256=self.digest,
            directory=self.directory / "model-checkpoint",
            prior_generation_ids=tuple(self.contract.prior_generations["generation_ids"]),
            prior_fingerprints=self.contract.prior_generations,
            legacy_fingerprints={k: v.model_dump() for k, v in self.contract.legacy_runs.items()},
            minimum_remaining_seconds=1200,
        )
        self.notice = models.integrate_models(
            store=self.store,
            market=self.market,
            runs=self.runs,
            economic=self.economic,
            mirror=self.mirror,
            as_of=date.fromisoformat(self.contract.asof),
            lease_handle=self.handle_path,
            growth_start=None,
            policy=TechnicalFinancialPolicy(),
            edinet_input=InputSnapshot.model_validate(self.contract.edinet_input),
            preserve_generation_ids=tuple(self.contract.prior_generations["generation_ids"]),
            publication_checkpoint=checkpoint,
        )
        self.model_finished = datetime.now(UTC)
        write_json_atomic(self.directory / "notice.json", self.notice)
        self.require_financials()
        self.local_proof = self.model_proof(self.runs)
        self.own_pointer()

    def model_proof(self, path: Path) -> dict[str, Any]:
        sqlite_check(path, 8)
        if legacy_fingerprints(path) != {
            k: v.model_dump() for k, v in self.contract.legacy_runs.items()
        }:
            raise ValueError("legacy runs were modified")
        return verify_models(
            path,
            self.contract.prior_generations,
            self.notice,
            self.contract.edinet_input,
            self.model_started,
            self.model_finished,
            self.store,
        )

    def own_pointer(self) -> None:
        if self.notice.get("models_ready") is not True:
            raise ValueError("model did not complete")
        saved = read_private(self.store, self.prepared_key)
        if saved is None:
            raise ValueError("durable matching-generation checkpoint absent")
        proof = json.loads(saved.payload)
        raw, identity = small_object(self.store, current_release_pointer_key())
        pointer = json.loads(raw)
        lake = self.notice["lake"]
        if (
            identity["sha256"] != proof["pointer_sha256"]
            or pointer["release_id"] != lake["release_id"]
            or pointer["manifest_sha256"] != lake["release_manifest_sha256"]
            or proof["generation_id"] != self.notice["generation_id"]
            or proof["release_id"] != lake["release_id"]
            or proof["manifest_sha256"] != lake["release_manifest_sha256"]
            or proof["selection_id"] != self.notice["selection_id"]
            or proof["contract_sha256"] != self.digest
            or proof["expected_previous_pointer_etag"] != self.contract.baseline.current.etag
        ):
            raise ValueError("publication is not this attempt's exact durable generation")

    def push(self) -> None:
        if not self.local_proof:
            raise ValueError("local model acceptance absent")
        self.held(15 * 60)
        self.own_pointer()
        # The sealed SQLite context has ended. Retain no duplicate hydrated baseline.
        self.capacity(required=24 * GIB)
        self.transfer("push-machine", evidence=True)
        self.own_pointer()

    def generations(self) -> dict[str, str]:
        return {
            "lake_release_id": self.notice["lake"]["release_id"],
            "lake_manifest_sha256": self.notice["lake"]["release_manifest_sha256"],
        }

    def pair(self) -> dict[str, Any]:
        self.own_pointer()
        if sources.canonical_generations(self.store) != self.generations():
            raise ValueError("machine pair is not coherent with own generation")
        raw, proof = small_object(self.store, "machine-manifest.json")
        return {"value": json.loads(raw), "identity": proof}

    def readback(self) -> None:
        self.held()
        self.published = self.pair()
        self.capacity(required=12 * GIB)
        checkout = self.directory / "readback-checkout"
        subprocess.run(
            [
                "git",
                "worktree",
                "add",
                "--detach",
                str(checkout),  # nosec B603 B607
                verified_git_commit(),
            ],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ["bash", "jobs/scripts/r2_transfer.sh", "pull-machine"],  # nosec B603 B607
            cwd=checkout,
            check=True,
            env={
                **os.environ,
                "R2_GENERATION_DIR": str(self.directory / "readback-etags"),
                "R2_LAKE_MIRROR": str(self.directory / "readback-mirror"),
                "FINANCE_RECOVERY_EVIDENCE_PREFIX": "",
            },
        )
        target = self.directory / "readback"
        target.mkdir()
        for name, relative in (("market.sqlite", MARKET_DB_PATH), ("runs.sqlite", RUNS_DB_PATH)):
            (checkout / relative).replace(target / name)
        evidence = self.directory / "pair-evidence"
        for key in ("market.sqlite", "runs.sqlite"):
            ack = json.loads((evidence / (key + ".put_ack.json")).read_bytes())
            etag = self.published["value"]["keys"][key]
            if etag != ack["put_response_etag"]:
                raise ValueError("machine manifest does not bind actual PUT acknowledgements")
            remote = self.store.head(key)
            if remote is None or remote.etag != etag or digest_file(target / key, "md5") != etag:
                raise ValueError("remote key changed during canonical pair pull")
            if (
                digest_file(target / key) != ack["sha256"]
                or (target / key).stat().st_size != ack["size"]
            ):
                raise ValueError("full remote bytes differ from transmitted snapshot")
        sqlite_check(target / "market.sqlite", 52)
        if read_lake_store_origin(target / "market.sqlite") != LakeStoreOrigin(
            release_id=self.generations()["lake_release_id"],
            release_manifest_sha256=self.generations()["lake_manifest_sha256"],
        ):
            raise ValueError("published thin market origin differs")
        if self.model_proof(target / "runs.sqlite") != self.local_proof:
            raise ValueError("remote accepted generation differs")
        verify_lake(
            self.store,
            target / "lake",
            self.generations(),
            {key: value.model_dump() for key, value in self.contract.market_tables.items()},
        )
        if self.pair() != self.published:
            raise ValueError("publication changed during acceptance")
        self.held()

    def seal(self) -> None:
        self.held()
        if not self.published or self.pair() != self.published:
            raise ValueError("full pair acceptance absent or stale")
        if (
            sources.main(
                [
                    "seal",
                    "--directory",
                    str(self.integration),
                    "--lease-handle",
                    str(self.handle_path),
                ]
            )
            != 0
        ):
            raise ValueError("source seal failed")
        if self.pair() != self.published:
            raise ValueError("pair changed during source seal")

    def release(self) -> None:
        if not self.published or self.pair() != self.published:
            raise ValueError("cannot release before accepted publication")
        self.held()
        handle = lease._load_handle(self.handle_path)
        lease.release(self.store, handle)
        observed = lease._generation(self.store)
        if observed is None or observed[0].state != "released" or observed[0].owner != handle.owner:
            raise ValueError("release acknowledgement uncertain")

    def run(self) -> None:
        claim = PREFIX + self.digest + "/attempt.json"
        if self.store.head(claim) is not None:
            raise ValueError("contract already claimed; reconciliation required")
        if self.store.head(self.prefix + "attempt-started.json") is not None:
            raise ValueError("attempt already started; reconciliation required")
        if self.store.head(self.prepared_key) is not None:
            raise ValueError("contract already prepared; reconciliation required")
        self.record("attempt", "started")
        for phase in PHASES:
            self.phase = phase
            self.record(phase, "started")
            try:
                getattr(self, phase)()
            except BaseException as error:
                # Preparation has not acquired authority or touched production keys.
                # Record once when possible, never reconnect after a latched denial.
                if (
                    phase == "prepare"
                    and not isinstance(error, R2OperationStopped)
                    and not (self.directory / "rejected").exists()
                ):
                    # Preserve the original failure; no second network attempt.
                    with suppress(BaseException):
                        self.record(phase, "failed", error_type=type(error).__name__)
                raise
            details = (
                {"notice": self.notice, "model_proof": self.local_proof}
                if phase == "models"
                else {}
            )
            if phase == "readback":
                details = {"publication": self.published, "model_proof": self.local_proof}
            self.record(phase, "succeeded", **details)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract-sha256", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args(argv)
    store = None
    operation = None
    owned = False
    try:
        if (
            re.fullmatch(r"[0-9a-f]{64}", args.contract_sha256) is None
            or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("GITHUB_RUN_ATTEMPT") != "1"
            or re.fullmatch(r"[0-9]+", os.environ.get("GITHUB_RUN_ID", "")) is None
            or os.environ.get("GITHUB_REPOSITORY") != "koumatsumoto/baibai-loop"
            or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or verified_git_commit() != os.environ.get("GITHUB_SHA")
        ):
            raise ValueError("first trusted main Actions dispatch required")
        from baibai_jobs.integration.finance_recovery_contract import APPROVED_CONTRACTS

        if args.contract_sha256 not in APPROVED_CONTRACTS:
            raise ValueError("unapproved finance input contract")
        os.umask(0o077)
        args.directory.mkdir(parents=True, exist_ok=False)
        owned = True
        temporary = args.directory / "tmp"
        temporary.mkdir()
        os.environ.update(
            R2_STOP_ON_REJECTION="1",
            R2_REJECTION_STOP_FILE=str(args.directory / "rejected"),
            AWS_MAX_ATTEMPTS="1",
            AWS_RETRY_MODE="standard",
            AWS_EC2_METADATA_DISABLED="true",
            BAIBAI_LAKE_QUERY_PROFILE="small-runner",
            TMPDIR=str(temporary),
            R2_GENERATION_DIR=str(args.directory / "etags"),
            R2_LAKE_MIRROR=str(args.directory / "mirror"),
        )
        tempfile.tempdir = str(temporary)
        store = Boto3R2Store(bucket="baibai-stores")
        saved = read_private(store, PREFIX + "contracts/" + args.contract_sha256 + ".json")
        if saved is None:
            raise ValueError("private approved contract missing")
        contract = decode_contract(saved.payload, args.contract_sha256)
        operation = FinanceRecovery(store, args.directory, args.contract_sha256, contract)
        operation.run()
        return 0
    except BaseException as error:
        # No cleanup network call, automatic release, retry, or fallback credentials.
        if owned:
            write_json_atomic(
                args.directory / "stopped.json",
                {
                    "state": "reconciliation_required",
                    "phase": operation.phase if operation else "validation",
                    "error_type": type(error).__name__,
                    "automatic_retry": False,
                    "rejection_latched": (args.directory / "rejected").exists(),
                },
            )
        print("Finance recovery stopped; reconcile private receipts and exact run before any retry")
        return 75 if isinstance(error, R2OperationStopped) else 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
