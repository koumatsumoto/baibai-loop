"""Credential-free, exact-tree Machine roundtrip; fake only the S3 HTTP peer."""

from __future__ import annotations

import argparse
import base64
from collections import Counter
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from hashlib import file_digest, md5, sha256
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4
from xml.etree import ElementTree

import boto3
import botocore
from botocore.awsrequest import AWSResponse
from botocore.httpsession import URLLib3Session
import duckdb
import pytest

from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationMetadata,
    GenerationSubject,
    InputSnapshot,
    ModelGeneration,
)
from baibai_engine.machine_analysis.generation.query import read_generation
from baibai_engine.machine_analysis.screening.discovery_index import read_discovery_index
from baibai_engine.machine_analysis.screening.policies.discovery import (
    evaluate_discovery_rows,
    project_discovery_row,
)
from baibai_engine.machine_analysis.screening.selection_query import read_publication
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.storage.objects.r2 import Boto3R2Store
from baibai_engine.storage.snapshots.keys import current_release_pointer_key
from baibai_engine.storage.snapshots.models import canonical_lake_model_bytes
from baibai_engine.storage.snapshots.objects import R2ObjectSource, R2ReadCredentials
from baibai_engine.storage.snapshots.release import create_release
from baibai_engine.storage.snapshots.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.sqlite.machine_analysis import RUN_STORE_SCHEMA_VERSION
from baibai_engine.storage.sqlite.snapshot import create_snapshot, validate_snapshot
from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    LakeStoreOrigin,
    read_lake_store_origin,
    write_lake_store_origin,
)
from baibai_engine.storage.sqlite.source_cache.schema import SQLITE_SCHEMA_VERSION
from baibai_jobs.publication import machine_current as machine
from baibai_jobs.publication import policy as publication_policy
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.lease import LeaseError, acquire, release, verify_held
from baibai_jobs.publication.machine_pull import pull_current_machine
from baibai_jobs.publication.snapshot_release import read_pointer_snapshot, stage_release
from tests.engine.machine_analysis.screening.test_financial_growth import payloads as financial_models
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.lake_release import market_store
from tests.helpers.model_generation import publish_generation
from tests.helpers.technical_discovery import _models as technical_models


ROOT = Path(__file__).resolve().parent
OFFICIAL_HEAD = "2490e9fa31a9a620567d74fb4cf4a2574441e95d"
OFFICIAL_TREE = "485efb5cbac2b0717bfb2c82d5bac4c54d6dfa52"
DATASETS = ("jquants.daily_bars", "jquants.short_sale_reports", "jquants.market_calendar")
HEAD = current_release_pointer_key()
DATE = date(2026, 11, 13)
BUCKET = "offline-machine-fixture"
FIXTURE_ENV = {
    "R2_ACCOUNT_ID": "a" * 32,
    "R2_ACCESS_KEY_ID": "offlinefixtureonly",
    "R2_SECRET_ACCESS_KEY": "not-a-real-credential",
    "R2_STOP_ON_REJECTION": "1",
}
PART = 64 * 1024**2
CHUNK = 8 * 1024**2
PADDING_ROWS = 5240


def progress(message):
    print(message, file=sys.stderr, flush=True)


def available(path):
    values = os.statvfs(path)
    return values.f_bavail * values.f_frsize


def bytes_digest(path):
    with path.open("rb") as f:
        return file_digest(f, "sha256").hexdigest()


def physical(path):
    info = path.stat()
    return dict(bytes=info.st_size, allocated=info.st_blocks * 512, inode=info.st_ino)


def forbid_network(*_args, **_kwargs):
    raise AssertionError("offline fixture tried to open a real network connection")


class Raw:
    """Minimal HTTP response-body API for botocore's real streaming wrapper."""

    def __init__(self, value=b"", *, path=None, metric=None):
        self.fd = BytesIO(value) if path is None else path.open("rb")
        self.metric = metric

    def read(self, size=None):
        # GET of the large Runs object must not request unbounded bytes.
        if self.metric is not None:
            assert size is not None and 0 < size <= CHUNK, size
        payload = self.fd.read(size)
        if self.metric is not None:
            self.metric["get_reads"] += 1
            self.metric["get_bytes"] += len(payload)
            self.metric["largest_get_read"] = max(self.metric["largest_get_read"], len(payload))
        return payload

    def stream(self, amt=CHUNK, **_kwargs):
        try:
            while block := self.read(amt):
                yield block
        finally:
            self.close()

    def tell(self):
        return self.fd.tell()

    def close(self):
        self.fd.close()


@dataclass
class Blob:
    path: Path
    etag: str
    headers: dict[str, str]


@dataclass
class Upload:
    key: str
    path: Path
    headers: dict[str, str]
    size: int = 0
    tags: list[str] = field(default_factory=list)
    sha: object = field(default_factory=sha256)
    md5: object = field(default_factory=lambda: md5(usedforsecurity=False))


