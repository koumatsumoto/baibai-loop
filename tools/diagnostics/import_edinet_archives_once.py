"""固定した旧EDINET原本を既存ownerへ移す一回用Actions入口。通常処理へは接続しない。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess  # nosec B404
import sys
from collections.abc import Callable, Mapping
from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.providers.edinet.archive_native import EdinetArchiveL1
from baibai_engine.data_collection.providers.edinet.snapshot_contracts import EDINET_SOURCE_ARCHIVES
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import Boto3R2Store
from baibai_engine.storage.snapshots.hydrate import hydrate_market_store
from baibai_engine.storage.snapshots.objects import open_lake
from baibai_engine.storage.snapshots.reader import FixedRelease, resolve_current_release
from baibai_engine.storage.snapshots.sources import sha256_file
from baibai_jobs.acquisition.receipts import ReceiptStore
from baibai_jobs.maintenance.edinet_archive_import import (
    apply_import,
    prepare_import,
    verify_import,
)
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.lease import _load_handle, verify_held
from baibai_jobs.publication.policy import PRODUCTION_RELEASE_POLICY
from tools.diagnostics.edinet_archive_inspection import (
    inspect_prepared_capacity,
    inspect_remote_inputs,
)

# Deterministic plan paths; mkdir(0700, exist_ok=False) rejects a preexisting path/symlink.
WORK = Path("/tmp/baibai-edinet-archive-import-once")  # nosec B108
RESERVE_BYTES = 1024**3
REPOSITORY = "koumatsumoto/baibai-loop"
WRITERS = ("cloud-market-sources.yml", "cloud-daily-batch.yml")


class ImportGuardError(ValueError):
    """A fixed, non-sensitive operator diagnostic from this finite entry."""


@dataclass(frozen=True)
class Request:
    mode: str
    code: str
    release: str
    manifest: str
    market: str
    lists: str
    archives: str
    plan: str
    initialize: bool
    bucket: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Request:
        request = cls(
            mode=env.get("MODE", ""),
            code=env.get("EXPECTED_CODE_SHA", ""),
            release=env.get("FIXED_RELEASE", ""),
            manifest=env.get("FIXED_MANIFEST", ""),
            market=env.get("FIXED_MARKET", ""),
            lists=env.get("FIXED_LIST_HEAD", ""),
            archives=env.get("EXPECTED_ARCHIVE_HEAD", ""),
            plan=env.get("EXPECTED_PLAN", ""),
            initialize=env.get("INITIALIZE_ARCHIVE_HEAD", "false") == "true",
            bucket=env.get("IMPORT_BUCKET", ""),
        )
        if request.mode not in {"inspect", "plan", "apply", "verify"}:
            raise ImportGuardError("unsupported import mode")
        if re.fullmatch(r"[0-9a-f]{40}", request.code) is None:
            raise ImportGuardError("exact code commit required")
        if request.bucket != "baibai-stores":
            raise ImportGuardError(
                "this finite entry is restricted to the existing production bucket"
            )
        if env.get("INITIALIZE_ARCHIVE_HEAD", "false") not in {"true", "false"}:
            raise ImportGuardError("invalid initialization selection")
        if request.mode != "inspect":
            if not request.release or any(
                re.fullmatch(r"[0-9a-f]{64}", value) is None
                for value in (request.manifest, request.lists)
            ):
                raise ImportGuardError("fixed release and list head required")
            if (
                request.archives != "absent"
                and re.fullmatch(r"[0-9a-f]{64}", request.archives) is None
            ):
                raise ImportGuardError("fixed observed archive head required")
        for value in (request.market, request.plan):
            if value and re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ImportGuardError("invalid fixed digest")
        if request.mode in {"apply", "verify"} and (not request.market or not request.plan):
            raise ImportGuardError("apply and verify need the preceding canonical and plan digests")
        if request.initialize and (
            request.mode != "apply"
            or request.archives != "absent"
            or env.get("GITHUB_RUN_ATTEMPT") != "1"
        ):
            raise ImportGuardError(
                "initialization requires an explicit first attempt with absent head"
            )
        return request


def _api(endpoint: str) -> dict[str, Any]:
    # Fixed executable and repository; endpoints come only from this file, never shell input.
    command = ["gh", "api", "--method", "GET", f"repos/{REPOSITORY}/{endpoint}"]
    result = subprocess.run(
        command,
        check=True,  # nosec B603
        capture_output=True,
        text=True,
        timeout=45,
    )
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ImportGuardError("unexpected GitHub response")
    return value


def check_github(request: Request, env: Mapping[str, str]) -> None:
    if (
        env.get("GITHUB_REPOSITORY") != REPOSITORY
        or env.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or env.get("GITHUB_REF") != "refs/heads/main"
        or env.get("GITHUB_SHA") != request.code
    ):
        raise ImportGuardError("only the explicitly pinned main workflow may run")
    if _api("git/ref/heads/main")["object"]["sha"] != request.code:
        raise ImportGuardError("main moved after the operator pinned the entry")
    if request.mode == "inspect":
        return
    for name in WRITERS:
        if _api(f"actions/workflows/{name}")["state"] != "disabled_manually":
            raise ImportGuardError("normal writers must stay disabled through cutover acceptance")
        # A state is not a drain: completed runs alone are safe, including old revisions.
        for status in ("queued", "in_progress", "waiting", "pending", "requested"):
            if _api(f"actions/workflows/{name}/runs?status={status}&per_page=1")["total_count"]:
                raise ImportGuardError("normal writer run has not drained")


def public_inspection(value: dict[str, Any]) -> dict[str, Any]:
    """Only counts and digests leave the private capture, never keys, ETags or raw rows."""
    result = {
        key: value[key]
        for key in (
            "release_id",
            "manifest_sha256",
            "archive_dataset",
            "source_stages",
            "handoff_inventory_sha256",
            "listed_inventory_sha256",
            "free_bytes",
        )
    }
    for name in ("canonical", "native_lists", "native_archives"):
        selected = {
            key: item for key, item in value[name].items() if key not in {"etag", "scope_since"}
        }
        metadata = selected.get("metadata_sha256")
        if metadata is not None and re.fullmatch(r"[0-9a-f]{64}", str(metadata)) is None:
            selected["metadata_sha256"] = None
        etag = value[name]["etag"]
        selected["etag_fingerprint"] = (
            None if etag is None else hashlib.sha256(etag.encode()).hexdigest()
        )
        result[name] = selected
    return result


def require_fixed(request: Request, observed: dict[str, Any]) -> None:
    if (observed["release_id"], observed["manifest_sha256"]) != (request.release, request.manifest):
        raise ImportGuardError("current market release is not the fixed input")
    canonical = observed["canonical"]
    if not canonical["present"]:
        raise ImportGuardError("canonical market is missing")
    if request.market and canonical["metadata_sha256"] not in {None, request.market}:
        raise ImportGuardError("canonical metadata differs from pinned bytes")
    if observed["native_lists"]["wire_sha256"] != request.lists:
        raise ImportGuardError("native list head changed")
    archives = observed["native_archives"]
    actual = archives["wire_sha256"] if archives["present"] else "absent"
    if actual != request.archives:
        raise ImportGuardError("inspect the changed target before resuming the same plan")
    for group in observed["source_stages"]:
        if (
            group["invalid_handoffs"]
            or group["receipts_without_handoff_directory"]
            or group["receipt_objects"] != group["handoffs"]
        ):
            raise ImportGuardError("unaccounted old receipts require individual inspection")


def unchanged_inputs(before: dict[str, Any], after: dict[str, Any], *, target: bool) -> None:
    keys = [
        "release_id",
        "manifest_sha256",
        "canonical",
        "native_lists",
        "handoff_inventory_sha256",
        "listed_inventory_sha256",
    ]
    if target:
        keys.append("native_archives")
    if any(before[key] != after[key] for key in keys):
        raise ImportGuardError("fixed import inputs moved")


def prepare_fixed(
    store: ReceiptStore,
    request: Request,
    release: FixedRelease,
    directory: Path,
    hydrate: Callable[[Path], None],
    report: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    canonical = directory / "canonical.sqlite"
    head = store.head("market.sqlite")
    if head is None:
        raise ImportGuardError("canonical market is missing")
    # This is only a lower bound; hydration/stage expansion can still exhaust the runner.
    if shutil.disk_usage(directory).free < 2 * head.size + RESERVE_BYTES:
        raise ImportGuardError("insufficient room even for canonical download and hydrate copy")
    store.download_file("market.sqlite", canonical, expect_bytes=head.size)
    digest = sha256_file(canonical)
    if store.head("market.sqlite") != head or head.metadata.get("sha256", digest) != digest:
        raise ImportGuardError("canonical changed or has inconsistent metadata")
    if request.market and digest != request.market:
        raise ImportGuardError("canonical bytes differ from pinned input")
    report["canonical_sha256"] = digest
    with closing(sqlite3.connect(canonical.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM edinet_source_archives").fetchone()[0]
    report["canonical_archive_rows_before_hydrate"] = rows
    if rows:
        raise ImportGuardError("nonempty canonical originals need a separate equality review")
    hydrate(canonical)  # Existing same-origin partial repair preserves operational scope.
    destination = directory / "import"
    plan = prepare_import(store, source=canonical, release=release, directory=destination)
    actual = (destination / "import-plan.sha256").read_text().strip()
    report["plan_sha256"] = actual
    if request.plan and actual != request.plan:
        raise ImportGuardError("reconstructed plan differs; do not replace the original plan")
    capacity = inspect_prepared_capacity(plan, directory=directory)
    report["capacity"] = capacity
    if capacity["free_bytes"] < capacity["native_body_write_peak_bytes"] + RESERVE_BYTES:
        raise ImportGuardError("insufficient additional room for all retained native ZIP bodies")
    return destination, plan


def execute(
    request: Request, lease_path: Path, report: dict[str, Any], emit: Callable[[], None]
) -> None:
    WORK.mkdir(mode=0o700, parents=True, exist_ok=False)
    store = Boto3R2Store(bucket=request.bucket)
    with open_lake(mirror=WORK / "cache", bucket=request.bucket) as (session, cache):

        def observe() -> tuple[FixedRelease, dict[str, Any]]:
            current = resolve_current_release(
                cache.source,
                catalog=LAKE_DATASETS,
                policy=PRODUCTION_RELEASE_POLICY,
            )
            return current, inspect_remote_inputs(store, current, directory=WORK)

        release, before = observe()
        report["observed"] = public_inspection(before)
        emit()
        if request.mode == "inspect":
            report["status"] = "observed_not_drained"
            return
        require_fixed(request, before)
        handle = _load_handle(lease_path)
        if handle.purpose != "operator":
            raise ImportGuardError("the finite import requires its own operator lease")
        verify_held(store, handle)
        report["phase"] = "prepare_local"
        emit()

        def hydrate(path: Path) -> None:
            if EDINET_SOURCE_ARCHIVES.name in release.dataset_names():
                hydrate_market_store(
                    session,
                    catalog=LAKE_DATASETS,
                    release=release,
                    cache=cache,
                    store=path,
                    dataset_names=(EDINET_SOURCE_ARCHIVES.name,),
                )

        directory, _ = prepare_fixed(store, request, release, WORK, hydrate, report)
        check_github(request, os.environ)
        verify_held(store, handle)
        unchanged_inputs(before, observe()[1], target=True)
        report["phase"] = "prepared"
        emit()
        if request.mode == "plan":
            report["status"] = "planned_not_imported"
            return
        owner = EdinetArchiveL1(store, directory)
        imported_identity = None
        if request.mode == "apply":
            report.update(phase="native_apply", native_write_may_have_occurred=True)
            emit()  # Public boundary is emitted before the first possible native PUT.
            if request.initialize:
                owner.initialize()
            imported_identity = apply_import(owner, directory=directory)["l1_identity"]
        report["phase"] = "verify_all_originals"
        emit()
        verify_import(owner, directory=directory)
        verify_held(store, handle)
        check_github(request, os.environ)
        _, after = observe()
        unchanged_inputs(before, after, target=request.mode == "verify")
        if (
            imported_identity is not None
            and after["native_archives"]["identity"] != imported_identity
        ):
            raise ImportGuardError("archive head changed after the verified import")
        report.update(
            status="verified_not_cut_over", verified=True, observed=public_inspection(after)
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "run"))
    parser.add_argument("--lease-handle", type=Path)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--private-log", type=Path)
    parser.add_argument("--public-fd", type=int, choices=(3,))
    args = parser.parse_args()
    report: dict[str, Any] = {
        "status": "running",
        "phase": "preflight",
        "native_write_may_have_occurred": False,
        "verified": False,
        "cut_over": False,
    }
    public = sys.stdout

    def emit() -> None:
        if args.summary is not None:
            write_json_atomic(args.summary, report)
        encoded = json.dumps(report, sort_keys=True)
        if args.public_fd is None:
            print(encoded, file=public, flush=True)
        else:
            # The shell captures fd 1/2, including native libraries and child processes.
            # Only this allowlisted summary uses the separate public descriptor.
            remaining = (encoded + "\n").encode()
            while remaining:
                remaining = remaining[os.write(args.public_fd, remaining) :]

    try:
        request = Request.from_env(os.environ)
        check_github(request, os.environ)
        if args.command == "check":
            print("fixed_code_and_drain_check=passed")
            return 0
        if args.private_log is None or args.summary is None or args.lease_handle is None:
            raise ImportGuardError("run needs private capture, summary and lease paths")
        os.umask(0o077)
        with args.private_log.open("a", encoding="utf-8") as private:
            args.private_log.chmod(0o600)
            with redirect_stdout(private), redirect_stderr(private):
                execute(request, args.lease_handle, report, emit)
    except Exception as error:
        report.update(status="failed", error_type=type(error).__name__)
        if isinstance(error, ImportGuardError):
            report["reason"] = str(error)
        emit()
        return 1
    emit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
