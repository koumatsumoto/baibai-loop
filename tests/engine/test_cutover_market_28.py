import sqlite3
from contextlib import closing

import pytest
from tools.cutover_market_28 import cutover

from baibai_engine.market.sqlite import open_connection
from baibai_engine.market.tradingview.contract import TABLE


def predecessor(path):
    with closing(open_connection(path)) as conn, conn:
        conn.execute(f"DROP TABLE {TABLE}")
        conn.execute("PRAGMA user_version=27")
        conn.execute("INSERT INTO jquants_market_calendar VALUES ('2026-09-24',1)")
    return path


def test_cutover_preserves_source_and_rows(tmp_path):
    source = predecessor(tmp_path / "old.sqlite")
    target = tmp_path / "new.sqlite"
    before = source.read_bytes()
    cutover(source, target)
    assert source.read_bytes() == before
    with closing(open_connection(target)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 28
        assert conn.execute("SELECT * FROM jquants_market_calendar").fetchall() == [
            ("2026-09-24", 1)
        ]
        assert conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] == 0
    with pytest.raises(FileExistsError):
        cutover(source, target)


@pytest.mark.parametrize("version", [26, 28])
def test_cutover_rejects_other_versions(tmp_path, version):
    source = predecessor(tmp_path / "old.sqlite")
    with closing(sqlite3.connect(source)) as conn:
        conn.execute(f"PRAGMA user_version={version}")
    target = tmp_path / "new.sqlite"
    with pytest.raises(ValueError, match="requires schema 27"):
        cutover(source, target)
    assert not target.exists()


def test_cutover_rejects_incompatible_predecessor(tmp_path):
    source = predecessor(tmp_path / "old.sqlite")
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("DROP TABLE jquants_market_calendar")
    target = tmp_path / "new.sqlite"
    with pytest.raises(RuntimeError, match="incomplete"):
        cutover(source, target)
    assert not target.exists()