class DiskS3:
    """Independent regular files; never hardlink or buffer a full oversized body."""

    def __init__(self, dir):
        self.dir = dir
        self.blobs = {}
        self.uploads = {}
        self.calls = []
        self.committed_heads = []
        self.part_lengths = []
        self.part_bytes = 0
        self.part_head_etags = set()
        self.multipart_head_etags = {}
        self.completion_conditions = {}
        self.get_metrics = {
            "get_reads": 0,
            "get_bytes": 0,
            "largest_get_read": 0,
        }
        self.get_pins = []
        self.mismatch_next_get = None
        self.injected = 0
        self.large_key = None

    @staticmethod
    def _headers(request):
        return {
            name.lower(): value.decode("ascii") if isinstance(value, bytes) else str(value)
            for name, value in request.headers.items()
        }

    @staticmethod
    def _reply(request, status, *, headers=None, value=b"", path=None, metric=None):
        if headers is None:
            headers = {"content-length": str(len(value))}
        return AWSResponse(request.url, status, headers, Raw(value, path=path, metric=metric))

    def _error(self, request, status, code):
        payload = f"<Error><Code>{code}</Code></Error>".encode()
        return self._reply(
            request, status, headers={"content-type": "application/xml"}, value=payload
        )

    @staticmethod
    def _object_headers(hdr):
        return {
            name: val
            for name, val in hdr.items()
            if name == "content-type" or name.startswith("x-amz-meta-")
        }

    def _path(self):
        return self.dir / uuid4().hex

    def _current_etag(self):
        blob = self.blobs.get(HEAD)
        return None if blob is None else blob.etag

    def _conditions_ok(self, key, hdr):
        existing = self.blobs.get(key)
        return (
            ("if-none-match" not in hdr or (hdr["if-none-match"] == "*" and existing is None))
            and ("if-match" not in hdr or
                 (existing is not None and hdr["if-match"] == '\"' + existing.etag + '\"'))
        )

    def _send(self, request):
        url = urlsplit(request.url)
        assert url.scheme == "https"
        host = FIXTURE_ENV["R2_ACCOUNT_ID"] + ".r2.cloudflarestorage.com"
        if url.hostname == host:
            prefix = f"/{BUCKET}/"
            assert url.path.startswith(prefix), (url.hostname, url.path)
            key = unquote(url.path.removeprefix(prefix))
        else:
            assert url.hostname == f"{BUCKET}.{host}", url.hostname
            key = unquote(url.path.removeprefix("/"))
        assert key, (request.method, url.path)
        hdr = self._headers(request)
        assert hdr["authorization"].startswith("AWS4-HMAC-SHA256 ")
        assert "Credential=offlinefixtureonly/" in hdr["authorization"]
        signed_headers = hdr["authorization"].split("SignedHeaders=")[1].split(",")[0].split(";")
        for signed in ("content-md5", "if-match", "if-none-match"):
            if signed in hdr:
                assert signed in signed_headers, (request.method, signed)
        q = parse_qs(url.query, keep_blank_values=True)
        method = request.method

        if method in {"HEAD", "GET"}:
            self.calls.append((method, key))
            stored = self.blobs.get(key)
            if stored is None:
                return self._error(request, 404, "NoSuchKey")
            if not self._conditions_ok(key, hdr):
                return self._error(request, 412, "PreconditionFailed")
            result = dict(stored.headers)
            if method == "HEAD":
                return self._reply(request, 200, headers=result)
            if "if-match" in hdr:
                self.get_pins.append((key, hdr["if-match"], int(result["content-length"])))
            if self.mismatch_next_get == key:
                self.mismatch_next_get = None
                self.injected += 1
                result["etag"] = '"fixture-mismatched-response"'
            return self._reply(
                request, 200, headers=result, path=stored.path,
                metric=self.get_metrics if key == self.large_key else None,
            )

        if method == "DELETE" and "uploadId" in q:
            self.calls.append(("ABORT_MULTIPART", key))
            upload = self.uploads.pop(q["uploadId"][0])
            assert upload.key == key
            upload.path.unlink()
            return self._reply(request, 204)

        if method == "DELETE":
            self.calls.append(("DELETE", key))
            assert not q and key != HEAD
            self.blobs.pop(key).path.unlink()
            return self._reply(request, 204)

        if method == "POST" and "uploads" in q:
            self.calls.append(("CREATE_MULTIPART", key))
            assert key.endswith("/runs.sqlite")
            assert key not in self.blobs
            upload_id = uuid4().hex
            target = self._path()
            target.touch(exist_ok=False)
            obj_headers = self._object_headers(hdr)
            assert obj_headers["x-amz-meta-integrity"] == "part-md5-v1"
            self.uploads[upload_id] = Upload(key, target, obj_headers)
            self.multipart_head_etags["create"] = self._current_etag()
            return self._reply(
                request, 200, value=(
                    f"<InitiateMultipartUploadResult><UploadId>{upload_id}</UploadId>"
                    "</InitiateMultipartUploadResult>"
                ).encode()
            )

        if method == "PUT" and "partNumber" in q:
            self.calls.append(("UPLOAD_PART", key))
            upload = self.uploads[q["uploadId"][0]]
            number = int(q["partNumber"][0])
            assert upload.key == key and number == len(upload.tags) + 1
            wire_length = int(hdr["content-length"])
            assert 0 < wire_length <= PART
            # Botocore converts the original bytes into a seekable BytesIO.
            body = request.body if isinstance(request.body, bytes) else request.body.read(wire_length)
            assert len(body) == wire_length
            assert isinstance(request.body, bytes) or not request.body.read(1)
            assert number == 1 or self.part_lengths[-1] == PART
            part_digest = md5(body, usedforsecurity=False)
            assert hdr["content-md5"] == base64.b64encode(part_digest.digest()).decode()
            with upload.path.open("ab") as target:
                assert target.write(body) == len(body)
            upload.tags.append(part_digest.hexdigest())
            upload.size += len(body)
            upload.sha.update(body)
            upload.md5.update(body)
            self.part_lengths.append(len(body))
            self.part_bytes += len(body)
            self.part_head_etags.add(self._current_etag())
            return self._reply(request, 200, headers={"etag": f'"{part_digest.hexdigest()}"'})

        if method == "POST" and "uploadId" in q:
            self.calls.append(("COMPLETE_MULTIPART", key))
            assert hdr.get("if-none-match") == "*"
            assert self._conditions_ok(key, hdr)
            upload = self.uploads.pop(q["uploadId"][0])
            assert upload.key == key and len(upload.tags) >= 2
            parts = ElementTree.fromstring(request.body).findall(".//{*}Part")
            assert [
                (int(p.findtext("{*}PartNumber")), p.findtext("{*}ETag").strip('"')) for p in parts
            ] == list(enumerate(upload.tags, 1))
            actual_sha = upload.sha.hexdigest()
            actual_md5 = base64.b64encode(upload.md5.digest()).decode()
            assert actual_sha == upload.headers["x-amz-meta-sha256"]
            assert actual_md5 == upload.headers["x-amz-meta-content-md5"]
            joined = b"".join(bytes.fromhex(tag) for tag in upload.tags)
            tag = f"{md5(joined, usedforsecurity=False).hexdigest()}-{len(upload.tags)}"
            with upload.path.open("rb") as file:
                os.fsync(file.fileno())
            self.blobs[key] = Blob(
                upload.path, tag,
                upload.headers | {"etag": f'"{tag}"', "content-length": str(upload.size)},
            )
            self.large_key = key
            self.completion_conditions = {"if_none_match": hdr["if-none-match"]}
            self.multipart_head_etags["complete"] = self._current_etag()
            return self._reply(
                request, 200,
                value=(
                    f'<CompleteMultipartUploadResult><ETag>"{tag}"</ETag>'
                    '</CompleteMultipartUploadResult>'
                ).encode(),
            )

        assert method == "PUT" and not q, (method, q)
        self.calls.append((method, key))
        assert ("if-match" in hdr) != ("if-none-match" in hdr)
        if not self._conditions_ok(key, hdr):
            return self._error(request, 412, "PreconditionFailed")
        target = self._path()
        sha, digest, total = sha256(), md5(usedforsecurity=False), 0
        with target.open("xb") as f:
            while block := request.body.read(CHUNK):
                assert f.write(block) == len(block)
                sha.update(block)
                digest.update(block)
                total += len(block)
        assert 0 < total <= machine.MAX_MACHINE_PUT_BYTES
        assert int(hdr["content-length"]) == total
        assert hdr["content-md5"] == base64.b64encode(digest.digest()).decode()
        assert hdr["x-amz-meta-content-md5"] == hdr["content-md5"]
        assert hdr["x-amz-meta-sha256"] == sha.hexdigest()
        assert hdr["x-amz-meta-integrity"] == "content-md5-v1"
        tag = digest.hexdigest()
        past = self.blobs.get(key)
        self.blobs[key] = Blob(
            target, tag,
            self._object_headers(hdr) | {"etag": f'"{tag}"', "content-length": str(total)},
        )
        if key == HEAD:
            self.committed_heads.append(
                {"if_match": hdr.get("if-match"), "if_none_match": hdr.get("if-none-match"),
                 "previous_etag": None if past is None else past.etag,
                 "etag": tag, "sha256": sha.hexdigest(), "signed_if_match": "if-match" in signed_headers}
            )
        if past is not None:
            past.path.unlink()
        return self._reply(request, 200, headers={"etag": f'"{tag}"'})


