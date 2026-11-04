"""Read retained J-Quants requests directly from fixed L1 Parquet files.

The caller supplies files materialized and verified against one fixed release.
A request is complete only when its native pagination chain is complete; the
presence of a derived row or a previous L2 store is never evidence of receipt.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsSource,
    RetainedRequest,
)
from baibai_engine.data_collection.providers.jquants.retained import decode_retained_rows
from baibai_engine.data_collection.providers.jquants.snapshot_contracts import JQUANTS_SOURCE_PAGES
from baibai_engine.storage.snapshots.duck import LakeSession
from baibai_engine.storage.snapshots.objects import LakeObjectCache
from baibai_engine.storage.snapshots.reader import (
    FixedRelease,
    accepted_dataset,
    materialize_partitions,
    selected_partitions,
)


def iter_retained_requests(
    connection: duckdb.DuckDBPyConnection,
    paths: Sequence[Path],
    *,
    source: JQuantsSource,
    known_at: datetime,
) -> Iterator[RetainedRequest]:
    """Read one source through a receipt-time cutoff, reporting incomplete requests.

    Receipt time alone does not prove when data became available to a consumer.
    This does not claim that retained L1 covers the whole requested history.
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
                    yield decode_retained_rows(current, pages)
                    pages = []
                current = identity
                pages.append(row)
        if current is not None:
            yield decode_retained_rows(current, pages)
    finally:
        cursor.close()


def iter_fixed_retained_requests(
    session: LakeSession,
    *,
    release: FixedRelease,
    cache: LakeObjectCache,
    source: JQuantsSource,
    known_at: datetime,
) -> Iterator[RetainedRequest]:
    """Read only digest-verified L1 partitions of one fixed release.

    Receipt month is a physical boundary, not the date the provider's data
    describes. Incomplete source coverage remains a property of the release.
    """
    if known_at.utcoffset() is None:
        raise ValueError("known_at requires a timezone")
    dataset = accepted_dataset(release, JQUANTS_SOURCE_PAGES)
    cutoff = known_at.astimezone(UTC)
    parts = tuple(
        part
        for part in selected_partitions(release, dataset.name)
        if (int(part.values["year"]), int(part.values["month"])) <= (cutoff.year, cutoff.month)
    )
    paths = materialize_partitions(cache, dataset=dataset, partitions=parts)
    yield from iter_retained_requests(session.connection, paths, source=source, known_at=known_at)
