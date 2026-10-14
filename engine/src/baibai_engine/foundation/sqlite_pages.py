"""保存済み行を再評価せず有限pageとして見せる。"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any


def select_page(
    connection: sqlite3.Connection,
    *,
    table: str,
    columns: str = "*",
    order: tuple[str, ...],
    equal: Mapping[str, object],
    ranges: Sequence[tuple[str, str, object]] = (),
    after: list[str | int | float] | None = None,
    limit: int,
) -> list[dict[str, Any]]:
    """Identifiers/expressions come only from domain code; all request values are bound."""
    clauses = [f"{key} = ?" for key in equal]
    parameters = list(equal.values())
    for column, comparison, value in ranges:
        if comparison not in {">=", "<", "<=", ">"}:
            raise ValueError("invalid comparison")
        clauses.append(f"{column} {comparison} ?")
        parameters.append(value)
    if after is not None:
        if len(after) != len(order):
            raise ValueError("invalid page key")
        clauses.append(f"({', '.join(order)}) > ({', '.join('?' for _ in order)})")
        parameters.extend(after)
    where = " AND ".join(clauses) or "1"
    parameters.append(limit)
    return [
        dict(row)
        for row in connection.execute(
            f"SELECT {columns} FROM {table} WHERE {where} "  # nosec B608
            f"ORDER BY {', '.join(order)} LIMIT ?",
            parameters,
        )
    ]