class Capacity:
    def __init__(self, path):
        self.path = path
        self.samples = 0
        self.minimum = available(path)
        self.phases = {}
        self.stop = Event()

    @contextmanager
    def observe(self, phase):
        initial = available(self.path)
        low = initial
        start = monotonic()
        self.stop.clear()

        def monitor():
            nonlocal low
            while not self.stop.wait(0.08):
                self.samples += 1
                low = min(low, available(self.path))

        thread = Thread(target=monitor, daemon=True)
        progress(f"PAIR_PHASE {phase} start")
        thread.start()
        try:
            yield
        finally:
            self.stop.set()
            thread.join()
            finish = available(self.path)
            self.minimum = min(low, finish, self.minimum)
            self.phases[phase] = {
                "free_before": initial, "free_min_sampled": min(low, finish),
                "free_after": finish, "elapsed_seconds": round(monotonic() - start, 3),
            }
            progress(f"PAIR_PHASE {phase} done")
            assert self.minimum > 2 * 10**9, "shared disk safety margin exhausted"


def restricted_release(monkey):
    narrow_release_policy(monkey, datasets=DATASETS)
    configured = publication_policy.PRODUCTION_RELEASE_POLICY
    monkey.setattr(
        publication_policy, "PRODUCTION_RELEASE_POLICY",
        configured.model_copy(update={"datasets": tuple(
            i.model_copy(update={"require_complete_coverage": False})
            if i.dataset == "jquants.market_calendar" else i for i in configured.datasets
        )}),
    )


