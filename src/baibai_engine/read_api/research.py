"""Query-only research revision views."""

from __future__ import annotations

import json
from pathlib import Path

from .sqlite import connect_read_only


def research_packet_payload(path: Path, *, packet_id: str) -> dict[str, object] | None:
    return _one(path, "research_packet", "packet_id", packet_id)


def list_research_packet_payloads(
    path: Path,
    *,
    ticker: str | None = None,
) -> list[dict[str, object]]:
    return _many(
        path,
        "research_packet",
        where=None if ticker is None else ("ticker = ?", (ticker,)),
        order="as_of DESC, published_at DESC, packet_id DESC",
    )


def list_research_review_payloads(
    path: Path,
    *,
    packet_id: str | None = None,
) -> list[dict[str, object]]:
    return _many(
        path,
        "research_review",
        where=None if packet_id is None else ("packet_id = ?", (packet_id,)),
        order="reviewed_at DESC, review_id DESC",
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


def research_packet_publication(
    path: Path,
    *,
    packet_id: str,
) -> dict[str, object] | None:
    rows = _publications(
        path,
        "research_packet",
        ("packet_id", "ticker", "as_of", "recommendation", "published_at", "supersedes_id"),
        where=("packet_id = ?", (packet_id,)),
        order="packet_id",
    )
    return None if not rows else rows[0]


def list_research_packet_publications(
    path: Path,
    *,
    ticker: str | None = None,
) -> list[dict[str, object]]:
    return _publications(
        path,
        "research_packet",
        ("packet_id", "ticker", "as_of", "recommendation", "published_at", "supersedes_id"),
        where=None if ticker is None else ("ticker = ?", (ticker,)),
        order="as_of DESC, published_at DESC, packet_id DESC",
    )


def list_research_review_publications(
    path: Path,
    *,
    packet_id: str | None = None,
) -> list[dict[str, object]]:
    return _publications(
        path,
        "research_review",
        ("review_id", "packet_id", "reviewed_at"),
        where=None if packet_id is None else ("packet_id = ?", (packet_id,)),
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
        ("holding_review_id", "ticker", "as_of", "packet_id", "candidate_packet_id"),
        where=None if ticker is None else ("ticker = ?", (ticker,)),
        order="as_of DESC, holding_review_id DESC",
    )


def _one(
    path: Path,
    table: str,
    key_column: str,
    key: str,
) -> dict[str, object] | None:
    if not path.is_file():
        return None
    connection = connect_read_only(path)
    try:
        row = connection.execute(
            f"SELECT payload FROM {table} WHERE {key_column} = ?",
            (key,),
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else _object(str(row[0]))


def _many(
    path: Path,
    table: str,
    *,
    where: tuple[str, tuple[object, ...]] | None,
    order: str,
) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    clause, parameters = ("", ()) if where is None else (f" WHERE {where[0]}", where[1])
    connection = connect_read_only(path)
    try:
        rows = connection.execute(
            f"SELECT payload FROM {table}{clause} ORDER BY {order}",
            parameters,
        ).fetchall()
    finally:
        connection.close()
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
    if not path.is_file():
        return []
    clause, parameters = ("", ()) if where is None else (f" WHERE {where[0]}", where[1])
    selected = ", ".join((*columns, "payload"))
    connection = connect_read_only(path)
    try:
        rows = connection.execute(
            f"SELECT {selected} FROM {table}{clause} ORDER BY {order}",
            parameters,
        ).fetchall()
    finally:
        connection.close()
    return [
        {**{column: row[column] for column in columns}, "payload": _object(str(row["payload"]))}
        for row in rows
    ]


__all__ = [
    "list_holding_review_payloads",
    "list_holding_review_publications",
    "list_research_packet_payloads",
    "list_research_packet_publications",
    "list_research_review_payloads",
    "list_research_review_publications",
    "research_packet_payload",
    "research_packet_publication",
]
