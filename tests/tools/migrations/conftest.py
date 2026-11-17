"""Closed additive-migration source fixture."""

from contextlib import closing

import pytest
from tools.migrations.market_schema_52 import DETAILS

from baibai_engine.storage.sqlite.source_cache.schema import open_connection


@pytest.fixture
def schema51(tmp_path):
    source = tmp_path / "source.sqlite"
    with closing(open_connection(source)) as c, c:
        c.execute(
            "INSERT INTO edinet_metrics(asof_date,ticker,fcf_ttm,failure_reasons) VALUES ('2026-11-16','0000',80,'[]')"
        )
        c.execute("CREATE TABLE retained_extra(key TEXT PRIMARY KEY,value BLOB)")
        c.execute("INSERT INTO retained_extra VALUES (?,?)", ("native", b"\x00\xffraw"))
        for name in DETAILS:
            c.execute(f"ALTER TABLE edinet_metrics DROP COLUMN {name}")
        c.execute("PRAGMA user_version=51")
    return source
