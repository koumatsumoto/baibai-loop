"""The public DB diagnostic describes one read-only database snapshot."""

import sqlite3
from contextlib import closing
from pathlib import Path

import yaml

from baibai_engine.cli import database
from baibai_engine.cli.app import main
from baibai_engine.storage.sqlite.read import connect_read_only


def test_info_counts_quoted_tables_and_version_in_one_snapshot(tmp_path, capsys, monkeypatch):
    db = tmp_path / "snapshot.sqlite"
    with closing(sqlite3.connect(db)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.executescript(
            """
            CREATE TABLE a_balance (id INTEGER);
            CREATE TABLE "z""evidence" (id INTEGER);
            INSERT INTO a_balance VALUES (1);
            INSERT INTO "z""evidence" VALUES (1);
            PRAGMA user_version=17;
            """
        )
        published = []

        def reader(path: Path) -> sqlite3.Connection:
            connection = connect_read_only(path)
            assert connection.execute("PRAGMA query_only").fetchone()[0] == 1

            def publish_between_counts(sql: str) -> None:
                if sql == 'SELECT count(*) FROM "z""evidence"' and not published:
                    writer.executescript(
                        """
                        BEGIN;
                        INSERT INTO a_balance VALUES (2);
                        DELETE FROM "z""evidence";
                        PRAGMA user_version=18;
                        COMMIT;
                        """
                    )
                    published.append(True)

            connection.set_trace_callback(publish_between_counts)
            return connection

        monkeypatch.setattr(database, "connect_read_only", reader)
        assert main(["db", "info", "--db", str(db)]) == 0
        result = capsys.readouterr()
        assert result.err == ""
        assert published == [True]
        assert yaml.safe_load(result.out) == {
            "path": str(db),
            "exists": True,
            "tables": {"a_balance": 1, 'z"evidence': 1},
            "user_version": 17,
        }

        assert main(["db", "info", "--db", str(db)]) == 0
        assert yaml.safe_load(capsys.readouterr().out) == {
            "path": str(db),
            "exists": True,
            "tables": {"a_balance": 2, 'z"evidence': 0},
            "user_version": 18,
        }


def test_info_distinguishes_an_absent_database_from_an_unreadable_one(tmp_path, capsys):
    path = tmp_path / "info.sqlite"
    assert main(["db", "info", "--db", str(path)]) == 0
    assert yaml.safe_load(capsys.readouterr().out) == {"path": str(path), "exists": False}
    assert not path.exists()

    before = b"no SQLite database"
    path.write_bytes(before)
    assert main(["db", "info", "--db", str(path)]) == 1
    result = capsys.readouterr()
    assert result.out == ""
    assert "file is not a database" in result.err
    assert path.read_bytes() == before
