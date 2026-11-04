"""Decode retained provider pages without depending on a physical store."""

from __future__ import annotations

import gzip
import hashlib
import json
import zlib
from datetime import datetime
from typing import cast

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsAcquisitionError,
    JQuantsPage,
    JQuantsSource,
    RetainedRequest,
    complete_request,
)


def decode_retained_rows(identity: str, rows: list[tuple[object, ...]]) -> RetainedRequest:
    fingerprints: list[tuple[int, str]] = []
    pages: list[JQuantsPage] = []
    first_query: dict[str, str] | None = None
    last_received_at: datetime | None = None
    try:
        for row in rows:
            digest = hashlib.sha256()
            for value in row:
                data = value if isinstance(value, bytes) else str(value).encode()
                digest.update(len(data).to_bytes(8, "big"))
                digest.update(data)
            fingerprints.append((int(cast(int, row[1])), digest.hexdigest()))
        clocks = [datetime.fromisoformat(cast(str, row[4])) for row in rows]
        if any(clock.utcoffset() is None for clock in clocks):
            raise ValueError("receipt time requires a timezone")
        last_received_at = max(clocks, default=None)
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
        return RetainedRequest(
            identity,
            complete_request(pages),
            None,
            first_query,
            last_received_at,
            tuple(fingerprints),
        )
    except (JQuantsAcquisitionError, ValueError, TypeError, OSError, EOFError, zlib.error) as error:
        return RetainedRequest(
            identity, None, str(error), first_query, last_received_at, tuple(fingerprints)
        )
