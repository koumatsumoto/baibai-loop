from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

from baibai_engine.market.jpx_rebuild import store_jpx_regulations


def test_regulation_receipt_uses_the_supplied_acquisition_instant(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    fetched_at = "2026-05-08T08:00:00+00:00"
    store_jpx_regulations(
        path,
        date(2026, 5, 8),
        flags_by_ticker={"7203": ["特別注意銘柄"]},
        source_names=["特別注意銘柄"],
        fetched_at_utc=fetched_at,
    )
    with closing(sqlite3.connect(path)) as connection:
        for table in ("jpx_regulation_flags", "jpx_regulation_sources", "source_coverage"):
            assert connection.execute(f"SELECT fetched_at_utc FROM {table}").fetchall() == [
                (fetched_at,)
            ]
