from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from baibai_engine.screening.sqlite_cache import open_connection

REFERENCE = Path(__file__).resolve().parents[1] / "docs/reference/screening-runtime.md"


def _store_tables(tmp_path: Path) -> set[str]:
    path = tmp_path / "market.sqlite"
    open_connection(path).close()
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        connection.close()


def test_every_market_table_is_documented(tmp_path: Path) -> None:
    """The reference claims to list what the store creates, so a gap is a wrong claim.

    A table nobody documented is one nobody can read from `research` or a replay without
    opening the schema, and the omission is invisible from the document itself.
    """
    reference = REFERENCE.read_text(encoding="utf-8")
    documented = set(re.findall(r"^- `(\w+)\(", reference, flags=re.MULTILINE))

    assert _store_tables(tmp_path) <= documented
