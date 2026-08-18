from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pytest

from baibai_engine.appdb.migrations import LATEST_VERSION, MIGRATIONS, Migration
from baibai_engine.appdb.write import (
    backup_database,
    connect_rw,
    initialize_database,
    prune_backups,
)
from baibai_engine.foundation.time import JST


def test_init_is_idempotent_and_enables_foreign_keys(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"

    assert initialize_database(path) == LATEST_VERSION
    assert initialize_database(path) == LATEST_VERSION

    connection = connect_rw(path)
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION
    finally:
        connection.close()


def test_failed_migration_rolls_back_schema_data_and_version(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    migrations = (
        Migration(1, ("CREATE TABLE parent (id INTEGER PRIMARY KEY) STRICT",)),
        Migration(
            2,
            (
                "CREATE TABLE transient (id INTEGER PRIMARY KEY) STRICT",
                "INSERT INTO missing_table VALUES (1)",
            ),
        ),
    )

    with pytest.raises(sqlite3.OperationalError):
        initialize_database(path, migrations=migrations)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_schema WHERE type='table' AND name='transient'"
            ).fetchone()[0]
            == 0
        )


def test_backup_includes_uncheckpointed_wal_rows(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    initialize_database(path)
    writer = connect_rw(path)
    try:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("INSERT INTO app_meta (key, value) VALUES ('k', 'v')")
        target = backup_database(
            path,
            backup_dir=tmp_path / "backups",
            now=datetime(2026, 7, 19, 12, 0, tzinfo=JST),
        )
    finally:
        writer.close()

    assert target is not None
    with sqlite3.connect(target) as backup:
        assert backup.execute("SELECT value FROM app_meta WHERE key='k'").fetchone()[0] == "v"
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("PRAGMA foreign_key_check").fetchall() == []


def test_backup_reports_absent_database(tmp_path: Path) -> None:
    assert backup_database(tmp_path / "missing.sqlite") is None


_SEED = (
    Migration(1, ("CREATE TABLE ledger (id INTEGER PRIMARY KEY, ticker TEXT) STRICT",)),
    Migration(2, ("INSERT INTO ledger (id, ticker) VALUES (1, '8255'), (2, '9692')",)),
)


def _seeded(path: Path) -> None:
    assert initialize_database(path, migrations=_SEED) == 2


def test_a_pending_migration_takes_a_checkpoint_before_its_first_statement(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.sqlite"
    _seeded(path)
    backups = path.parent / "backups"
    assert not backups.exists()

    initialize_database(
        path, migrations=(*_SEED, Migration(3, ("ALTER TABLE ledger ADD lots INTEGER",)))
    )

    generations = sorted(backups.glob("baibai-*.sqlite"))
    assert len(generations) == 1
    with closing(sqlite3.connect(generations[0])) as checkpoint:
        # Taken before the migration, so it carries the schema the migration changed.
        assert checkpoint.execute("PRAGMA user_version").fetchone()[0] == 2
        assert "lots" not in {
            row[1] for row in checkpoint.execute("PRAGMA table_info(ledger)").fetchall()
        }


def test_a_store_with_nothing_pending_takes_no_checkpoint(tmp_path: Path) -> None:
    """Routine writers call this on every command; a copy per command would be a copy
    of a store nothing is about to change."""
    path = tmp_path / "app.sqlite"
    _seeded(path)

    initialize_database(path, migrations=_SEED)

    assert not (path.parent / "backups").exists()


def test_a_new_store_takes_no_checkpoint(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"

    _seeded(path)

    assert not (path.parent / "backups").exists()


def test_a_checkpoint_that_cannot_be_written_leaves_the_migration_unapplied(
    tmp_path: Path,
) -> None:
    """Fail-close: applying it anyway is applying it with no way back."""
    path = tmp_path / "app.sqlite"
    _seeded(path)
    (path.parent / "backups").write_text("not a directory", encoding="utf-8")

    with pytest.raises(FileExistsError):
        initialize_database(
            path, migrations=(*_SEED, Migration(3, ("ALTER TABLE ledger ADD lots INTEGER",)))
        )

    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert "lots" not in {
            row[1] for row in connection.execute("PRAGMA table_info(ledger)").fetchall()
        }


def test_the_checkpoint_gives_back_the_rows_a_committed_migration_removed(
    tmp_path: Path,
) -> None:
    """The transaction only undoes statements that failed. A migration that runs exactly
    as written and means the wrong thing commits, and this is the copy it is undone from.
    """
    path = tmp_path / "app.sqlite"
    _seeded(path)

    assert (
        initialize_database(
            path, migrations=(*_SEED, Migration(3, ("DELETE FROM ledger WHERE ticker = '9692'",)))
        )
        == 3
    )
    with closing(sqlite3.connect(path)) as damaged:
        assert damaged.execute("SELECT count(*) FROM ledger").fetchone()[0] == 1

    checkpoint = sorted((path.parent / "backups").glob("baibai-*.sqlite"))[-1]
    shutil.copy(checkpoint, path)

    with closing(sqlite3.connect(path)) as restored:
        assert restored.execute("PRAGMA user_version").fetchone()[0] == 2
        assert [row[0] for row in restored.execute("SELECT ticker FROM ledger ORDER BY id")] == [
            "8255",
            "9692",
        ]


def test_pruning_removes_the_oldest_generation_only(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _seeded(path)
    backups = tmp_path / "backups"
    stamps = [datetime(2026, 8, day, 12, 0, tzinfo=JST) for day in (1, 2, 3, 4)]

    written = [backup_database(path, backup_dir=backups, now=stamp, keep=3) for stamp in stamps]

    assert all(target is not None for target in written)
    oldest, *kept = written
    assert oldest is not None
    remaining = sorted(item.name for item in backups.glob("baibai-*.sqlite"))
    assert remaining == sorted(target.name for target in kept if target is not None)
    assert not oldest.exists()


def test_pruning_never_empties_the_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="keep must be at least 1"):
        prune_backups(tmp_path, keep=0)


def test_migration_v10_rewrites_legacy_vocabulary_rows(tmp_path: Path) -> None:
    """v9 の実データ形 (旧 table/column/payload key) が v10 で無損失に改名される。"""
    path = tmp_path / "app.sqlite"

    assert initialize_database(path, migrations=MIGRATIONS[:9]) == 9
    connection = connect_rw(path)
    try:
        connection.execute(
            "INSERT INTO reviewed_shortlist VALUES (?, ?, ?, ?, ?, ?)",
            (
                "shortlist-1",
                "selection-1",
                "run-1",
                "2026-07-01",
                "2026-07-01T10:00:00+09:00",
                '{"kind": "reviewed-shortlist", "entries": []}',
            ),
        )
        connection.execute(
            "INSERT INTO research_packet VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "packet-20260701-9999",
                "9999",
                "2026-07-01",
                "buy",
                "2026-07-01T11:00:00+09:00",
                None,
                "{}",
            ),
        )
        connection.execute(
            "INSERT INTO research_review VALUES (?, ?, ?, ?)",
            (
                "review-1",
                "packet-20260701-9999",
                "2026-07-01T12:00:00+09:00",
                '{"reviewed_packet_sha256": "' + "a" * 64 + '"}',
            ),
        )
        connection.execute(
            "INSERT INTO holding_review VALUES (?, ?, ?, ?, ?, ?)",
            (
                "holding-review-1",
                "9999",
                "2026-07-02",
                "packet-20260701-9999",
                None,
                (
                    '{"sources": {"ledger": {"entity_id": "portfolio-ledger"},'
                    ' "holding_packet": {"entity_id": "packet-20260701-9999"},'
                    ' "candidate_packet": null}}'
                ),
            ),
        )
        connection.execute(
            "INSERT INTO proposal VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "proposal-1",
                "9999",
                "packet-20260701-9999",
                "review-1",
                "2026-07-02T09:00:00+09:00",
                "pending",
                None,
                "{}",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    assert initialize_database(path) == LATEST_VERSION

    connection = connect_rw(path)
    try:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"thesis", "thesis_review", "shortlist"} <= tables
        assert not {"research_packet", "research_review", "reviewed_shortlist"} & tables

        assert connection.execute("SELECT thesis_id FROM thesis").fetchone()[0] == (
            "packet-20260701-9999"
        )
        review_payload = connection.execute("SELECT payload FROM thesis_review").fetchone()[0]
        assert '"reviewed_thesis_sha256"' in review_payload
        assert "reviewed_packet_sha256" not in review_payload

        holding = connection.execute(
            "SELECT thesis_id, candidate_thesis_id, payload FROM holding_review"
        ).fetchone()
        assert holding[0] == "packet-20260701-9999"
        assert holding[1] is None
        assert tuple(
            connection.execute(
                "SELECT json_extract(payload, '$.sources.holding_thesis.entity_id'),"
                " json_type(payload, '$.sources.candidate_thesis'),"
                " json_type(payload, '$.sources.holding_packet'),"
                " json_type(payload, '$.sources.candidate_packet')"
                " FROM holding_review"
            ).fetchone()
        ) == ("packet-20260701-9999", "null", None, None)

        assert tuple(
            connection.execute("SELECT json_extract(payload, '$.kind') FROM shortlist").fetchone()
        ) == ("shortlist",)
        assert tuple(connection.execute("SELECT thesis_id FROM proposal").fetchone()) == (
            "packet-20260701-9999",
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_migration_v15_moves_the_retired_handoff_into_result(tmp_path: Path) -> None:
    """model から外れた field を持つ行が、読み手を落とさない形へ移る。

    `OperationPayload` は `extra="forbid"` なので、`handoff` を持つ行が 1 つでもあると
    `operation show` が全件で落ちる。読み取り側を寛容にすると model から field を外す
    たびに分岐が積もるので、保存済みの行を 1 度だけ移す。
    """
    path = tmp_path / "app.sqlite"
    assert initialize_database(path, migrations=MIGRATIONS[:14]) == 14
    connection = connect_rw(path)
    try:
        rows = (
            # 内容を持ち result が空の行: 判断内容を result へ畳む。
            (
                "op-20260729-opportunity-1",
                (
                    '{"checkpoint": "done", "artifacts": [], "canonical_refs": [],'
                    ' "human_confirmation": null, "completion_reason": null,'
                    ' "result": null, "next": null,'
                    ' "handoff": {"order_proposal": "none", "reason": "割高"}}'
                ),
            ),
            # 内容を持つが result が既に埋まっている行: 記録済みの結論を上書きしない。
            (
                "op-20260728-opportunity-1",
                (
                    '{"checkpoint": "done", "artifacts": [], "canonical_refs": [],'
                    ' "human_confirmation": null, "completion_reason": null,'
                    ' "result": "no actionable bargain", "next": null,'
                    ' "handoff": {"order_proposal": "none", "reason": "後で"}}'
                ),
            ),
            # 値の無い行: key を落とすだけ。
            (
                "op-20260717-opportunity-1",
                (
                    '{"checkpoint": "done", "artifacts": [], "canonical_refs": [],'
                    ' "human_confirmation": null, "completion_reason": null,'
                    ' "result": null, "next": null, "handoff": null}'
                ),
            ),
        )
        for operation_id, payload in rows:
            connection.execute(
                "INSERT INTO operation_session VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    operation_id,
                    "opportunity",
                    "completed",
                    "2026-07-29",
                    None,
                    "2026-07-29T09:00:00+09:00",
                    "2026-07-29T18:00:00+09:00",
                    payload,
                ),
            )
    finally:
        connection.close()

    assert initialize_database(path) == LATEST_VERSION

    connection = connect_rw(path)
    try:
        remaining = connection.execute(
            "SELECT COUNT(*) FROM operation_session"
            " WHERE json_type(payload, '$.handoff') IS NOT NULL"
        ).fetchone()[0]
        assert remaining == 0
        results = dict(
            connection.execute(
                "SELECT operation_id, json_extract(payload, '$.result') FROM operation_session"
            ).fetchall()
        )
        assert results["op-20260729-opportunity-1"] == "none: 割高"
        # 記録済みの結論はそのまま。
        assert results["op-20260728-opportunity-1"] == "no actionable bargain"
        # 値の無かった行は result を作らない。
        assert results["op-20260717-opportunity-1"] is None
        # 完了 session の不変性を守る trigger は復元されている。
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                " AND tbl_name = 'operation_session'"
            ).fetchall()
        }
        assert "operation_session_completed_no_update" in triggers
        assert "operation_session_completed_no_delete" in triggers
    finally:
        connection.close()
