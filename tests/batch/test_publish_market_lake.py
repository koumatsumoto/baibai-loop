"""The full-rebuild path and the floor that keeps it from publishing a shorter history.

A differential publication cannot lose rows: the partitions it does not rewrite are
carried by reference from the base release. A full rebuild has no such floor — it seals
exactly what the store holds — so it needs one supplied, and the base release's own
totals are it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import baibai_batch.storage.publish_market_lake as publish_module
from baibai_batch.storage.lake_publish import LakePublishError
from baibai_batch.storage.publish_market_lake import (
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


def _store_with_bars(tmp_path: Path, *, rows: int) -> Path:
    store = tmp_path / "market.sqlite"
    with sqlite3.connect(store) as connection:
        connection.execute("CREATE TABLE jquants_daily_bars (ticker TEXT, traded_at TEXT)")
        connection.executemany(
            "INSERT INTO jquants_daily_bars VALUES (?, ?)",
            [(str(index), "2026-07-31") for index in range(rows)],
        )
    return store


def _pointer() -> L1ReleasePointer:
    return L1ReleasePointer(
        pointer_version=1,
        release_id=_RELEASE_ID,
        manifest_key=_MANIFEST_KEY,
        manifest_sha256="f" * 64,
    )


def _use_fixture_release(monkeypatch: pytest.MonkeyPatch, mirror: Path) -> None:
    release = publish_module.load_lake_model_json(
        (mirror / _MANIFEST_KEY).read_bytes(), publish_module.LakeReleaseManifest
    )
    monkeypatch.setattr(
        publish_module,
        "_resolve_base_release",
        lambda *_: SimpleNamespace(manifest=release),
    )


class _UnusedStore:
    """The manifest is already in the mirror, so nothing may be downloaded."""

    def download_file(self, key: str, path: Path, *, expect_bytes: int | None = None) -> None:
        raise AssertionError(f"the mirror already holds {key}")


def test_a_store_behind_the_release_cannot_be_published_as_a_full_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror = _mirror_with_release(tmp_path, rows=10)
    store = _store_with_bars(tmp_path, rows=9)
    _use_fixture_release(monkeypatch, mirror)

    with pytest.raises(LakePublishError, match="would drop the difference"):
        _require_no_rows_lost(store, _UnusedStore(), mirror, _pointer())


def test_a_store_that_matches_the_release_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror = _mirror_with_release(tmp_path, rows=10)
    store = _store_with_bars(tmp_path, rows=10)
    _use_fixture_release(monkeypatch, mirror)

    _require_no_rows_lost(store, _UnusedStore(), mirror, _pointer())


def test_a_store_ahead_of_the_release_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary shape after a local fetch — the rebuild is what publishes them."""

    mirror = _mirror_with_release(tmp_path, rows=10)
    store = _store_with_bars(tmp_path, rows=12)
    _use_fixture_release(monkeypatch, mirror)

    _require_no_rows_lost(store, _UnusedStore(), mirror, _pointer())


def test_the_first_publication_has_no_floor_to_check(tmp_path: Path) -> None:
    store = _store_with_bars(tmp_path, rows=0)

    _require_no_rows_lost(store, _UnusedStore(), tmp_path / "mirror", None)


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
            "--full-rebuild",
        ]
    )

    assert parsed.full_rebuild is True
    assert parsed.bucket == "baibai-stores"
