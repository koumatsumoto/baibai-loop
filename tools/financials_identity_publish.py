"""財務ID切替専用のsnapshot固定・条件付き反映。通常Jobsからは呼ばない。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess  # nosec B404 - fixed read-only git argument vectors below
import tempfile
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import Boto3R2Store, ObjectStore
from baibai_engine.storage.sqlite.source_cache.snapshot import create_snapshot
from baibai_jobs.publication.lease import (
    DEFAULT_STORES_BUCKET,
    LeaseError,
    PublicationLeaseHandle,
    _load_handle,
    verify_held,
)
from tools.financials_identity import _open, _self_contained, file_sha256, prepare_copy

MACHINE = "machine-manifest.json"
ORDER = ("runs.sqlite", MACHINE, "baibai.sqlite")
UNTOUCHED = ("market.sqlite", "indicators.sqlite")
KEYS = (*ORDER, *UNTOUCHED)
type Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
type Revision = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]


class FrozenFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    sha256: Digest
    content_md5: str
    size: Annotated[int, Field(gt=0)]


class UploadPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    format: Literal["company-financials-publish-v1"] = "company-financials-publish-v1"
    bucket: str
    prepared_at: AwareDatetime
    source_revision: Revision
    target_revision: Revision
    copy_manifest_sha256: Digest
    base_etags: dict[str, str]
    uploads: dict[str, FrozenFile]
    backups: dict[str, FrozenFile]


def _now() -> datetime:
    return datetime.now(UTC)


def _once(path: Path, body: bytes) -> None:
    with path.open("xb") as output:
        output.write(body)
        output.flush()
        os.fsync(output.fileno())
    _seal(path)


def _seal(path: Path) -> None:
    with path.open("rb") as source:
        os.fsync(source.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _descriptor(path: Path) -> FrozenFile:
    if path.is_symlink():
        raise ValueError("cutover files must not be symlinks")
    _self_contained(path)
    sha = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            sha.update(chunk)
            md5.update(chunk)
    return FrozenFile(
        sha256=sha.hexdigest(),
        content_md5=base64.b64encode(md5.digest()).decode(),
        size=path.stat().st_size,
    )


def _heads(store: ObjectStore) -> dict[str, str]:
    values = {key: store.head(key) for key in KEYS}
    if any(value is None or not value.etag for value in values.values()):
        raise ValueError("cutover requires every existing store and machine manifest")
    return {key: value.etag for key, value in values.items() if value is not None}


def _download_sha(store: ObjectStore, key: str) -> str:
    before = store.head(key)
    if before is None:
        raise ValueError("cutover object is missing")
    with tempfile.TemporaryDirectory(prefix="financials-cutover-readback-") as temporary:
        path = Path(temporary) / "object"
        store.download_file(key, path, expect_bytes=before.size)
        digest = file_sha256(path)
        if path.stat().st_size != before.size:
            raise ValueError("cutover object size changed during readback")
    after = store.head(key)
    if after is None or (before.etag, before.size) != (after.etag, after.size):
        raise ValueError("cutover object changed during readback")
    return digest


def _database_fingerprint(path: Path, version: int) -> str:
    """backup APIが全SQL値・schemaを保持したことを物理page配置とは別に照合する。"""
    digest = hashlib.sha256()
    with closing(_open(path, version)) as connection:
        for row in connection.execute("SELECT name,type,sql FROM sqlite_schema ORDER BY name"):
            digest.update(repr(tuple(row)).encode())
        for (table,) in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"
        ):
            if re.fullmatch(r"[a-z][a-z0-9_]*", table) is None:
                raise ValueError("unsupported SQL table identifier")
            info = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            columns = [column[1] for column in info]
            if any(re.fullmatch(r"[a-z][a-z0-9_]*", name) is None for name in columns):
                raise ValueError("unsupported SQL column identifier")
            keys = [column[1] for column in sorted(info, key=lambda item: item[5]) if column[5]]
            order = ",".join(f'"{name}"' for name in (keys or columns))
            # Both names and order are validated SQLite identifiers, never CLI input.
            query = f'SELECT * FROM "{table}" ORDER BY {order}'  # nosec B608
            digest.update(table.encode())
            for row in connection.execute(query):
                for value in row:
                    data = value if isinstance(value, bytes) else repr(value).encode()
                    digest.update(type(value).__name__.encode())
                    digest.update(len(data).to_bytes(8, "big"))
                    digest.update(data)
    return digest.hexdigest()


def _guard(
    store: ObjectStore, handle: PublicationLeaseHandle, drained: bool, clock: Callable[[], datetime]
) -> None:
    if not drained or handle.purpose != "operator":
        raise ValueError("cutover requires independently drained writers and an operator lease")
    verify_held(store, handle, now=clock())
    if not handle.acquired_at_utc <= clock() < handle.expires_at_utc:
        raise LeaseError("operator lease expired during its verification")


def prepare_upload(
    store: ObjectStore,
    *,
    bucket: str,
    source_app: Path,
    source_runs: Path,
    copy: Path,
    generations: Path,
    directory: Path,
    source_revision: str,
    target_revision: str,
    handle: PublicationLeaseHandle,
    writers_drained: bool,
    clock: Callable[[], datetime] = _now,
) -> UploadPlan:
    """取得済みpaired ETagに束縛し、PUT可能になる前に実転送fileと原本を固定する。"""
    if directory.exists():
        raise ValueError("upload directory must not exist; preserve any interrupted preparation")
    _guard(store, handle, writers_drained, clock)
    paired = {
        key: (generations / f"{key}.etag").read_text().strip().strip('"')
        for key in ("runs.sqlite", "baibai.sqlite")
    }
    before = _heads(store)
    if any(before[key] != etag for key, etag in paired.items()):
        raise ValueError("remote differs from the original paired pull generations")
    original_manifest = store.get_bytes(MACHINE)
    original = json.loads(original_manifest)
    if (
        set(original) != {"schema_version", "written_at_utc", "keys"}
        or original["schema_version"] != 1
        or original["keys"] != {key: before[key] for key in (*UNTOUCHED, "runs.sqlite")}
    ):
        raise ValueError("original machine manifest differs from the pulled bundle")
    conversion = json.loads((copy / "manifest.json").read_text())
    prepare_copy(
        source_app=source_app,
        source_runs=source_runs,
        output=copy,
        recorded_at=datetime.fromisoformat(conversion["recorded_at"]),
    )
    sources = {"baibai.sqlite": source_app, "runs.sqlite": source_runs}
    for key, source in sources.items():
        if _download_sha(store, key) != file_sha256(source):
            raise ValueError("original remote bytes differ from the fixed source copy")
    directory.mkdir(parents=True, mode=0o700)
    (directory / "backup").mkdir()
    uploads, backups = {}, {}
    for key, source in sources.items():
        backup = directory / "backup" / key
        shutil.copyfile(source, backup)
        _seal(backup)
        backups[key] = _descriptor(backup)
        candidate = copy / ("application.sqlite" if key == "baibai.sqlite" else key)
        version = 29 if key == "baibai.sqlite" else 8
        fingerprint = _database_fingerprint(candidate, version)
        create_snapshot(candidate, directory / key)
        _seal(directory / key)
        if fingerprint != _database_fingerprint(directory / key, version):
            raise ValueError("upload snapshot differs from the verified conversion")
        uploads[key] = _descriptor(directory / key)
    _once(directory / "backup" / MACHINE, original_manifest)
    backups[MACHINE] = _descriptor(directory / "backup" / MACHINE)
    plan = UploadPlan(
        bucket=bucket,
        prepared_at=clock(),
        source_revision=source_revision,
        target_revision=target_revision,
        copy_manifest_sha256=file_sha256(copy / "manifest.json"),
        base_etags=before,
        uploads=uploads,
        backups=backups,
    )
    for key, source in sources.items():
        if file_sha256(source) != backups[key].sha256:
            raise ValueError("original source changed during upload preparation")
    if _heads(store) != before:
        raise ValueError("remote generations changed during upload preparation")
    _guard(store, handle, writers_drained, clock)
    _once(directory / "plan.json", _json(plan.model_dump(mode="json")))
    _once(directory / "acknowledged.json", b"{}")
    return plan


def _load(directory: Path, bucket: str) -> UploadPlan:
    plan = UploadPlan.model_validate_json((directory / "plan.json").read_bytes())
    if (
        plan.bucket != bucket
        or set(plan.base_etags) != set(KEYS)
        or set(plan.uploads) != {"runs.sqlite", "baibai.sqlite"}
        or set(plan.backups) != set(ORDER)
        or any(not value for value in plan.base_etags.values())
    ):
        raise ValueError("upload plan differs from the fixed cutover contract")
    for key, expected in plan.uploads.items():
        if _descriptor(directory / key) != expected:
            raise ValueError("frozen upload bytes changed")
    for key, expected in plan.backups.items():
        if _descriptor(directory / "backup" / key) != expected:
            raise ValueError("frozen rollback backup changed")
    return plan


def _acks(directory: Path) -> dict[str, str]:
    value = json.loads((directory / "acknowledged.json").read_text())
    if (
        not isinstance(value, dict)
        or set(value) - set(ORDER)
        or any(not isinstance(etag, str) or not etag for etag in value.values())
    ):
        raise ValueError("invalid cutover acknowledgement journal")
    return cast(dict[str, str], value)


def _ack(directory: Path, key: str, etag: str) -> None:
    saved = _acks(directory)
    if key in saved and saved[key] != etag:
        raise ValueError("acknowledged generation changed")
    saved[key] = etag
    write_json_atomic(directory / "acknowledged.json", saved)


def _machine_bytes(plan: UploadPlan, runs_etag: str, written_at: datetime) -> bytes:
    return _json(
        {
            "schema_version": 1,
            "written_at_utc": written_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "keys": {**{key: plan.base_etags[key] for key in UNTOUCHED}, "runs.sqlite": runs_etag},
        }
    )


def _machine_candidate(
    directory: Path,
    plan: UploadPlan,
    runs_etag: str,
    *,
    create: bool,
    clock: Callable[[], datetime] = _now,
) -> FrozenFile | None:
    path = directory / MACHINE
    receipt = directory / "machine-upload.json"
    if not path.exists():
        if receipt.exists():
            raise ValueError("machine upload file is missing")
        if not create:
            return None
        _once(path, _machine_bytes(plan, runs_etag, clock()))
    saved = json.loads(path.read_bytes())
    written_at = datetime.fromisoformat(saved["written_at_utc"])
    if written_at.utcoffset() != UTC.utcoffset(written_at):
        raise ValueError("machine manifest timestamp must be UTC")
    body = _machine_bytes(plan, runs_etag, written_at)
    if path.read_bytes() != body:
        raise ValueError("frozen machine manifest differs from the verified runs generation")
    descriptor = _descriptor(path)
    if receipt.exists():
        if FrozenFile.model_validate_json(receipt.read_bytes()) != descriptor:
            raise ValueError("machine upload digest changed")
    elif create:
        _once(receipt, _json(descriptor.model_dump(mode="json")))
    else:
        return None
    return descriptor


def inspect_upload(store: ObjectStore, *, bucket: str, directory: Path) -> dict[str, Any]:
    """原本/候補の既知prefixだけをHEAD→GET→HEADで識別する。remote書込なし。"""
    plan = _load(directory, bucket)
    before = _heads(store)
    if any(before[key] != plan.base_etags[key] for key in UNTOUCHED):
        raise ValueError("unrelated machine store changed; do not rebase the cutover")
    acknowledgements = _acks(directory)
    states = []
    for key in ORDER:
        if key in acknowledgements and before[key] != acknowledgements[key]:
            raise ValueError("acknowledged remote generation changed")
        if before[key] == plan.base_etags[key]:
            expected = plan.backups[key].sha256
            states.append("original")
        else:
            if key == MACHINE:
                candidate = _machine_candidate(directory, plan, before["runs.sqlite"], create=False)
                if candidate is None:
                    raise ValueError("remote machine manifest has no frozen upload evidence")
            else:
                candidate = plan.uploads[key]
            expected = candidate.sha256
            states.append("candidate")
        if _download_sha(store, key) != expected:
            raise ValueError("remote bytes are neither the exact original nor the frozen candidate")
    if _heads(store) != before:
        raise ValueError("remote bundle changed during cutover inspection")
    allowed = [
        ["original", "original", "original"],
        ["candidate", "original", "original"],
        ["candidate", "candidate", "original"],
        ["candidate", "candidate", "candidate"],
    ]
    if states not in allowed:
        raise ValueError("remote cutover state is not a known publication prefix")
    completed = states.count("candidate")
    return {
        "completed_keys": list(ORDER[:completed]),
        "etags": before,
        "next_key": None if completed == 3 else ORDER[completed],
    }


def publish_upload(
    store: ObjectStore,
    *,
    bucket: str,
    directory: Path,
    handle: PublicationLeaseHandle,
    writers_drained: bool,
    clock: Callable[[], datetime] = _now,
) -> dict[str, Any]:
    """再実行も全keyをexact照合してから、未到達の元ETagだけへCASする。"""
    _guard(store, handle, writers_drained, clock)
    plan = _load(directory, bucket)
    for _ in range(4):
        state = inspect_upload(store, bucket=bucket, directory=directory)
        for key in state["completed_keys"]:
            _ack(directory, key, state["etags"][key])
        key = state["next_key"]
        if key is None:
            write_json_atomic(directory / "verified.json", state)
            return state
        if key == MACHINE:
            descriptor = _machine_candidate(
                directory, plan, state["etags"]["runs.sqlite"], create=True, clock=clock
            )
            assert descriptor is not None
        else:
            descriptor = plan.uploads[key]
        if _descriptor(directory / key) != descriptor:
            raise ValueError("upload bytes changed before PUT")
        if _heads(store) != state["etags"]:
            raise ValueError("remote generations changed before PUT")
        _guard(store, handle, writers_drained, clock)
        # The production adapter is constructed with verify_put_with_head=False.
        # An exception deliberately stops here; the next call reconciles actual GET bytes first.
        response = store.put_file(
            key,
            directory / key,
            sha256=descriptor.sha256,
            content_md5=descriptor.content_md5,
            content_type="application/json" if key == MACHINE else "application/octet-stream",
            if_match=plan.base_etags[key],
        )
        if not response.etag or response.size != descriptor.size:
            raise ValueError("PUT acknowledgement is incomplete; reconcile before resuming")
        _ack(directory, key, response.etag)
    raise ValueError("cutover did not reach a verified complete state")


def _revision() -> str:
    # Fixed argument vector and trusted checkout; no shell or caller-supplied executable.
    git = shutil.which("git")
    if git is None:
        raise ValueError("git is required to bind the executable checkout")
    root = str(Path(__file__).resolve().parents[1])
    if subprocess.check_output(  # nosec B603
        [git, "-C", root, "status", "--porcelain", "--untracked-files=normal"], text=True
    ).strip():
        raise ValueError("cutover requires a clean executable checkout")
    return subprocess.check_output(  # nosec B603
        [git, "-C", root, "rev-parse", "HEAD"], text=True
    ).strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare-upload")
    prepare.add_argument("--source-app", type=Path, required=True)
    prepare.add_argument("--source-runs", type=Path, required=True)
    prepare.add_argument("--copy", type=Path, required=True)
    prepare.add_argument("--generations", type=Path, required=True)
    prepare.add_argument("--source-revision", required=True)
    for command in (prepare, commands.add_parser("apply"), commands.add_parser("status")):
        command.add_argument("--directory", type=Path, required=True)
        if command.prog.split()[-1] != "status":
            command.add_argument("--lease-handle", type=Path, required=True)
            command.add_argument("--writers-drained", action="store_true", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    bucket = os.environ.get("R2_STORES_BUCKET") or DEFAULT_STORES_BUCKET
    store = Boto3R2Store(bucket=bucket, verify_put_with_head=False)
    try:
        if args.command == "prepare-upload":
            prepare_upload(
                store,
                bucket=bucket,
                source_app=args.source_app,
                source_runs=args.source_runs,
                copy=args.copy,
                generations=args.generations,
                directory=args.directory,
                source_revision=args.source_revision,
                target_revision=_revision(),
                handle=_load_handle(args.lease_handle),
                writers_drained=args.writers_drained,
            )
            print("upload snapshots and backup fixed; no remote PUT")
        elif args.command == "status":
            result = inspect_upload(store, bucket=bucket, directory=args.directory)
            print(
                json.dumps(
                    {"completed_keys": result["completed_keys"], "next_key": result["next_key"]}
                )
            )
        else:
            if _load(args.directory, bucket).target_revision != _revision():
                raise ValueError("executable checkout differs from the fixed target revision")
            result = publish_upload(
                store,
                bucket=bucket,
                directory=args.directory,
                handle=_load_handle(args.lease_handle),
                writers_drained=args.writers_drained,
            )
            print(
                json.dumps(
                    {"completed_keys": result["completed_keys"], "next_key": result["next_key"]}
                )
            )
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
