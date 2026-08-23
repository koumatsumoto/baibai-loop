"""The full-rebuild path and the floor that keeps it from publishing a shorter history.

A differential publication cannot lose rows: the partitions it does not rewrite are
carried by reference from the base release. A full rebuild has no such floor — it seals
exactly what the store holds — so it needs one supplied, and the base release's own
totals are it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import baibai_batch.storage.publish_market_lake as publish_module
from baibai_batch.storage.lake_publish import LakePublishError
from baibai_batch.storage.publish_market_lake import (
    _require_expected_release,
    _require_no_rows_lost,
    publish_market_lake,
)
from baibai_engine.batch_api import L1ReleasePointer, LakeBuildError

ROOT = Path(__file__).resolve().parents[2]
_RELEASE_ID = "20260817T085308Z-70f77913-da700a8e825e"
_MANIFEST_KEY = f"lake/manifests/releases/l1/{_RELEASE_ID}.json"


def _manifest(rows: int) -> dict[str, object]:
    return {
        "manifest_version": 1,
        "release_id": _RELEASE_ID,
        "profile": "production",
        "created_at": datetime(2026, 8, 17, tzinfo=UTC).isoformat().replace("+00:00", "Z"),
        "data_as_of": "2026-07-31",
        "datasets": {
            "jquants.daily_bars": {
                "build_id": "20260817T084715Z-legacy-7fdfcf3c-a8eb8f6747ce",
                "contract_version": 1,
                "coverage_status": "complete",
                "data_as_of": "2026-07-31",
                "manifest_sha256": "e" * 64,
                "totals": {"bytes": 1, "objects": 1, "rows": rows},
            }
        },
    }


def _mirror_with_release(tmp_path: Path, *, rows: int) -> Path:
    mirror = tmp_path / "mirror"
    path = mirror / _MANIFEST_KEY
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_manifest(rows)), encoding="utf-8")
    return mirror


def _pointer() -> L1ReleasePointer:
    return L1ReleasePointer(
        pointer_version=1,
        release_id=_RELEASE_ID,
        manifest_key=_MANIFEST_KEY,
        manifest_sha256="f" * 64,
    )


def _fixed_release(mirror: Path) -> SimpleNamespace:
    release = publish_module.load_lake_model_json(
        (mirror / _MANIFEST_KEY).read_bytes(), publish_module.LakeReleaseManifest
    )
    return SimpleNamespace(
        release_id=_RELEASE_ID,
        manifest=release,
    )


def _exported(rows: int) -> dict[str, object]:
    return {"jquants.daily_bars": SimpleNamespace(totals=SimpleNamespace(rows=rows))}


class _UnusedStore:
    """The manifest is already in the mirror, so nothing may be downloaded."""

    def download_file(self, key: str, path: Path, *, expect_bytes: int | None = None) -> None:
        raise AssertionError(f"the mirror already holds {key}")


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


def test_a_store_behind_the_release_cannot_be_published_as_a_full_rebuild(
    tmp_path: Path,
) -> None:
    mirror = _mirror_with_release(tmp_path, rows=10)

    with pytest.raises(LakePublishError, match="would drop the difference"):
        _require_no_rows_lost(_exported(9), _fixed_release(mirror))  # type: ignore[arg-type]


def test_a_store_that_matches_the_release_is_allowed(
    tmp_path: Path,
) -> None:
    mirror = _mirror_with_release(tmp_path, rows=10)

    _require_no_rows_lost(_exported(10), _fixed_release(mirror))  # type: ignore[arg-type]


def test_a_store_ahead_of_the_release_is_allowed(
    tmp_path: Path,
) -> None:
    """The ordinary shape after a local fetch — the rebuild is what publishes them."""

    mirror = _mirror_with_release(tmp_path, rows=10)

    _require_expected_release(_pointer(), _RELEASE_ID, "f" * 64, role="origin")
    _require_no_rows_lost(_exported(12), _fixed_release(mirror))  # type: ignore[arg-type]


def test_a_new_dataset_does_not_change_the_replaced_release_floor(tmp_path: Path) -> None:
    mirror = _mirror_with_release(tmp_path, rows=10)
    exported = {
        **_exported(10),
        "new.dataset": SimpleNamespace(totals=SimpleNamespace(rows=1)),
    }

    _require_no_rows_lost(exported, _fixed_release(mirror))  # type: ignore[arg-type]


def test_a_dataset_that_disappears_from_the_sealed_export_is_refused(tmp_path: Path) -> None:
    mirror = _mirror_with_release(tmp_path, rows=10)

    with pytest.raises(LakePublishError, match="holds 0 row"):
        _require_no_rows_lost({}, _fixed_release(mirror))  # type: ignore[arg-type]


def test_the_first_publication_has_no_floor_to_check() -> None:
    _require_no_rows_lost({}, None)


def test_full_rebuild_requires_the_recorded_store_origin() -> None:
    with pytest.raises(LakePublishError, match="name it as the store origin"):
        _require_expected_release(_pointer(), None, None, role="origin")


def test_full_rebuild_rejects_same_release_id_with_a_different_digest() -> None:
    with pytest.raises(LakePublishError, match="hydrate again before publishing"):
        _require_expected_release(_pointer(), _RELEASE_ID, "0" * 64, role="origin")


def test_same_count_store_from_an_older_release_is_refused() -> None:
    with pytest.raises(LakePublishError, match="hydrate again before publishing"):
        _require_expected_release(_pointer(), "older-release", "f" * 64, role="origin")


def test_first_publication_accepts_no_origin() -> None:
    _require_expected_release(None, None, None, role="origin")


def test_sealed_export_floor_runs_before_release_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror = _mirror_with_release(tmp_path, rows=10)
    fixed = _fixed_release(mirror)
    serving = SimpleNamespace(pointer=_pointer(), precondition=object())
    exported = SimpleNamespace(
        datasets={
            "jquants.daily_bars": SimpleNamespace(
                manifest=SimpleNamespace(totals=SimpleNamespace(rows=9))
            )
        }
    )
    created = False

    def _create(**_: object) -> object:
        nonlocal created
        created = True
        raise AssertionError("release creation must follow the sealed floor")

    monkeypatch.setattr(publish_module, "_serving_pointer", lambda _: serving)
    monkeypatch.setattr(publish_module, "_resolve_base_release", lambda *_: fixed)
    monkeypatch.setattr(publish_module, "export_lake_legacy", lambda **_: exported)
    monkeypatch.setattr(publish_module, "lake_verified_git_commit", lambda: "a" * 40)
    monkeypatch.setattr(publish_module, "create_lake_l1_release", _create)

    with pytest.raises(LakePublishError, match="sealed export"):
        publish_market_lake(
            sqlite_path=tmp_path / "market.sqlite",
            mirror_root=mirror,
            store=_UnusedStore(),
            expected_base_release_id=None,
            expected_base_manifest_sha256=None,
            origin_release_id=_RELEASE_ID,
            origin_manifest_sha256="f" * 64,
            full_rebuild=True,
        )

    assert created is False


def test_a_full_rebuild_and_an_expected_base_are_mutually_exclusive(tmp_path: Path) -> None:
    """Naming a base a rebuild will not read would assert a check that never runs."""

    with pytest.raises(LakePublishError, match="no base release to expect"):
        publish_market_lake(
            sqlite_path=tmp_path / "market.sqlite",
            mirror_root=tmp_path / "mirror",
            store=_UnusedStore(),
            expected_base_release_id=_RELEASE_ID,
            expected_base_manifest_sha256="a" * 64,
            full_rebuild=True,
        )


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


def test_a_fingerprint_refusal_names_a_recovery_instead_of_ending_in_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The shape that stopped 2026-08-19's batch: the export refuses a base built under
    the previous transform fingerprint, and the run log ended in an uncaught traceback
    naming no way out. The guard is right; what was missing was where to go next."""

    def _refuse(**_: object) -> object:
        raise LakeBuildError(
            "base manifest transform_fingerprint differs; run a full rebuild without "
            "--base-manifest"
        )

    monkeypatch.setattr(publish_module, "publish_market_lake", _refuse)
    monkeypatch.setattr(publish_module, "Boto3R2Store", lambda **_: _UnusedStore())

    exit_code = publish_module.main(["--sqlite", "market.sqlite", "--mirror", "stores"])

    assert exit_code == 1
    printed = capsys.readouterr().err
    assert "transform_fingerprint differs" in printed
    assert "no retry clears this" in printed
    assert publish_module.RECOVERY_RUNBOOK_SECTION in printed


