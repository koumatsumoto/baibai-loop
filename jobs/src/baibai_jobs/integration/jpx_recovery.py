"""One fresh runner, one immutable contract, one forward-only recovery attempt."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess  # nosec B404
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.edinet_inputs import read_edinet_input
from baibai_engine.machine_analysis.screening.policies.economic_paths import EconomicPathPolicy
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.json import put_private, read_private
from baibai_engine.storage.objects.r2 import Boto3R2Store, ObjectStore, R2OperationStopped
from baibai_engine.storage.paths import INDICATORS_DB_PATH, MARKET_DB_PATH, RUNS_DB_PATH
from baibai_engine.storage.snapshots.identity import source_repo_root, verified_git_commit
from baibai_engine.storage.snapshots.keys import current_release_pointer_key, release_manifest_key
from baibai_engine.storage.snapshots.release import LakeReleasePointer
from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    LakeStoreOrigin,
    read_lake_store_origin,
)
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_jobs.acquisition.receipts import download_fixed
from baibai_jobs.integration import models, sources
from baibai_jobs.integration.edinet_list_native import clear_verified_receipt_cache
from baibai_jobs.integration.jpx_recovery_contract import PREFIX, RecoveryContract, decode_contract
from baibai_jobs.integration.jpx_recovery_verification import (
    coverage,
    edinet_coverage_rows,
    prior_generations,
    row_fingerprint,
    snapshot_market,
    verify_edinet_coverage,
    verify_lake,
    verify_models,
)
from baibai_jobs.integration.workflow_inputs import validate_recovery_input
from baibai_jobs.publication import lease
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.snapshot_publication import _resolve_serving_release

PHASES = ("acquire", "prepare", "apply", "models", "push", "readback", "seal", "release")


def digest_file(path: Path, algorithm: str = "sha256") -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def small_object(store: ObjectStore, key: str) -> tuple[bytes, dict[str, str]]:
    """Shell-owned machine receipts have no private-JSON SHA metadata."""
    before = store.head(key)
    if before is None or not 0 < before.size <= 2_000_000:
        raise ValueError("required small object is absent or oversized")
    payload = store.get_bytes(key)
    if store.head(key) != before or len(payload) != before.size:
        raise ValueError("small object changed during readback")
    if hashlib.md5(payload, usedforsecurity=False).hexdigest() != before.etag:
        raise ValueError("single-part object checksum differs")
    return payload, {"etag": before.etag, "sha256": hashlib.sha256(payload).hexdigest()}


def binary_readback(store: Boto3R2Store, key: str, target: Path, etag: str) -> None:
    before = store.head(key)
    if (
        before is None
        or before.etag != etag
        or re.fullmatch(r"[0-9a-f]{32}", etag) is None
        or not 0 < before.size <= 4 * 1024**3
        or target.exists()
    ):
        raise ValueError("machine readback generation, size, or destination differs")
    target.parent.mkdir(parents=True, exist_ok=True)
    store.download_file(key, target, expect_bytes=before.size, max_bytes=before.size)
    if digest_file(target, "md5") != etag or store.head(key) != before:
        raise ValueError("machine full-byte readback differs")
    with closing(sqlite3.connect(target.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("readback SQLite integrity failed")


def require_baseline(store: ObjectStore, contract: RecoveryContract) -> None:
    expected = contract.baseline
    for key, proof in (
        (current_release_pointer_key(), expected.current),
        ("machine-manifest.json", expected.machine_receipt),
    ):
        if small_object(store, key)[1] != proof.model_dump():
            raise ValueError("approved publication baseline changed")
    if sources.canonical_generations(store) != expected.generations():
        raise ValueError("baseline release or machine pair differs")
    for key, etag in (
        ("market.sqlite", expected.market_etag),
        ("runs.sqlite", expected.runs_etag),
        ("indicators.sqlite", expected.economic_etag),
    ):
        remote = store.head(key)
        if remote is None or remote.etag != etag:
            raise ValueError("baseline store generation differs")


def require_empty_list_cache(market: Path) -> None:
    # General hydrate/dehydrate may otherwise verify this cache against a new head.
    with closing(connect_read_only(market)) as connection:
        if connection.execute("SELECT 1 FROM edinet_list_responses LIMIT 1").fetchone() is not None:
            raise ValueError("recovery transfer requires an empty verified EDINET list cache")


def verify_stages(
    store: Boto3R2Store,
    contract: RecoveryContract,
    directory: Path,
    *,
    generations: dict[str, str] | None = None,
) -> None:
    plan = contract.plan
    selected = {
        entry["source"]: datetime.fromisoformat(entry["started_at"]) for entry in plan["handoffs"]
    }
    observed = set()
    for source, clock in selected.items():
        for key, _ in store.iter_objects("coordination/market-sources/" + source + "-"):
            if key.endswith("/handoff.json"):
                saved = read_private(store, key)
                if saved is None:
                    raise ValueError("JPX stage disappeared during listing")
                report = json.loads(saved.payload)["result"]
                if datetime.fromisoformat(report["started_at"]) >= clock:
                    observed.add(key.split("/")[-2])
    if observed != {stage.stage_id for stage in contract.stages}:
        raise ValueError("JPX stage membership changed; reconcile before recovery")
    if generations is None:
        fixed = sources._jpx_recovery_handoffs(
            store,
            [
                [entry["source"], entry["stage_id"], entry["manifest_sha256"]]
                for entry in plan["handoffs"]
            ],
        )
        if fixed != plan["handoffs"]:
            raise ValueError("original plan identities or receipt clocks differ")
    for stage in contract.stages:
        prefix = "coordination/market-sources/" + stage.stage_id
        saved = read_private(store, prefix + "/handoff.json")
        if saved is None or hashlib.sha256(saved.payload).hexdigest() != stage.handoff_sha256:
            raise ValueError("protected JPX handoff changed")
        marker = read_private(store, prefix + "/integrated.json")
        marker_digest = None if marker is None else hashlib.sha256(marker.payload).hexdigest()
        if marker_digest != stage.marker_sha256:
            raise ValueError("original integrated or pending marker changed")
        if stage.role == "preserved" and (
            marker is None or json.loads(marker.payload).get("status") != "integrated"
        ):
            raise ValueError("preserved newer stage is not integrated")
        manifest = json.loads(saved.payload)
        if (manifest["receipt_sha256"], manifest["receipt_bytes"]) != (
            stage.receipt_sha256,
            stage.receipt_bytes,
        ):
            raise ValueError("protected JPX receipt metadata changed")
        packet = directory / stage.stage_id
        download_fixed(
            store,
            {
                "stage_id": stage.stage_id,
                "market": True,
                "manifest_sha256": stage.handoff_sha256,
            },
            packet,
        )
        if digest_file(packet / "receipts.sqlite") != stage.receipt_sha256:
            raise ValueError("protected JPX original bytes changed")
        if stage.role == "recovery":
            recovered = read_private(store, prefix + "/recovered-" + stage.handoff_sha256 + ".json")
            if generations is None:
                if recovered is not None:
                    raise ValueError("recovery already has a receipt")
            else:
                if recovered is None:
                    raise ValueError("recovered receipt is absent")
                value = json.loads(recovered.payload)
                entry = next(
                    item for item in plan["handoffs"] if item["stage_id"] == stage.stage_id
                )
                if any(
                    value.get(key) != expected
                    for key, expected in {
                        "status": "integrated",
                        "manifest_sha256": stage.handoff_sha256,
                        "source": entry["source"],
                        "started_at": entry["started_at"],
                        "canonical_generations": generations,
                    }.items()
                ):
                    raise ValueError("recovered receipt is not bound to the verified publication")


class Recovery:
    def __init__(
        self, store: Boto3R2Store, directory: Path, digest: str, contract: RecoveryContract
    ) -> None:
        self.store, self.directory, self.digest, self.contract = store, directory, digest, contract
        self.root = source_repo_root()
        self.market = self.root / MARKET_DB_PATH
        self.runs = self.root / RUNS_DB_PATH
        self.economic = self.root / INDICATORS_DB_PATH
        self.mirror = self.root / "stores"
        self.integration = directory / "integration"
        self.handle_path = directory / "lease.json"
        self.notice: dict[str, Any] = {}
        self.prior: dict[str, Any] = {}
        self.local_models: dict[str, Any] = {}
        self.published: dict[str, Any] = {}
        self.model_started = self.model_finished = datetime.now(UTC)
        self.phase = "preflight"
        self.receipt_prefix = (
            PREFIX + "attempts/" + digest + "/" + os.environ["GITHUB_RUN_ID"] + "-1/"
        )

    def record(self, phase: str, state: str, **details: Any) -> None:
        payload = json.dumps(
            {"phase": phase, "state": state, "at": datetime.now(UTC).isoformat(), **details},
            sort_keys=True,
        ).encode()
        name = phase + "-" + state + ".json"
        (self.directory / name).write_bytes(payload)
        key = self.receipt_prefix + name
        put_private(self.store, key, payload, previous_etag=None)
        saved = read_private(self.store, key)
        if saved is None or saved.payload != payload:
            raise ValueError("recovery phase receipt outcome is uncertain")

    def run(self) -> None:
        require_baseline(self.store, self.contract)
        verify_stages(self.store, self.contract, self.directory / "originals")
        edinet = read_edinet_input(
            self.store, InputSnapshot.model_validate(self.contract.plan["edinet_input"])
        )
        if len(edinet.archives.archive_keys) != self.contract.edinet_archive_count:
            raise ValueError("fixed EDINET archive membership differs")
        if any(path.exists() for path in (self.market, self.runs, self.economic)):
            raise ValueError("recovery requires a fresh isolated checkout")
        if shutil.disk_usage(self.root).free < 20 * 1024**3:
            raise ValueError("recovery needs at least 20 GiB free before fresh preparation")
        claim = self.receipt_prefix + "attempt-started.json"
        if self.store.head(claim) is not None:
            raise ValueError("run already attempted; reconcile instead of rerunning")
        self.record(
            "attempt",
            "started",
            source_commit=verified_git_commit(),
            run_id=os.environ["GITHUB_RUN_ID"],
            run_attempt=os.environ["GITHUB_RUN_ATTEMPT"],
        )
        self.integration.mkdir()
        (self.integration / "plan.json").write_bytes(self.contract.plan_json.encode())
        for phase in PHASES:
            self.phase = phase
            self.record(phase, "started")
            try:
                getattr(self, phase)()
            except ValueError:
                # Only a terminal validation failure before publication is known safe.
                # Transport/child failures and every publication-phase failure stop here.
                if phase in {"prepare", "apply"}:
                    require_baseline(self.store, self.contract)
                    self.record("failure-release", "started")
                    self.release_lease()
                    self.record("failure-release", "succeeded")
                raise
            evidence: dict[str, Any] = {}
            if phase == "prepare":
                evidence = {"prior_generations": self.prior}
            elif phase == "apply":
                evidence = {"applied": json.loads((self.integration / "applied.json").read_bytes())}
            elif phase == "models":
                evidence = {"notice": self.notice, "model_proof": self.local_models}
            elif phase == "readback":
                evidence = {"publication": self.published, "model_proof": self.local_models}
            self.record(phase, "succeeded", **evidence)

    def acquire(self) -> None:
        require_baseline(self.store, self.contract)
        handle, _ = lease.acquire(self.store, "daily")
        lease._save_handle(self.handle_path, handle)
        self.held()

    def held(self) -> None:
        lease.verify_held(self.store, lease._load_handle(self.handle_path))

    def transfer(self, action: str) -> None:
        if action not in {"pull-machine", "pull-indicators", "hydrate-market", "push-machine"}:
            raise ValueError("unsupported recovery transfer")
        self.held()
        subprocess.run(  # nosec B603 B607
            ["bash", "jobs/scripts/r2_transfer.sh", action],
            cwd=self.root,
            check=True,
            env={**os.environ, "PUBLICATION_LEASE_HANDLE": str(self.handle_path)},
        )

    def prepare(self) -> None:
        require_baseline(self.store, self.contract)
        self.transfer("pull-machine")
        self.transfer("pull-indicators")
        for path, expected in (
            (self.market, self.contract.baseline.market_etag),
            (self.runs, self.contract.baseline.runs_etag),
            (self.economic, self.contract.baseline.economic_etag),
        ):
            if digest_file(path, "md5") != expected:
                raise ValueError("fresh pulled store bytes differ from baseline")
        require_empty_list_cache(self.market)
        self.transfer("hydrate-market")
        require_baseline(self.store, self.contract)
        if read_lake_store_origin(self.market) != LakeStoreOrigin(
            self.contract.baseline.lake_release_id, self.contract.baseline.lake_manifest_sha256
        ):
            raise ValueError("hydrated market origin differs")
        if snapshot_market(self.market) != self.contract.expected_before():
            raise ValueError("hydrated source tables differ from approved baseline")
        if coverage(self.market, "jpx") != self.contract.jpx_coverage_before.model_dump():
            raise ValueError("JPX coverage baseline differs")
        if coverage(self.market, "edinet") != self.contract.edinet_coverage_before.model_dump():
            raise ValueError("EDINET coverage baseline differs")
        self.prior = prior_generations(self.runs)
        write_json_atomic(self.directory / "prior-generations.json", self.prior)

    def apply(self) -> None:
        self.held()
        require_baseline(self.store, self.contract)
        before = edinet_coverage_rows(self.market)
        started = datetime.now(UTC)
        result = sources.apply(
            self.store,
            self.integration,
            market=self.market,
            asof=date.fromisoformat(self.contract.asof),
        )
        finished = datetime.now(UTC)
        if sources.main(["check", "--directory", str(self.integration)]) != 0:
            raise ValueError("source integration check failed; models cannot start")
        outcomes = cast(list[dict[str, Any]], result["handoffs"])
        if (
            [item["entry"] for item in outcomes] != self.contract.plan["handoffs"]
            or any(not item["applied"] or item["failed"] or item["recovery"] for item in outcomes)
            or result["edinet_input"] != self.contract.plan["edinet_input"]
        ):
            raise ValueError("exact source apply failed or changed identity")
        self.require_tables()
        changed = verify_edinet_coverage(
            before, edinet_coverage_rows(self.market), started_at=started, finished_at=finished
        )
        if changed != self.contract.edinet_projection_clock_updates:
            raise ValueError("EDINET projection clock update count differs")
        # Normal dehydrate freezes a current list head for nonempty receipt caches.
        # Verify this disposable copy against the original fixed pair before publication.
        fixed = read_edinet_input(
            self.store, InputSnapshot.model_validate(self.contract.plan["edinet_input"])
        )
        clear_verified_receipt_cache(fixed.lists, self.market)

    def require_tables(self) -> None:
        if snapshot_market(self.market) != self.contract.expected_after():
            raise ValueError("JPX, EDINET, or quants rows differ from the approved result")
        if coverage(self.market, "jpx") != self.contract.jpx_coverage_after.model_dump():
            raise ValueError("JPX original coverage clocks differ")

    def models(self) -> None:
        self.held()
        require_baseline(self.store, self.contract)
        self.model_started = datetime.now(UTC)
        self.notice = models.integrate_models(
            store=self.store,
            market=self.market,
            economic=self.economic,
            runs=self.runs,
            mirror=self.mirror,
            as_of=date.fromisoformat(self.contract.asof),
            lease_handle=self.handle_path,
            growth_start=None,
            policy=EconomicPathPolicy(),
            edinet_input=InputSnapshot.model_validate(self.contract.plan["edinet_input"]),
            preserve_generation_ids=tuple(self.prior["generation_ids"]),
        )
        self.model_finished = datetime.now(UTC)
        write_json_atomic(self.directory / "notice.json", self.notice)
        self.require_tables()
        self.local_models = self.model_proof(self.runs)

    def model_proof(self, path: Path) -> dict[str, Any]:
        return verify_models(
            path,
            self.prior,
            self.notice,
            self.contract.plan["edinet_input"],
            self.model_started,
            self.model_finished,
            self.store,
        )

    def generations(self) -> dict[str, str]:
        return {
            "lake_release_id": self.notice["lake"]["release_id"],
            "lake_manifest_sha256": self.notice["lake"]["release_manifest_sha256"],
        }

    def push(self) -> None:
        if self.notice.get("models_ready") is not True or not self.local_models:
            raise ValueError("models were not fully accepted")
        self.held()
        require_empty_list_cache(self.market)
        self.transfer("push-machine")

    def published_pair(self) -> dict[str, Any]:
        if sources.canonical_generations(self.store) != self.generations():
            raise ValueError("published current or machine pair differs")
        payload, receipt = small_object(self.store, "machine-manifest.json")
        return {
            "receipt": receipt,
            "value": json.loads(payload),
            "current": small_object(self.store, current_release_pointer_key())[1],
        }

    def readback(self) -> None:
        self.held()
        self.published = self.published_pair()
        target = self.directory / "readback"
        for key in ("market.sqlite", "runs.sqlite"):
            binary_readback(self.store, key, target / key, self.published["value"]["keys"][key])
        market = target / "market.sqlite"
        if read_lake_store_origin(market) != LakeStoreOrigin(
            self.generations()["lake_release_id"], self.generations()["lake_manifest_sha256"]
        ):
            raise ValueError("published compact market origin differs")
        with (
            closing(connect_read_only(market)) as remote,
            closing(connect_read_only(self.market)) as local,
        ):
            if any(
                remote.execute("SELECT count(*) FROM " + ds.sqlite_table).fetchone()[0]  # nosec B608
                for ds in LAKE_DATASETS.values()
            ):
                raise ValueError("published machine market is not compact")
            if row_fingerprint(remote.execute("SELECT * FROM source_coverage")) != row_fingerprint(
                local.execute("SELECT * FROM source_coverage")
            ):
                raise ValueError("published operational coverage differs")
        if self.model_proof(target / "runs.sqlite") != self.local_models:
            raise ValueError("published model or Selection rows differ")
        verify_lake(self.store, target / "lake", self.generations(), self.contract.expected_after())
        previous = LakeReleasePointer(
            release_id=self.contract.baseline.lake_release_id,
            manifest_key=release_manifest_key(release_id=self.contract.baseline.lake_release_id),
            manifest_sha256=self.contract.baseline.lake_manifest_sha256,
        )
        _resolve_serving_release(self.store, target / "previous", previous)
        verify_stages(self.store, self.contract, target / "originals")
        if self.published_pair() != self.published:
            raise ValueError("publication changed during full readback")
        self.held()

    def seal(self) -> None:
        self.held()
        if not self.published or self.published_pair() != self.published:
            raise ValueError("full publication readback is absent or stale")
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
            raise ValueError("publication-bound JPX seal failed")
        verify_stages(
            self.store,
            self.contract,
            self.directory / "sealed-originals",
            generations=self.generations(),
        )
        if self.published_pair() != self.published:
            raise ValueError("publication changed during seal")

    def release(self) -> None:
        self.release_lease()
        if self.published_pair() != self.published:
            raise ValueError("publication changed before final readback")

    def release_lease(self) -> None:
        handle = lease._load_handle(self.handle_path)
        self.held()
        lease.release(self.store, handle)
        observed = lease._generation(self.store)
        if observed is None or observed[0].state != "released" or observed[0].owner != handle.owner:
            raise ValueError("lease release readback differs")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract-sha256", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args(argv)
    store = None
    operation = None
    owned_directory = False
    try:
        validate_recovery_input(contract_sha256=args.contract_sha256)
        if (
            os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("GITHUB_RUN_ATTEMPT") != "1"
            or os.environ.get("GITHUB_REPOSITORY") != "koumatsumoto/baibai-loop"
            or verified_git_commit() != os.environ.get("GITHUB_SHA")
        ):
            raise ValueError(
                "exact recovery requires the first attempt of a verified Actions checkout"
            )
        os.umask(0o077)
        args.directory.mkdir(parents=True, exist_ok=False)
        owned_directory = True
        os.environ.update(
            R2_STOP_ON_REJECTION="1",
            R2_REJECTION_STOP_FILE=str(args.directory / "rejected"),
            AWS_MAX_ATTEMPTS="1",
            AWS_RETRY_MODE="standard",
            AWS_EC2_METADATA_DISABLED="true",
            BAIBAI_LAKE_QUERY_PROFILE="small-runner",
        )
        store = Boto3R2Store(bucket="baibai-stores")
        saved = read_private(store, PREFIX + "contracts/" + args.contract_sha256 + ".json")
        if saved is None:
            raise ValueError("private recovery contract is absent")
        contract = decode_contract(saved.payload, args.contract_sha256)
        if contract.source_commit != verified_git_commit():
            raise ValueError("recovery source differs from the reviewed contract commit")
        (args.directory / "contract.json").write_bytes(saved.payload)
        operation = Recovery(store, args.directory, args.contract_sha256, contract)
        operation.run()
        return 0
    except BaseException as error:
        # Never reconnect for cleanup after rejection or an unknown write outcome.
        if owned_directory:
            latch = args.directory / "rejected"
            status = latch.read_text().strip() if latch.is_file() else ""
            write_json_atomic(
                args.directory / "stopped.json",
                {
                    "state": "reconciliation_required",
                    "phase": operation.phase if operation is not None else "validation",
                    "error_type": type(error).__name__,
                    "rejection_latched": latch.exists(),
                    "http_status": int(status) if status in {"403", "429"} else None,
                    "automatic_retry": False,
                    "lease_may_be_held": (args.directory / "acquire-started.json").exists()
                    and not (args.directory / "failure-release-succeeded.json").exists()
                    and not (args.directory / "release-succeeded.json").exists(),
                },
            )
        print("JPX recovery stopped; reconcile private receipts and publication before any retry")
        return 75 if isinstance(error, R2OperationStopped) else 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
