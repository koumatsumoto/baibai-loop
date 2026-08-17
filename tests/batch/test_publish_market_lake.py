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

import pytest

from baibai_batch.storage.lake_publish import LakePublishError
from baibai_batch.storage.publish_market_lake import (
    _require_no_rows_lost,
    build_parser,
    publish_market_lake,
)
from baibai_engine.batch_api import L1ReleasePointer

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


class _UnusedStore:
    """The manifest is already in the mirror, so nothing may be downloaded."""

    def download_file(self, key: str, path: Path, *, expect_bytes: int | None = None) -> None:
        raise AssertionError(f"the mirror already holds {key}")


def test_a_store_behind_the_release_cannot_be_published_as_a_full_rebuild(
    tmp_path: Path,
) -> None:
    mirror = _mirror_with_release(tmp_path, rows=10)
    store = _store_with_bars(tmp_path, rows=9)

    with pytest.raises(LakePublishError, match="would drop the difference"):
        _require_no_rows_lost(store, _UnusedStore(), mirror, _pointer())


def test_a_store_that_matches_the_release_is_allowed(tmp_path: Path) -> None:
    mirror = _mirror_with_release(tmp_path, rows=10)
    store = _store_with_bars(tmp_path, rows=10)

    _require_no_rows_lost(store, _UnusedStore(), mirror, _pointer())


def test_a_store_ahead_of_the_release_is_allowed(tmp_path: Path) -> None:
    """The ordinary shape after a local fetch — the rebuild is what publishes them."""

    mirror = _mirror_with_release(tmp_path, rows=10)
    store = _store_with_bars(tmp_path, rows=12)

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


def test_the_flag_reaches_the_publication() -> None:
    args = build_parser().parse_args(
        ["--sqlite", "market.sqlite", "--mirror", "stores", "--full-rebuild"]
    )

    assert args.full_rebuild is True
    assert args.base_release is None