def test_the_recovery_the_message_names_is_one_the_runbook_carries() -> None:
    """A pointer to a section nobody wrote sends the operator nowhere. Bind the two, so
    renaming the section fails here rather than at 17:00 on the day it is needed."""

    runbook = (ROOT / "batch/OPERATIONS.md").read_text(encoding="utf-8")

    assert f"### {publish_module.RECOVERY_RUNBOOK_SECTION}" in runbook
    section = runbook.split(f"### {publish_module.RECOVERY_RUNBOOK_SECTION}", 1)[1].split("\n### ")[
        0
    ]
    assert "--full-rebuild" in section
    assert "publish_market_lake" in section


def test_the_runbook_recovery_goes_through_the_path_that_records_the_release() -> None:
    """The publisher creates a release; the transfer script records which one the store
    now corresponds to. A recovery published around that record leaves the store naming
    a release the lake has moved past, and the next `push-market` refuses to dehydrate
    against it — which is what happened on 2026-08-19 when the recovery ran as a direct
    module call. Bind the runbook's command to the branch that writes the record."""

    runbook = (ROOT / "batch/OPERATIONS.md").read_text(encoding="utf-8")
    section = runbook.split(f"### {publish_module.RECOVERY_RUNBOOK_SECTION}", 1)[1].split("\n### ")[
        0
    ]
    transfer = (ROOT / "batch/scripts/r2_transfer.sh").read_text(encoding="utf-8")
    branch = transfer.split('if [[ "${mode}" == "full-rebuild" ]]; then', 1)[1].split(
        "\n  fi\n", 1
    )[0]

    assert "batch/scripts/r2_transfer.sh publish-lake full-rebuild" in section
    assert "module を直接叩かない" in section
    assert "--full-rebuild" in branch
    assert "record_lake_release" in branch


def test_the_publisher_still_accepts_the_flag_the_script_passes() -> None:
    """The script names a flag; argparse is what decides whether it runs."""

    parsed = publish_module.build_parser().parse_args(
        [
            "--sqlite",
            "market.sqlite",
            "--mirror",
            "stores",
            "--bucket",
            "baibai-stores",
            "--origin-release",
            _RELEASE_ID,
            "--origin-manifest-sha256",
            "f" * 64,
            "--full-rebuild",
        ]
    )

    assert parsed.full_rebuild is True
    assert parsed.bucket == "baibai-stores"
    assert parsed.origin_release == _RELEASE_ID
