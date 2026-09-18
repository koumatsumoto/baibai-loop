"""見積り較正工程へatomic currentの保存rowをquery-onlyで見せる。"""

from __future__ import annotations

import base64
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from baibai_engine.foundation.sqlite_pages import object_payload, select_page

from .store import (
    CACHE_SCHEMA_VERSION,
    forward_row_from_mapping,
    panel_row_from_mapping,
    validate_panel_meta,
)


class SnapshotChangedError(ValueError):
    """Current was replaced between related reads."""


def snapshot_marker(path: Path) -> str:
    stat = path.stat()
    return base64.urlsafe_b64encode(
        json.dumps(
            [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns], separators=(",", ":")
        ).encode()
    ).decode()


def read_rows(
    path: Path,
    *,
    kind: str,
    filters: dict[str, object],
    after: list[str | int | float] | None,
    limit: int,
) -> tuple[list[dict[str, Any]], str]:
    marker = snapshot_marker(path)
    if filters.get("snapshot_token") not in (None, marker):
        raise SnapshotChangedError("snapshot replaced")
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        if not connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' LIMIT 1"
        ).fetchone():
            raise FileNotFoundError("unwritten snapshot")
        row = connection.execute(
            "SELECT contract_version FROM snapshot_meta WHERE singleton=1"
        ).fetchone()
        if row is None or row[0] != CACHE_SCHEMA_VERSION:
            raise ValueError("calibration contract differs")
        if kind not in {"cohort", "panel_row", "forward_row"}:
            raise ValueError("unknown calibration row kind")
        if kind != "cohort":
            parent = connection.execute(
                "SELECT forward_ready FROM cohort WHERE asof=?", (filters["asof"],)
            ).fetchone()
            if parent is None or (kind == "forward_row" and not parent[0]):
                raise FileNotFoundError("cohort or forward unavailable")
        rows = select_page(
            connection,
            table=kind,
            order={
                "cohort": ("asof",),
                "panel_row": ("ticker",),
                "forward_row": ("ticker", "horizon"),
            }[kind],
            equal={key: filters[key] for key in ("asof", "ticker", "horizon") if key in filters},
            ranges=[
                ("asof", op, filters[key])
                for key, op in (("from", ">="), ("to", "<="))
                if key in filters
            ],
            after=after,
            limit=limit,
        )
        for item in rows:
            if kind == "cohort":
                item["diagnostics"] = validate_panel_meta(object_payload(item["diagnostics"]))
                item["forward_policy"] = object_payload(item["forward_policy"])
                item["forward_ready"] = bool(item["forward_ready"])
                item["contract_version"] = CACHE_SCHEMA_VERSION
            else:
                payload = object_payload(item["payload"])
                (panel_row_from_mapping if kind == "panel_row" else forward_row_from_mapping)(
                    payload
                )
                if any(
                    payload.get(key) != item[key]
                    for key in ("asof", "ticker", "horizon")
                    if key in item
                ):
                    raise ValueError("calibration identity differs")
                item["payload"] = payload
        if snapshot_marker(path) != marker:
            raise SnapshotChangedError("snapshot replaced during read")
        return rows, marker
