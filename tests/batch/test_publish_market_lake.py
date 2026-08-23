"""Publication binds one SQLite generation to one immutable release graph."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import baibai_batch.storage.publish_market_lake as publish_module
from baibai_batch.storage.lake_publish import LakePublishError
from baibai_engine.batch_api import (
    L1ReleasePointer,
    LakeBuildError,
    LakeTransformFingerprintMismatch,
)

ROOT = Path(__file__).resolve().parents[2]
_RELEASE_ID = "20260817T085308Z-70f77913-da700a8e825e"


def _pointer(*, release_id: str = _RELEASE_ID, manifest_sha256: str = "f" * 64) -> L1ReleasePointer:
    return L1ReleasePointer(
        pointer_version=1,
        release_id=release_id,
        manifest_key=f"lake/manifests/releases/l1/{release_id}.json",
        manifest_sha256=manifest_sha256,
    )


class _UnusedStore:
    def download_file(self, key: str, path: Path, *, expect_bytes: int | None = None) -> None:
        raise AssertionError(f"the test must not download {key}")


class _DownloadStore:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.downloads: list[str] = []

    def download_file(self, key: str, path: Path, *, expect_bytes: int | None = None) -> None:
        self.downloads.append(key)
        path.write_bytes(self.payload)


def test_fetch_heals_a_corrupted_manifest_cache_after_digest_validation(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    key = "lake/manifests/releases/l1/release.json"
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
    key = "lake/manifests/releases/l1/release.json"
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
    key = "lake/manifests/releases/l1/release.json"
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


def test_the_flag_reaches_the_publication(monkeypatch: pytest.MonkeyPatch) -> None:
    """argparse が旗を持つことと、main がそれを渡すことは別の事実である。"""

    seen: dict[str, object] = {}

    def _capture(**kwargs: object) -> object:
        seen.update(kwargs)
        raise LakePublishError("stop before touching R2")

    monkeypatch.setattr(publish_module, "publish_market_lake", _capture)
    monkeypatch.setattr(publish_module, "Boto3R2Store", lambda **_: _UnusedStore())

    exit_code = publish_module.main(
        ["--sqlite", "market.sqlite", "--mirror", "stores", "--full-rebuild"]
    )

    assert exit_code == 1
    assert seen["full_rebuild"] is True


def test_a_fingerprint_refusal_names_the_full_rebuild_recovery(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _refuse(**_: object) -> object:
        raise LakeTransformFingerprintMismatch("transform_fingerprint differs")

    monkeypatch.setattr(publish_module, "publish_market_lake", _refuse)
    monkeypatch.setattr(publish_module, "Boto3R2Store", lambda **_: _UnusedStore())

    exit_code = publish_module.main(["--sqlite", "market.sqlite", "--mirror", "stores"])

    assert exit_code == 1
    printed = capsys.readouterr().err
    assert "transform_fingerprint differs" in printed
    assert "no retry clears this" in printed
    assert publish_module.RECOVERY_RUNBOOK_SECTION in printed


def test_an_unrelated_build_error_does_not_prescribe_a_full_rebuild(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _refuse(**_: object) -> object:
        raise LakeBuildError("invalid date in sealed SQLite snapshot")

    monkeypatch.setattr(publish_module, "publish_market_lake", _refuse)
    monkeypatch.setattr(publish_module, "Boto3R2Store", lambda **_: _UnusedStore())

    exit_code = publish_module.main(["--sqlite", "market.sqlite", "--mirror", "stores"])

    assert exit_code == 1
    printed = capsys.readouterr().err
    assert "invalid date" in printed
    assert "full rebuild" not in printed
    assert "no retry clears this" not in printed


def test_the_recovery_the_message_names_is_one_the_runbook_carries() -> None:
    runbook = (ROOT / "batch/OPERATIONS.md").read_text(encoding="utf-8")

    assert f"### {publish_module.RECOVERY_RUNBOOK_SECTION}" in runbook
    section = runbook.split(f"### {publish_module.RECOVERY_RUNBOOK_SECTION}", 1)[1].split(
        "\n### ", 1
    )[0]
    assert "--full-rebuild" in section
    assert "publish_market_lake" in section


def test_the_runbook_recovery_goes_through_the_path_that_records_the_release() -> None:
    runbook = (ROOT / "batch/OPERATIONS.md").read_text(encoding="utf-8")
    section = runbook.split(f"### {publish_module.RECOVERY_RUNBOOK_SECTION}", 1)[1].split(
        "\n### ", 1
    )[0]
    transfer = (ROOT / "batch/scripts/r2_transfer.sh").read_text(encoding="utf-8")
    branch = transfer.split('if [[ "${mode}" == "full-rebuild" ]]; then', 1)[1].split(
        "\n  fi\n", 1
    )[0]

    assert "batch/scripts/r2_transfer.sh publish-lake full-rebuild" in section
    assert "module を直接叩かない" in section
    assert "--full-rebuild" in branch
    assert "record_lake_release" in branch


def test_publisher_cli_has_no_sidecar_origin_arguments() -> None:
    parsed = publish_module.build_parser().parse_args(
        [
            "--sqlite",
            "market.sqlite",
            "--mirror",
            "stores",
            "--bucket",
            "baibai-stores",
            "--full-rebuild",
        ]
    )

    assert parsed.full_rebuild is True
    assert parsed.bucket == "baibai-stores"
    assert not hasattr(parsed, "origin_release")
