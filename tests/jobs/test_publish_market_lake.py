"""Publication binds one SQLite generation to one immutable release graph."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.helpers.r2_store import MemoryR2Store

import baibai_jobs.publication.snapshot_publication as publish_module
from baibai_engine.storage.objects.r2 import (
    LakePublishError,
)
from baibai_engine.storage.snapshots.writer import LakeBuildError
from baibai_jobs.publication.lease import _save_handle, acquire


class _DownloadStore:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.downloads: list[str] = []

    def download_file(self, key: str, path: Path, *, expect_bytes: int | None = None) -> None:
        self.downloads.append(key)
        path.write_bytes(self.payload)


def test_fetch_heals_a_corrupted_manifest_cache_after_digest_validation(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    key = "lake/manifests/releases/market/release.json"
    cached = mirror / key
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"corrupted")
    remote = b'{"valid":true}\n'
    store = _DownloadStore(remote)

    result = publish_module._fetch(
        store,
        mirror,
        key,
        expected_sha256=hashlib.sha256(remote).hexdigest(),
    )

    assert result == cached
    assert cached.read_bytes() == remote
    assert store.downloads == [key]


def test_fetch_does_not_replace_cache_with_wrong_remote_digest(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    key = "lake/manifests/releases/market/release.json"
    cached = mirror / key
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"existing-corruption")
    store = _DownloadStore(b"wrong-remote")

    with pytest.raises(LakePublishError, match="digest mismatch"):
        publish_module._fetch(
            store,
            mirror,
            key,
            expected_sha256=hashlib.sha256(b"expected").hexdigest(),
        )

    assert cached.read_bytes() == b"existing-corruption"
    assert store.downloads == [key]


def test_fetch_reuses_a_cache_with_the_expected_digest(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    key = "lake/manifests/releases/market/release.json"
    cached = mirror / key
    cached.parent.mkdir(parents=True)
    payload = b"already-valid"
    cached.write_bytes(payload)
    store = _DownloadStore(b"must-not-be-read")

    result = publish_module._fetch(
        store,
        mirror,
        key,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )

    assert result == cached
    assert store.downloads == []


def test_export_manifests_are_frozen_from_the_checked_models_inside_the_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = tmp_path / "mirror/lake/manifests/datasets/shared.json"
    shared.parent.mkdir(parents=True)
    shared.write_bytes(b"manifest-before-export")
    checked_model = object()
    report = SimpleNamespace(manifest=checked_model, manifest_path=shared)
    monkeypatch.setattr(
        publish_module,
        "canonical_lake_model_bytes",
        lambda model: b"checked-model\n" if model is checked_model else b"unexpected",
    )

    with publish_module._export_manifest_snapshot(
        {"jquants.daily_bars": report}, tmp_path / "mirror"
    ) as paths:
        shared.write_bytes(b"valid-but-different-manifest\n")

        assert len(paths) == 1
        assert paths[0].read_bytes() == b"checked-model\n"
        assert paths[0].is_relative_to(tmp_path / "mirror")


def test_a_build_error_is_reported_without_prescribing_an_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """What is left here cannot be answered by rebuilding, so it names no recovery."""

    def _refuse(**_: object) -> object:
        raise LakeBuildError("invalid date in sealed SQLite snapshot")

    monkeypatch.setattr(publish_module, "publish_market_lake", _refuse)
    store = MemoryR2Store()
    handle, _ = acquire(store, "operator")
    path = tmp_path / "lease.json"
    _save_handle(path, handle)
    monkeypatch.setattr(publish_module, "Boto3R2Store", lambda **_: store)

    exit_code = publish_module.main(
        ["--sqlite", "market.sqlite", "--mirror", "stores", "--lease-handle", str(path)]
    )

    assert exit_code == 1
    printed = capsys.readouterr().err
    assert "invalid date" in printed
    assert "full rebuild" not in printed


def test_publisher_cli_has_no_sidecar_origin_arguments() -> None:
    parsed = publish_module.build_parser().parse_args(
        [
            "--sqlite",
            "market.sqlite",
            "--mirror",
            "stores",
            "--bucket",
            "baibai-stores",
            "--lease-handle",
            "lease.json",
        ]
    )

    assert not hasattr(parsed, "full_rebuild")
    assert parsed.bucket == "baibai-stores"
    assert not hasattr(parsed, "origin_release")
