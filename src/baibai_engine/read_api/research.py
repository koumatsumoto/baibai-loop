"""Query-only research revision views."""

from __future__ import annotations

import json
from pathlib import Path

from .sqlite import read_rows


def list_thesis_payloads(
    path: Path,
    *,
    ticker: str | None = None,
) -> list[dict[str, object]]:
    return _many(
        path,
        "thesis",
        where=None if ticker is None else ("ticker = ?", (ticker,)),
        order="as_of DESC, published_at DESC, thesis_id DESC",
    )


def list_holding_review_payloads(
    path: Path,
    *,
    ticker: str | None = None,
) -> list[dict[str, object]]:
    return _many(
        path,
        "holding_review",
        where=None if ticker is None else ("ticker = ?", (ticker,)),
        order="as_of DESC, holding_review_id DESC",
    )


def thesis_publication(
    path: Path,
    *,
    thesis_id: str,
) -> dict[str, object] | None:
    rows = _publications(
        path,
        "thesis",
        ("thesis_id", "ticker", "as_of", "recommendation", "published_at", "supersedes_id"),
        where=("thesis_id = ?", (thesis_id,)),
        order="thesis_id",
    )
    return None if not rows else rows[0]


def list_thesis_publications(
    path: Path,
    *,
    ticker: str | None = None,
) -> list[dict[str, object]]:
    return _publications(
        path,
        "thesis",
        ("thesis_id", "ticker", "as_of", "recommendation", "published_at", "supersedes_id"),
        where=None if ticker is None else ("ticker = ?", (ticker,)),
        order="as_of DESC, published_at DESC, thesis_id DESC",
    )


def list_thesis_review_publications(
    path: Path,
    *,
    thesis_id: str | None = None,
) -> list[dict[str, object]]:
    return _publications(
        path,
        "thesis_review",
        ("review_id", "thesis_id", "reviewed_at"),
        where=None if thesis_id is None else ("thesis_id = ?", (thesis_id,)),
        order="reviewed_at DESC, review_id DESC",
    )


def list_holding_review_publications(
    path: Path,
    *,
    ticker: str | None = None,
) -> list[dict[str, object]]:
    return _publications(
        path,
        "holding_review",
        ("holding_review_id", "ticker", "as_of", "thesis_id", "candidate_thesis_id"),
        where=None if ticker is None else ("ticker = ?", (ticker,)),
        order="as_of DESC, holding_review_id DESC",
    )


def _many(
    path: Path,
    table: str,
    *,
    where: tuple[str, tuple[object, ...]] | None,
    order: str,
) -> list[dict[str, object]]:
    clause, parameters = ("", ()) if where is None else (f" WHERE {where[0]}", where[1])
    rows = read_rows(
        path,
        # Private callers provide fixed schema fragments; all values stay bound.
        f"SELECT payload FROM {table}{clause} ORDER BY {order}",  # nosec B608
        parameters,
    )
    return [_object(str(row[0])) for row in rows]


def _object(payload: str) -> dict[str, object]:
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise ValueError("research payload must be an object")
    return parsed


def _publications(
    path: Path,
    table: str,
    columns: tuple[str, ...],
    *,
    where: tuple[str, tuple[object, ...]] | None,
    order: str,
) -> list[dict[str, object]]:
    clause, parameters = ("", ()) if where is None else (f" WHERE {where[0]}", where[1])
    selected = ", ".join((*columns, "payload"))
    rows = read_rows(
        path,
        # Private callers provide fixed schema fragments; all values stay bound.
        f"SELECT {selected} FROM {table}{clause} ORDER BY {order}",  # nosec B608
        parameters,
    )
    return [
        {**{column: row[column] for column in columns}, "payload": _object(str(row["payload"]))}
        for row in rows
    ]


__all__ = [
    "list_holding_review_payloads",
    "list_holding_review_publications",
    "list_thesis_payloads",
    "list_thesis_publications",
    "list_thesis_review_publications",
    "thesis_publication",
]