def market_release(folder, *, release_id, close, store):
    folder.mkdir()
    path = market_store(
        folder / "market.sqlite",
        daily_bars=(("1234", "2026-11-12", 100.0, 800.0),
                    ("1234", DATE.isoformat(), close, 1600.0)),
        short_sale_reports=(("2026-11-13", 0, "2026-11-12", "1234", "Synthetic fund", 0.006, 600, 6),),
        short_sale_coverage=("test:pair", "2026-11-12", DATE.isoformat(),
                             "2026-11-14T00:00:00+00:00", 1),
    )
    with closing(sqlite3.connect(path)) as db, db:
        db.executemany("INSERT INTO jquants_market_calendar(day,holiday_division) VALUES(?,?)",
                       [("2026-11-12", 1), ("2026-11-13", 1), ("2026-11-14", 0)])
    mirror = folder / "mirror"
    clock = datetime(2026, 11, 14, 9, tzinfo=UTC)
    with sealed_sqlite_snapshot(
        sqlite_path=path, mirror_root=mirror, snapshot_id=release_id
    ) as source:
        manifests = [
            export_dataset_snapshot(
                dataset=LAKE_DATASETS[name], mirror_root=mirror,
                producer_git_commit=OFFICIAL_HEAD, source_snapshot=source,
                build_id=release_id + "-" + name.replace(".", "-"), created_at=clock,
            ).manifest_path
            for name in DATASETS
        ]
    manifest_path, manifest = create_release(
        dataset_manifest_paths=manifests, mirror_root=mirror, release_id=release_id,
        created_at=clock, catalog=LAKE_DATASETS,
        policy=publication_policy.release_policy_for_profile("production"),
    )
    proved = stage_release(
        mirror_root=mirror, release_manifest_path=manifest_path, store=store,
        base=read_pointer_snapshot(store), expected_release_sha256=bytes_digest(manifest_path),
    )
    pointer = proved.pointer
    assert pointer.release_id == manifest.release_id == release_id
    with closing(sqlite3.connect(path)) as db, db:
        write_lake_store_origin(db, LakeStoreOrigin(release_id, pointer.manifest_sha256))
    return path, proved


def seed_runs(path, pointer, close):
    generation_id, selection_id = machine.machine_publication_ids(pointer)
    finance = financial_models()
    technical, context = technical_models()
    technical.payload["price_path"]["current_price"] = close
    context.payload["master"].update({"name": "Synthetic stock", "sector_33": "情報・通信業"})
    context.payload["price"].update(
        {"close": close, "resolved_date": DATE.isoformat(), "price_basis": "price_adjusted_close"}
    )
    models = (
        technical, context,
        *(GeneratedModel(model_id=name, subject="1234", calculation_revision="test-v1", payload=body)
          for name, body in finance.items()),
        GeneratedModel(model_id="market_context", subject="market", calculation_revision="test-v1",
                       payload={"series": [{"value": 12.5, "unit": "percent"}]}),
    )
    metadata = GenerationMetadata(
        generation_id=generation_id, as_of=DATE,
        evaluation_time=datetime(2026, 11, 14, 8, 41, tzinfo=UTC),
        schema_version=2,
        inputs={"market": InputSnapshot(kind="lake_release", reference=pointer.release_id,
                                        sha256=pointer.manifest_sha256)},
        subject_plan={
            "1234": GenerationSubject(scope="stock", model_ids=tuple(
                m.model_id for m in models if m.subject == "1234"
            ), related_subjects=("market",)),
            "market": GenerationSubject(scope="market", model_ids=("market_context",)),
        },
    )
    generation = ModelGeneration(**dict(metadata), models=models)
    assert publish_generation(generation, path=path) is True
    row = project_discovery_row("1234", models=tuple(m for m in models if m.subject == "1234"),
                                metadata=metadata)
    assert row["models_recorded"] and row["price_ready"]
    assert row["technical_state"] == "matched" and row["price"] == close
    selection = evaluate_discovery_rows(metadata, tickers=("1234",), rows={"1234": row})
    ScreeningSelectionStore(path).publish(selection_id, selection)
    return generation, selection, selection_id


