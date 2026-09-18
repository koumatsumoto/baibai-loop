"""調査工程へ保存済みapplication行を、再評価せず有限pageとして見せる。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any


def object_payload(raw: object) -> dict[str, Any]:
    value = json.loads(str(raw))
    if not isinstance(value, dict):
        raise ValueError("stored payload must be an object")
    return value


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


def jst_day_ranges(column: str, filters: Mapping[str, object]) -> list[tuple[str, str, object]]:
    """Bind inclusive JST calendar days as an offset-aware half-open interval."""
    from datetime import date, datetime, time, timedelta
    from zoneinfo import ZoneInfo

    ranges: list[tuple[str, str, object]] = []
    for name, op in (("from", ">="), ("to", "<")):
        if name not in filters:
            continue
        day = date.fromisoformat(str(filters[name]))
        if name == "to":
            day += timedelta(days=1)
        instant = datetime.combine(day, time(), ZoneInfo("Asia/Tokyo"))
        # Julian days match SQLite's timezone-aware timestamp ordering.
        ranges.append((f"julianday({column})", op, instant.timestamp() / 86400 + 2440587.5))
    return ranges
