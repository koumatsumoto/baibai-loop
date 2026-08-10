from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import ANY

import pytest

from baibai_batch.storage import store_layout_migration
from baibai_batch.storage.store_layout_migration import (
    StoreLayoutMigrationError,
    migrate,
)
from baibai_engine.batch_api import STORE_LAYOUT_MAPPINGS


def _create_store(path: Path, kind: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"PRAGMA user_version = {store_layout_migration._EXPECTED_SCHEMA_VERSIONS[kind]}"
        )
        for table in sorted(store_layout_migration._REQUIRED_TABLES[kind]):
            if table == "ledger_event":
                connection.execute("CREATE TABLE ledger_event (append_seq INTEGER PRIMARY KEY)")
                connection.execute("INSERT INTO ledger_event VALUES (7)")
            else:
                connection.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY)')


def _seed_legacy_layout(root: Path) -> None:
    kinds = ("application", "market", "macro", "runs", "calibration")
    for kind, (legacy, _current) in zip(kinds, STORE_LAYOUT_MAPPINGS, strict=True):
        path = root / legacy
        if kind == "calibration":
            path.mkdir(parents=True)
            (path / "panel.csv").write_text("ticker,value\n0001,1\n", encoding="utf-8")
        else:
            _create_store(path, kind)


def test_forward_dry_run_is_read_only_and_reports_pending(tmp_path: Path) -> None:
    _seed_legacy_layout(tmp_path)

    result = migrate(tmp_path, "forward")

    assert result.applied is False
    assert next(item for item in result.verification if item["kind"] == "application") == {
        "kind": "application",
        "schema_version": 14,
        "size_bytes": (tmp_path / STORE_LAYOUT_MAPPINGS[0][0]).stat().st_size,
        "table_count": len(store_layout_migration._REQUIRED_TABLES["application"]),
        "application_row_counts": {
            **{
                table: 0
                for table in store_layout_migration._REQUIRED_TABLES["application"]
                if table != "ledger_event"
            },
            "ledger_event": 1,
        },
        "ledger_append_head": 7,
        "application_dump_sha256": ANY,
    }
    assert dict(result.resources) == {
        "application": "pending",
        "market": "pending",
        "macro": "pending",
        "runs": "pending",
        "calibration": "pending",
    }
    for legacy, current in STORE_LAYOUT_MAPPINGS:
        assert (tmp_path / legacy).exists()
        assert not (tmp_path / current).exists()
    assert not list(tmp_path.rglob("baibai-layout-*.sqlite"))


def test_forward_and_rollback_keep_one_physical_store_and_verified_backup(
    tmp_path: Path,
) -> None:
    _seed_legacy_layout(tmp_path)
    original_inode = (tmp_path / STORE_LAYOUT_MAPPINGS[0][0]).stat().st_ino

    forward = migrate(tmp_path, "forward", apply=True)

    assert forward.applied is True
    assert forward.backup is not None
    assert (tmp_path / forward.backup).is_file()
    assert (tmp_path / STORE_LAYOUT_MAPPINGS[0][1]).stat().st_ino == original_inode
    for legacy, current in STORE_LAYOUT_MAPPINGS:
        assert not (tmp_path / legacy).exists()
        assert (tmp_path / current).exists()

    rollback = migrate(tmp_path, "rollback", apply=True)

    assert rollback.applied is True
    assert rollback.backup is not None
    assert (tmp_path / STORE_LAYOUT_MAPPINGS[0][0]).stat().st_ino == original_inode
    for legacy, current in STORE_LAYOUT_MAPPINGS:
        assert (tmp_path / legacy).exists()
        assert not (tmp_path / current).exists()


def test_both_layouts_are_rejected_without_creating_a_backup(tmp_path: Path) -> None:
    _seed_legacy_layout(tmp_path)
    _create_store(tmp_path / STORE_LAYOUT_MAPPINGS[0][1], "application")

    with pytest.raises(StoreLayoutMigrationError, match=r"both .* exist"):
        migrate(tmp_path, "forward", apply=True)

    assert not list(tmp_path.rglob("baibai-layout-*.sqlite"))
    assert (tmp_path / STORE_LAYOUT_MAPPINGS[0][0]).exists()
    assert (tmp_path / STORE_LAYOUT_MAPPINGS[0][1]).exists()