def pad(path, rows):
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA page_size").fetchone() == (4096,)
        assert db.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        db.execute("CREATE TABLE synthetic_storage_load (id INTEGER PRIMARY KEY, content BLOB NOT NULL)")
        for start in range(0, rows, 32):
            db.executemany("INSERT INTO synthetic_storage_load(content) VALUES(randomblob(1048576))",
                           [()] * min(32, rows - start))
            db.commit()
        assert db.execute("PRAGMA freelist_count").fetchone() == (0,)


def _negative(label, expected_error, callback):
    try:
        callback()
    except expected_error:
        return label
    raise AssertionError(f"{label} did not stop")


def run(folder, *, rows, expect_multipart):
    capacity = Capacity(folder)
    before = available(folder)
    assert before >= (14_000_000_000 if rows >= 5000 else 3_000_000_000), before
    remote_dir, pull_dir = folder / "receiver", folder / "consumer"
    remote_dir.mkdir()
    pull_dir.mkdir()
    os.environ["TMPDIR"] = str(folder)
    import tempfile
    tempfile.tempdir = str(folder)
    assert len({p.stat().st_dev for p in (folder, remote_dir, pull_dir)}) == 1
    assert folder.stat().st_dev != Path("/dev/shm").stat().st_dev
    peer = DiskS3(remote_dir)
    negatives = []

    def intercepted_send(_http, request):
        return peer._send(request)

    with (
        pytest.MonkeyPatch.context() as monkey,
        patch.object(URLLib3Session, "send", intercepted_send),
        patch.object(socket.socket, "connect", forbid_network),
        patch.object(socket.socket, "connect_ex", forbid_network),
        patch.object(socket, "getaddrinfo", forbid_network),
        patch.object(socket, "create_connection", forbid_network),
        closing(Boto3R2Store(bucket=BUCKET, env=FIXTURE_ENV)) as writer,
        closing(Boto3R2Store(bucket=BUCKET, env=FIXTURE_ENV)) as reader,
        closing(R2ObjectSource(R2ReadCredentials(
            FIXTURE_ENV["R2_ACCOUNT_ID"], FIXTURE_ENV["R2_ACCESS_KEY_ID"],
            FIXTURE_ENV["R2_SECRET_ACCESS_KEY"], BUCKET,
        ))) as lake_source,
    ):
        restricted_release(monkey)
        handle, expired = acquire(writer, "operator")
        assert not expired
        held = []

        def while_held():
            verify_held(writer, handle)
            held.append(peer._current_etag())

        old_market, old_lake = market_release(folder / "old", release_id="pair-incumbent",
                                              close=108.0, store=writer)
        old_runs = old_market.with_name("runs.sqlite")
        _, _, old_selection_id = seed_runs(old_runs, old_lake.pointer, 108.0)
        old_base = machine.finish_machine_retirement(writer, old_lake.base, while_held=while_held)
        old, retired = machine.publish_staged_machine(
            writer, lake=old_lake, market=old_market, runs=old_runs, while_held=while_held, base=old_base
        )
        assert retired and old.detached == ()
        assert old.current.pointer is not None and old.current.pointer.machine is not None
        incumbent_members = old.current.pointer.machine
        old_etag = old.current.precondition.etag
        assert peer.committed_heads[0]["if_none_match"] == "*"
        with pull_current_machine(reader, lake_source, directory=pull_dir) as incumbent_read:
            assert incumbent_read.current == old.current
            assert read_discovery_index(path=incumbent_read.runs).selection_id == old_selection_id
        assert not tuple(pull_dir.iterdir())
        progress("offline pair: incumbent pinned read passed")

        new_market, new_lake = market_release(folder / "new", release_id="pair-candidate",
                                              close=110.0, store=writer)
        working = new_market.with_name("working-runs.sqlite")
        sealed = new_market.with_name("runs.sqlite")
        generation, selection, selection_id = seed_runs(working, new_lake.pointer, 110.0)
        with capacity.observe("sqlite_fixture_padding"):
            pad(working, rows)
        with capacity.observe("real_compact_snapshot"):
            create_snapshot(working, sealed, compact=True)
        assert validate_snapshot(sealed) == RUN_STORE_SCHEMA_VERSION
        assert not any(
            sealed.with_name(sealed.name + suffix).exists() and
            sealed.with_name(sealed.name + suffix).stat().st_size > 0
            for suffix in ("-wal", "-journal")
        )
        original_sizes = {"market": physical(new_market), "runs": physical(sealed)}
        assert original_sizes["runs"]["allocated"] >= original_sizes["runs"]["bytes"]
        assert (original_sizes["runs"]["bytes"] > machine.MAX_MACHINE_PUT_BYTES) == expect_multipart
        if expect_multipart and rows == PADDING_ROWS:
            assert original_sizes["runs"]["bytes"] > 5_400_000_000
        assert not working.samefile(sealed)
        working.unlink()
        assert not working.exists()
        originals = {"market": bytes_digest(new_market), "runs": bytes_digest(sealed)}
        progress(f"offline pair: candidate Runs sealed, bytes={original_sizes['runs']['bytes']}")

        base = machine.finish_machine_retirement(writer, new_lake.base, while_held=while_held)
        assert base.snapshot == old.current
        def stage(market, runs, lease):
            return machine.stage_machine_members(
                writer, lake=new_lake, base=base, market=market, runs=runs, while_held=lease
            )

        before_machine_puts = len(peer.calls)
        negatives.append(_negative("wrong_market_origin", machine.LakePublishError,
                                   lambda: stage(old_market, sealed, while_held)))
        negatives.append(_negative("wrong_runs_generation", machine.LakePublishError,
                                   lambda: stage(new_market, old_runs, while_held)))
        if expect_multipart:
            negatives.append(_negative("oversize_without_lease", machine.MachineMemberTooLarge,
                                       lambda: stage(new_market, sealed, None)))
        assert len(peer.calls) == before_machine_puts, "invalid pairs sent a wire operation"
        held.clear()
        with capacity.observe("real_adapter_upload"):
            staged = stage(new_market, sealed, while_held)
        assert staged.pointer.machine is not None
        assert staged.pointer.machine.publication_id != incumbent_members.publication_id
        assert machine.read_machine_current(reader) == old.current
        assert read_pointer_snapshot(reader).payload == old.current.payload
        assert len(peer.committed_heads) == 1
        assert ("PUT", staged.pointer.machine.runs.key) not in peer.calls if expect_multipart else not peer.part_lengths
        if expect_multipart:
            assert peer.part_head_etags == {old_etag}
            assert peer.multipart_head_etags == {"create": old_etag, "complete": old_etag}
            assert held == [old_etag, old_etag], held
            assert peer.part_bytes == original_sizes["runs"]["bytes"]
            assert sum(peer.part_lengths) == peer.part_bytes
            assert peer.part_lengths[:-1] == [PART] * (len(peer.part_lengths) - 1)
            assert peer.large_key == staged.pointer.machine.runs.key
        remote_path = peer.blobs[staged.pointer.machine.runs.key].path
        remote_size = physical(remote_path)
        # Normal filesystems may reserve an extra extent, so allocated can be
        # above logical EOF. Require non-sparse allocation plus exact logical size.
        assert remote_size["allocated"] >= remote_size["bytes"] == original_sizes["runs"]["bytes"], remote_size
        assert remote_size["inode"] != original_sizes["runs"]["inode"]
        sealed.unlink()
        assert not sealed.exists()
        progress("offline pair: multipart/stage finished; old pointer is still current")

        negatives.append(_negative(
            "stale_lease_prevents_cas", LeaseError,
            lambda: machine.activate_machine(
                writer, staged,
                before_switch=lambda: verify_held(writer, replace(handle, etag="stale-fixture")),
            ),
        ))
        assert len(peer.committed_heads) == 1
        assert sum(method == "PUT" and key == HEAD for method, key in peer.calls) == 1
        assert machine.read_machine_current(reader) == old.current

        committed = machine.activate_machine(writer, staged, before_switch=while_held)
        assert committed.current.pointer == staged.pointer
        candidate_bytes = canonical_lake_model_bytes(staged.pointer)
        assert committed.current.payload == candidate_bytes
        assert machine.read_machine_current(reader) == committed.current
        assert held[-1] == old_etag
        assert len(peer.committed_heads) == 2
        assert sum(method == "PUT" and key == HEAD for method, key in peer.calls) == 2
        assert peer.committed_heads[1]["if_match"] == f'"{old_etag}"'
        assert peer.committed_heads[1]["signed_if_match"]
        assert peer.committed_heads[1]["sha256"] == sha256(candidate_bytes).hexdigest()
        assert committed.detached == (incumbent_members.market.key, incumbent_members.runs.key)
        machine.finish_machine_retirement(writer, committed.current, while_held=while_held)
        assert [key for method, key in peer.calls if method == "DELETE"] == list(committed.detached)
        assert all(k not in peer.blobs for k in committed.detached)
        assert old_lake.pointer.manifest_key in peer.blobs
        assert not any(".bak" in key for key in peer.blobs)
        assert not peer.uploads
        progress("offline pair: exactly one signed CAS, only incumbent pair retired")

        candidate_runs = staged.pointer.machine.runs
        # Request precondition succeeds; a mismatched response ETag must still fail
        # before the download body is read or the context yields partially ready files.
        from baibai_engine.storage.objects.r2 import R2PinnedObjectMismatch
        peer.mismatch_next_get = candidate_runs.key
        with pytest.raises(R2PinnedObjectMismatch):
            with pull_current_machine(reader, lake_source, directory=pull_dir):
                raise AssertionError("partially accepted pair")
        assert peer.injected == 1 and not tuple(pull_dir.iterdir())
        assert peer.get_metrics["get_bytes"] == 0
        negatives.append("same_key_wrong_response_etag_never_exposed")

        with capacity.observe("real_adapter_download_and_readback"):
            with pull_current_machine(reader, lake_source, directory=pull_dir) as accepted:
                assert accepted.current == committed.current
                assert accepted.release.release_id == staged.pointer.release_id
                assert accepted.release.manifest_sha256 == staged.pointer.manifest_sha256
                assert set(accepted.release.manifest.datasets) == set(DATASETS)
                assert read_lake_store_origin(accepted.market) == LakeStoreOrigin(
                    accepted.release.release_id, accepted.release.manifest_sha256
                )
                assert validate_snapshot(accepted.market) == SQLITE_SCHEMA_VERSION
                assert validate_snapshot(accepted.runs) == RUN_STORE_SCHEMA_VERSION
                with closing(sqlite3.connect(accepted.market)) as db:
                    assert db.execute(
                        "SELECT close FROM jquants_daily_bars WHERE ticker=? AND traded_at=?",
                        ("1234", DATE.isoformat())
                    ).fetchone() == (110.0,)
                out = physical(accepted.runs)
                assert out["allocated"] >= out["bytes"] == original_sizes["runs"]["bytes"], out
                assert out["inode"] != remote_size["inode"]
                assert not sealed.exists(), "source must not exist during the independent read"
                assert not accepted.runs.samefile(remote_path)
                assert bytes_digest(accepted.runs) == originals["runs"]
                assert bytes_digest(accepted.market) == originals["market"]
                provenance = read_publication(path=accepted.runs, selection_id=selection_id)
                assert provenance is not None and provenance.selection == selection
                assert provenance.generation.inputs["market"] == generation.inputs["market"]
                stored_models = read_generation(generation.generation_id, path=accepted.runs)
                assert stored_models == generation
                discovery = read_discovery_index(path=accepted.runs)
                assert discovery.status == "indexed"
                assert discovery.selection_id == selection_id
                assert discovery.as_of == DATE and discovery.universe_tickers == ("1234",)
                assert discovery.rows == selection.result.rows
                with closing(sqlite3.connect(accepted.runs)) as db:
                    assert db.execute("SELECT COUNT(*), MIN(length(content)), MAX(length(content)) "
                                      "FROM synthetic_storage_load").fetchone() == (rows, 1048576, 1048576)
        assert not tuple(pull_dir.iterdir())
        if expect_multipart:
            assert peer.get_metrics["get_bytes"] == original_sizes["runs"]["bytes"]
            assert (peer.get_metrics["largest_get_read"], peer.get_metrics["get_reads"]) == (
                CHUNK, -(-original_sizes["runs"]["bytes"] // CHUNK) + 1,
            )
        runs_pins = [fields for fields in peer.get_pins if fields[0] == candidate_runs.key]
        assert runs_pins == [(candidate_runs.key, f'"{candidate_runs.etag}"', candidate_runs.size)] * 2

        activity = len(peer.calls)
        with pull_current_machine(reader, lake_source, directory=pull_dir, portfolio_only=True) as portfolio:
            assert portfolio.runs is None and portfolio.current == committed.current
        assert not any(key == candidate_runs.key for _, key in peer.calls[activity:])
        release(writer, handle)
        progress("offline pair: independent roundtrip, provenance and model/selection read passed")

    row = discovery.rows[0]
    return {
        "status": "pass",
        "basis_commit": OFFICIAL_HEAD,
        "verified_tree": OFFICIAL_TREE,
        "runtime": {"python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version,
                    "boto3": boto3.__version__, "botocore": botocore.__version__,
                    "duckdb": duckdb.__version__},
        "boundaries": {
            "mock_at": "botocore.httpsession.URLLib3Session.send",
            "socket_access": "blocked before all Boto clients",
            "real_r2_or_production_or_runner": "not used, not proven",
            "release_policy": "3 real Parquet datasets, narrowed synthetic policy (calendar and carry history relaxed)",
            "market_bytes": "real SQLite schema and synthetic observations, not production volume",
            "runs_bytes": "real SQLite writer and read contracts; 1MiB random padding rows, not real production composition",
            "does_not_prove": "GitHub runner hydration/capacity, Cloudflare service/retries/TLS, L3 production proposal, live cutover",
        },
        "fixture_release_datasets": sorted(DATASETS),
        "size_threshold_bytes": machine.MAX_MACHINE_PUT_BYTES,
        "sqlite_fixture_padding_rows": rows,
        "sealed_market": {k: v for k, v in original_sizes["market"].items() if k != "inode"},
        "sealed_runs": {k: v for k, v in original_sizes["runs"].items() if k != "inode"},
        "roundtrip_content": {
            "market_sha256": originals["market"], "runs_sha256": originals["runs"],
            "same_sha256_in_independent_files": True, "quick_check": "ok",
            "receiver_allocated_bytes": remote_size["allocated"],
            "independent_pull_allocated_bytes": out["allocated"],
            "market_schema": SQLITE_SCHEMA_VERSION, "runs_schema": RUN_STORE_SCHEMA_VERSION,
            "lake_release_id": committed.current.pointer.release_id,
            "lake_manifest_sha256": committed.current.pointer.manifest_sha256,
            "generation_id": generation.generation_id, "selection_id": selection_id,
            "as_of": DATE.isoformat(), "model_subjects": [[m.model_id, m.subject] for m in generation.models],
            "discovery_rows": len(discovery.rows),
            "discovery_witness": {field: row[field] for field in
                                  ("ticker", "price", "price_date", "basis", "technical_state",
                                   "financial_state", "screening_lane", "price_ready")},
        },
        "multipart": {
            "observed": expect_multipart, "part_calls": len(peer.part_lengths),
            "part_bytes": peer.part_bytes, "standard_part_bytes": PART,
            "last_part_bytes": peer.part_lengths[-1] if peer.part_lengths else None,
            "head_unchanged_through_create_all_parts_and_complete": expect_multipart,
            "complete_condition": peer.completion_conditions,
            "complete_calls": sum(m == "COMPLETE_MULTIPART" for m, _ in peer.calls),
            "runs_pinned_download": peer.get_metrics,
        },
        "switch": {
            "wire_head_puts_bootstrap_plus_candidate": 2,
            "wire_head_puts_for_candidate": 1,
            "candidate_uses_signed_incumbent_etag": True,
            "prerequisite_stale_lease_sends_no_put": True,
            "relies_on_exact_pointer_body_readback": True,
            "retired_old_member_count": len(committed.detached),
            "old_lake_remains": True,
            "backup_keys": 0,
            "no_runs_request_for_portfolio": True,
        },
        "negative_cases": negatives,
        "transport_observed_event_counts": dict(Counter(method for method, _ in peer.calls)),
        "filesystem": {
            "same_device": True, "not_tmpfs": True, "sampler_interval_seconds": 0.08,
            "free_at_start": before, "free_min_sampled": capacity.minimum,
            "peak_allocated_sampled_estimate": before - capacity.minimum,
            "sample_count": capacity.samples, "phases": capacity.phases,
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--multipart-preflight", action="store_true")
    args = parser.parse_args()
    repo = Path(os.environ.get("BAIBAI_PAIR_REPO", Path.cwd())).resolve()
    # Works for either the locally reconstructed index or the clean exact SHA checkout.
    tree = subprocess.check_output(["git", "-C", str(repo), "write-tree"], text=True).strip()
    assert tree == OFFICIAL_TREE, tree
    subprocess.run(["git", "-C", str(repo), "diff", "--quiet", "--"], check=True)
    for observed in (machine, sys.modules[Boto3R2Store.__module__],
                     sys.modules[create_snapshot.__module__]):
        assert Path(observed.__file__).resolve().is_relative_to(repo), observed.__name__
    workdir = Path(os.environ.get("BAIBAI_PAIR_WORKDIR", ROOT)).resolve()
    assert workdir.is_dir()
    assert not any(n.startswith(("AWS_", "R2_", "GH_", "GITHUB_")) for n in os.environ)
    start = monotonic()
    if args.multipart_preflight:
        # Quick transport-harness debugging only, never accepted as full-size evidence.
        PART = 8 * 1024**2
        machine.MAX_MACHINE_PUT_BYTES = 10 * 1024**2
        sys.modules[Boto3R2Store.__module__]._MULTIPART_PART_BYTES = PART
    rows = 12 if args.preflight or args.multipart_preflight else PADDING_ROWS
    with TemporaryDirectory(dir=workdir, prefix="disposable-only-") as disposable:
        summary = run(Path(disposable), rows=rows, expect_multipart=not args.preflight)
    summary["filesystem"]["free_after_owned_fixture_cleanup"] = available(workdir)
    summary["elapsed_seconds"] = round(monotonic() - start, 3)
    name = ("preflight-summary.json" if args.preflight
            else "multipart-preflight-summary.json" if args.multipart_preflight
            else "sanitized-summary.json")
    report = json.dumps(summary, indent=2, ensure_ascii=False) + "\n"
    (ROOT / name).write_text(report)
    print(report, end="", flush=True)
