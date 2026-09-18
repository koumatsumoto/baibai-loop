"""調査工程へlakeが所有しない取得帳簿と資本政策の保存値を見せる。"""

from __future__ import annotations

import sqlite3
from typing import Any

from baibai_engine.foundation.sqlite_pages import select_page

from .schema import validate_current_schema


def local_rows(
    connection: sqlite3.Connection,
    *,
    kind: str,
    filters: dict[str, object],
    after: list[str | int | float] | None,
    limit: int,
) -> list[dict[str, Any]]:
    validate_current_schema(connection)
    if kind == "source_coverage":
        order = ("source", "coverage_key")
        equal = filters
        ranges = []
    elif kind == "tse_capital_policy_snapshots":
        order = ("snapshot_month_end", "ticker")
        equal = {k: v for k, v in filters.items() if k in order}
        ranges = [
            ("snapshot_month_end", op, filters[key])
            for key, op in (("from", ">="), ("to", "<="))
            if key in filters
        ]
    else:
        raise ValueError("unknown market local table")
    return select_page(
        connection, table=kind, order=order, equal=equal, ranges=ranges, after=after, limit=limit
    )
