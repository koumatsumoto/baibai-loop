"""Reuse the publisher's actual GET, not an unverified local export."""

import hashlib
from pathlib import Path

import pytest

from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_jobs.publication.snapshot_publication import _fetch, daily_external_readback_dir
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
    ],
)
def test_actual_publication_readback_reused_only_for_the_same_daily_release(tmp_path, change):
    key = "lake/l2/processed/equity_web.source_responses/part.parquet"
    payload = b"a" * 1024**2
    sha256 = hashlib.sha256(payload).hexdigest()
    remote = _Remote(payload)
    scope = daily_external_readback_dir(tmp_path, bucket="stores", run="1-1")
    root = scope / ("c" * 64)
    publication = _RemotePublication(
        store=remote, verified_readback_root=root, verified_readback_keys=frozenset({key})
    )
    publication._require_bytes(key=key, expected_sha256=sha256, expected_size=len(payload))
    saved = root / key
    assert saved.read_bytes() == payload
    assert publication.get_requests == 1
    assert publication.downloaded_bytes == len(payload)

    if change == "other-run":
        scope = daily_external_readback_dir(tmp_path, bucket="stores", run="2-1")
    if change == "other-attempt":
        scope = daily_external_readback_dir(tmp_path, bucket="stores", run="1-2")
    if change == "other-bucket":
        scope = daily_external_readback_dir(tmp_path, bucket="other", run="1-1")
    if change == "missing":
        saved.unlink()
    if change == "damaged":
        saved.write_bytes(b"b" + payload[1:])  # Same length, wrong SHA.
    readback_root = (
        tmp_path / "external-readback"
        if change == "independent"
        else scope / (("d" if change == "other-release" else "c") * 64)
    )
    fetched = _fetch(remote, readback_root, key, expected_sha256=sha256, expected_size=len(payload))
    assert fetched.read_bytes() == payload
    assert remote.downloads == [(key, len(payload))] * (1 if change == "none" else 2)


def test_unselected_or_bad_remote_bodies_do_not_become_local_proof(tmp_path):
    payload = b"original"
    remote = _Remote(payload)
    publication = _RemotePublication(
        store=remote,
        verified_readback_root=tmp_path,
        verified_readback_keys=frozenset({"selected"}),
    )
    sha256 = hashlib.sha256(payload).hexdigest()
    publication._require_bytes(key="other", expected_sha256=sha256, expected_size=len(payload))
    assert not (tmp_path / "other").exists()
    remote.body = b"replaced"
    with pytest.raises(LakePublishError, match="remote object bytes postcondition failed"):
        publication._require_bytes(
            key="selected", expected_sha256=sha256, expected_size=len(payload)
        )
    assert not (tmp_path / "selected").exists()
    with pytest.raises(LakePublishError, match="downloaded lake object digest mismatch"):
        _fetch(remote, tmp_path, "selected", expected_sha256=sha256, expected_size=len(payload))
    assert not (tmp_path / "selected").exists()
