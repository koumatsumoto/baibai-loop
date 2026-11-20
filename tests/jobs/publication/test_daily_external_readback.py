"""Reuse the publisher's actual GET, not an unverified local export."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_engine.storage.snapshots.models import LakeObject
from baibai_jobs.integration import models, sources
from baibai_jobs.integration.external_assessments import _SealReadback
from baibai_jobs.publication.snapshot_publication import daily_external_readback_dir
from baibai_jobs.publication.snapshot_release import _RemotePublication


class _Remote:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.downloads: list[tuple[str, int]] = []

    def download_file(self, key: str, path: Path, *, expect_bytes: int | None = None) -> None:
        self.downloads.append((key, len(self.body)))
        if expect_bytes is not None:
            assert len(self.body) == expect_bytes
        path.write_bytes(self.body)


def _item(payload: bytes) -> LakeObject:
    return LakeObject(
        key="lake/l2/processed/equity_web.source_responses/part.parquet",
        sha256=hashlib.sha256(payload).hexdigest(),
        bytes=len(payload),
        rows=1,
        min_key=("1",),
        max_key=("1",),
    )


def _retain(root: Path, item: LakeObject):
    def retain(key: str, path: Path) -> None:
        assert key == item.key
        target = root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        path.replace(target)

    return retain


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "other-run",
        "other-attempt",
        "other-bucket",
        "other-release",
        "independent",
        "missing",
        "damaged",
        "damaged-after-prepare",
    ],
)
def test_actual_publication_get_reused_only_for_the_same_daily_release(tmp_path, change):
    payload = b"a" * 1024**2
    item = _item(payload)
    remote = _Remote(payload)
    scope = daily_external_readback_dir(tmp_path, bucket="stores", run="1-1")
    root = scope / ("c" * 64)
    publication = _RemotePublication(store=remote, retain_verified_readback=_retain(root, item))
    publication._require_bytes(key=item.key, expected_sha256=item.sha256, expected_size=item.bytes)
    saved = root / item.key
    assert saved.read_bytes() == payload
    assert publication.get_requests == 1
    assert publication.downloaded_bytes == item.bytes

    if change == "other-run":
        scope = daily_external_readback_dir(tmp_path, bucket="stores", run="2-1")
    if change == "other-attempt":
        scope = daily_external_readback_dir(tmp_path, bucket="stores", run="1-2")
    if change == "other-bucket":
        scope = daily_external_readback_dir(tmp_path, bucket="other", run="1-1")
    if change == "missing":
        saved.unlink()
    if change == "damaged":
        saved.write_bytes(b"b" + payload[1:])
    release = "d" * 64 if change == "other-release" else "c" * 64
    retained = None if change == "independent" else scope / release
    readback = _SealReadback(remote, tmp_path / "mandatory", retained)
    readback.prepare(item)
    if change == "damaged-after-prepare":
        saved.write_bytes(b"b" + payload[1:])
    assert readback.materialize(item).read_bytes() == payload
    assert remote.downloads == [(item.key, item.bytes)] * (1 if change == "none" else 2)
    assert (readback.root / item.key).is_file() is (change != "none")


def test_optional_retention_failure_neither_repeats_nor_breaks_the_mandatory_get(tmp_path):
    remote = _Remote(b"original")
    item = _item(remote.body)
    denied = tmp_path / "not-a-directory"
    denied.write_bytes(b"not a directory")
    publisher = _RemotePublication(store=remote, retain_verified_readback=_retain(denied, item))
    publisher._require_bytes(key=item.key, expected_sha256=item.sha256, expected_size=item.bytes)
    assert remote.downloads == [(item.key, item.bytes)]
    assert publisher.get_requests == 1

    readback = _SealReadback(remote, tmp_path / "mandatory", denied)
    readback.prepare(item)
    assert readback.materialize(item).read_bytes() == remote.body
    assert remote.downloads == [(item.key, item.bytes)] * 2

    strict = _RemotePublication(store=remote, verified_readback_root=denied)
    with pytest.raises(OSError):
        strict._require_bytes(key=item.key, expected_sha256=item.sha256, expected_size=item.bytes)


def test_bad_remote_body_never_becomes_local_proof(tmp_path):
    item = _item(b"original")
    remote = _Remote(b"replaced")
    publisher = _RemotePublication(store=remote, retain_verified_readback=_retain(tmp_path, item))
    with pytest.raises(LakePublishError, match="remote object bytes postcondition failed"):
        publisher._require_bytes(
            key=item.key, expected_sha256=item.sha256, expected_size=item.bytes
        )
    assert not (tmp_path / item.key).exists()
    readback = _SealReadback(remote, tmp_path / "mandatory", tmp_path)
    with pytest.raises(LakePublishError, match="downloaded lake object digest mismatch"):
        readback.prepare(item)
    assert not (readback.root / item.key).exists()


def test_optional_scratch_setup_is_bounded_and_a_second_owner_is_not_advertised(
    tmp_path, monkeypatch
):
    owner = daily_external_readback_dir(tmp_path, bucket="stores", run="1-1")
    item = _item(b"original")
    partition = SimpleNamespace(values={"year": 2026, "month": 11}, objects=(item,))
    dataset = SimpleNamespace(manifest=SimpleNamespace(partitions=(partition,)))
    prepared = SimpleNamespace(
        release_sha256="c" * 64, export=SimpleNamespace(datasets={"changes": dataset})
    )
    changed = frozenset({("changes", 2026, 11)})

    def disk_usage(_):
        raise PermissionError("optional scratch unavailable")

    with monkeypatch.context() as patches:
        patches.setattr(models.shutil, "disk_usage", disk_usage)
        assert models._external_readback_options(prepared, owner, changed) == {}
        patches.setattr(
            models.shutil, "disk_usage", lambda _: SimpleNamespace(free=16 * item.bytes)
        )
        assert models._external_readback_options(prepared, owner, changed)
        assert models._external_readback_options(prepared, owner, changed) == {}
        other = daily_external_readback_dir(tmp_path, bucket="stores", run="1-2")
        patches.setattr(models.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
        assert models._external_readback_options(prepared, other, changed) == {}


@pytest.mark.parametrize("drop", [False, True])
def test_only_successful_current_notice_allows_retained_readback(tmp_path, monkeypatch, drop):
    monkeypatch.setattr(sources, "current_daily_run", lambda: "1-2")
    sha = "c" * 64
    generations = {"lake_release_id": "current", "lake_manifest_sha256": sha}
    notice = {
        "models_ready": True,
        "daily_run": "1-2",
        "lake": {"release_id": "current", "release_manifest_sha256": sha},
    }
    if not drop:
        notice["daily_external_readback"] = True
    path = tmp_path / "notice.json"
    path.write_text(json.dumps(notice), encoding="utf-8")
    bound = sources._daily_external_readback(
        path, tmp_path, bucket="stores", generations=generations
    )
    expected = daily_external_readback_dir(tmp_path, bucket="stores", run="1-2") / sha
    assert bound == (None if drop else expected)
    notice["daily_run"] = "1-1"
    path.write_text(json.dumps(notice), encoding="utf-8")
    assert (
        sources._daily_external_readback(path, tmp_path, bucket="stores", generations=generations)
        is None
    )
