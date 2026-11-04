"""Read retained J-Quants requests directly from fixed L1 Parquet files.

The caller supplies files materialized and verified against one fixed release.
A request is complete only when its native pagination chain is complete; the
presence of a derived row or a previous L2 store is never evidence of receipt.
"""

from __future__ import annotations

import gzip
import json
import zlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import duckdb

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsAcquisitionError,
    JQuantsPage,
    JQuantsRequest,
    JQuantsSource,
    complete_request,
)


@dataclass(frozen=True, slots=True)
class RetainedRequest:
    request_id: str
    request: JQuantsRequest | None
    error: str | None
    first_query: dict[str, str] | None = None


def iter_retained_requests(
    connection: duckdb.DuckDBPyConnection,
    paths: Sequence[Path],
    *,
    source: JQuantsSource,
    known_at: datetime,
) -> Iterator[RetainedRequest]:
    """Read one source at a known time, reporting incomplete requests explicitly.

    This does not claim that the retained L1 covers the whole requested history.
    Files are exact, validated release objects, never arbitrary globs or URLs.
    """
    if known_at.utcoffset() is None:
        raise ValueError("known_at requires a timezone")
    if not paths:
        return
    if any(not path.is_absolute() or not path.is_file() for path in paths):
        raise ValueError("verified local Parquet files are required")
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT request_id, page_index, source, query_json, received_at_utc, body_gzip "
            "FROM read_parquet($files, union_by_name = false) "
            "WHERE CAST(received_at_utc AS TIMESTAMPTZ) <= $cutoff "
            "AND request_id IN (SELECT request_id FROM read_parquet($files, union_by_name = false) "
            "WHERE source = $source AND CAST(received_at_utc AS TIMESTAMPTZ) <= $cutoff) "
            "ORDER BY request_id, page_index",
            {
                "files": [str(p) for p in paths],
                "source": source,
                "cutoff": known_at.astimezone(UTC),
            },
        )
        current: str | None = None
        pages: list[tuple[object, ...]] = []
        while batch := cursor.fetchmany(256):
            for row in batch:
                identity = str(row[0])
                if current is not None and identity != current:
                    yield _complete(current, pages)
                    pages = []
                current = identity
                pages.append(row)
        if current is not None:
            yield _complete(current, pages)
    finally:
        cursor.close()


def _complete(identity: str, rows: list[tuple[object, ...]]) -> RetainedRequest:
    pages: list[JQuantsPage] = []
    first_query: dict[str, str] | None = None
    try:
        for index, row in enumerate(rows):
            _, page_index, source, query_json, received, body = row
            if page_index != index:
                raise ValueError("missing or duplicate page index")
            query = json.loads(cast(str, query_json))
            if not isinstance(query, dict) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in query.items()
            ):
                raise ValueError("invalid query")
            if index == 0:
                first_query = query
            pages.append(
                JQuantsPage(
                    cast(JQuantsSource, source),
                    query,
                    datetime.fromisoformat(cast(str, received)),
                    gzip.decompress(cast(bytes, body)),
                )
            )
        return RetainedRequest(identity, complete_request(pages), None, first_query)
    except (JQuantsAcquisitionError, ValueError, TypeError, OSError, EOFError, zlib.error) as error:
        return RetainedRequest(identity, None, str(error), first_query)