def test_missing_required_table_stops_before_any_move(tmp_path: Path) -> None:
    _seed_legacy_layout(tmp_path)
    app = tmp_path / STORE_LAYOUT_MAPPINGS[0][0]
    with sqlite3.connect(app) as connection:
        connection.execute("DROP TABLE thesis")

    with pytest.raises(StoreLayoutMigrationError, match="missing required tables: thesis"):
        migrate(tmp_path, "forward", apply=True)

    for legacy, current in STORE_LAYOUT_MAPPINGS:
        assert (tmp_path / legacy).exists()
        assert not (tmp_path / current).exists()


@pytest.mark.parametrize("schema_version", [1, 999])
def test_incompatible_schema_stops_before_any_move(tmp_path: Path, schema_version: int) -> None:
    _seed_legacy_layout(tmp_path)
    market = tmp_path / STORE_LAYOUT_MAPPINGS[1][0]
    with sqlite3.connect(market) as connection:
        connection.execute(f"PRAGMA user_version = {schema_version}")

    with pytest.raises(
        StoreLayoutMigrationError,
        match=rf"schema version is {schema_version}; current market code requires 20",
    ):
        migrate(tmp_path, "forward", apply=True)

    for legacy, current in STORE_LAYOUT_MAPPINGS:
        assert (tmp_path / legacy).exists()
        assert not (tmp_path / current).exists()


def test_sqlite_sidecar_stops_before_any_move(tmp_path: Path) -> None:
    _seed_legacy_layout(tmp_path)
    Path(f"{tmp_path / STORE_LAYOUT_MAPPINGS[0][0]}-wal").touch()

    with pytest.raises(StoreLayoutMigrationError, match="SQLite sidecar exists"):
        migrate(tmp_path, "forward", apply=True)

    assert (tmp_path / STORE_LAYOUT_MAPPINGS[0][0]).exists()
    assert not (tmp_path / STORE_LAYOUT_MAPPINGS[0][1]).exists()


@pytest.mark.parametrize("side", ["source", "destination"])
def test_orphan_sidecar_on_either_layout_stops_a_complete_dry_run(
    tmp_path: Path, side: str
) -> None:
    _seed_legacy_layout(tmp_path)
    migrate(tmp_path, "forward", apply=True)
    legacy, current = STORE_LAYOUT_MAPPINGS[0]
    primary = tmp_path / (legacy if side == "source" else current)
    primary.parent.mkdir(parents=True, exist_ok=True)
    Path(f"{primary}-wal").touch()

    with pytest.raises(StoreLayoutMigrationError, match="SQLite sidecar exists"):
        migrate(tmp_path, "forward")


def test_a_mid_move_failure_restores_every_moved_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_legacy_layout(tmp_path)
    real_move = store_layout_migration._atomic_move
    calls = 0

    def fail_second_move(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected move failure")
        real_move(source, destination)

    monkeypatch.setattr(store_layout_migration, "_atomic_move", fail_second_move)

    with pytest.raises(StoreLayoutMigrationError, match="failed and was rolled back"):
        migrate(tmp_path, "forward", apply=True)

    for legacy, current in STORE_LAYOUT_MAPPINGS:
        assert (tmp_path / legacy).exists()
        assert not (tmp_path / current).exists()
    assert len(list(tmp_path.rglob("baibai-layout-*.sqlite"))) == 1


def test_atomic_move_never_overwrites_a_destination_that_appears_after_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_text("canonical", encoding="utf-8")
    destination.write_text("concurrent", encoding="utf-8")
    monkeypatch.setattr(store_layout_migration, "_lexists", lambda _path: False)

    with pytest.raises(StoreLayoutMigrationError, match="destination appeared"):
        store_layout_migration._atomic_move(source, destination)

    assert source.read_text(encoding="utf-8") == "canonical"
    assert destination.read_text(encoding="utf-8") == "concurrent"
