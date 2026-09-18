"""調査工程へ不足と空を区別するread transactionと保存行を見せる。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def required_read(
    path: Path, connector: Callable[[Path], sqlite3.Connection]
) -> Iterator[sqlite3.Connection]:
    if not path.is_file():
        raise FileNotFoundError("store unavailable")
    with_connection = connector(path)
    try:
        with_connection.execute("BEGIN")
        if not with_connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone():
            raise FileNotFoundError("store unwritten")
        yield with_connection
    finally:
        with_connection.close()
