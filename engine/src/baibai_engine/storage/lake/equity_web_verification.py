"""Verify complete affected L1 months and exact selected public response bytes."""

from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from baibai_engine.collection.equity_web.models import EquityWebSourceResponse
from baibai_engine.collection.equity_web.receipts import validate_response
from baibai_engine.storage.lake.datasets import EQUITY_WEB_SOURCE_RESPONSES
from baibai_engine.storage.lake.duck import LakeSession
from baibai_engine.storage.lake.objects import LakeObjectCache
from baibai_engine.storage.lake.reader import (
    FixedRelease,
    iter_partition_rows,
    materialize_partitions,
)
from baibai_engine.storage.observations.read import connect_read_only

type Month = tuple[int, int]
_NAMES = tuple(column.name for column in EQUITY_WEB_SOURCE_RESPONSES.columns)


def _response(row: Sequence[object]) -> EquityWebSourceResponse:
    try:
        received = datetime.fromisoformat(cast("str", row[7]))
        response = EquityWebSourceResponse(
            receipt_id=cast("str", row[0]),
            attempt_id=cast("str", row[1]),
            source=cast("str", row[2]),  # type: ignore[arg-type]
            requested_ticker=cast("str", row[3]),
            role=cast("str", row[4]),  # type: ignore[arg-type]
            parent_receipt_id=cast("str | None", row[5]),
            requested_url=cast("str", row[6]),
            received_at_utc=received,
            selected_encoding=cast("str", row[9]),
            body=gzip.decompress(cast("bytes", row[10])),
        )
        validate_response(response)
        if row[8] != received.date().isoformat():
            raise ValueError("equity web receipt date mismatch")
        return response
    except (OSError, TypeError, ValueError):
        raise ValueError("invalid equity web L1 response") from None


def response_digest(response: EquityWebSourceResponse) -> str:
    """Compact identity for exact selected-response metadata and original bytes."""
    validate_response(response)
    fields = (
        response.receipt_id,
        response.attempt_id,
        response.source,
        response.requested_ticker,
        response.role,
        response.parent_receipt_id,
        response.requested_url,
        response.received_at_utc.astimezone(UTC).isoformat(timespec="microseconds"),
        response.received_at_utc.date().isoformat(),
        response.selected_encoding,
    )
    digest = hashlib.sha256()
    metadata = json.dumps(fields, ensure_ascii=True, separators=(",", ":")).encode()
    digest.update(len(metadata).to_bytes(8, "big"))
    digest.update(metadata)
    digest.update(len(response.body).to_bytes(8, "big"))
    digest.update(response.body)
    return digest.hexdigest()


def _digest_rows(
    rows: Iterable[Sequence[object]],
    *,
    selected: Mapping[str, str] | None = None,
    found: set[str] | None = None,
) -> str:
    digest = hashlib.sha256()
    for row in rows:
        response = _response(row)
        if selected is not None and response.receipt_id in selected:
            if response_digest(response) != selected[response.receipt_id]:
                raise ValueError("equity web L1 selected receipt readback mismatch")
            assert found is not None
            found.add(response.receipt_id)
        metadata = json.dumps(tuple(row[:10]), ensure_ascii=True, separators=(",", ":")).encode()
        digest.update(len(metadata).to_bytes(8, "big"))
        digest.update(metadata)
        digest.update(len(response.body).to_bytes(8, "big"))
        digest.update(response.body)
    return digest.hexdigest()


def history_digests(database: Path, *, months: set[Month]) -> dict[Month, str]:
    """Digest every retained receipt in each affected UTC year/month."""
    result: dict[Month, str] = {}
    with closing(connect_read_only(database)) as conn:
        conn.execute("BEGIN")
        for year, month in sorted(months):
            start = f"{year:04}-{month:02}-01"
            end = f"{year + (month == 12):04}-{month % 12 + 1:02}-01"
            rows = conn.execute(
                f"SELECT {','.join(_NAMES)} FROM equity_web_source_responses "  # nosec B608
                "WHERE received_on >= ? AND received_on < ? ORDER BY receipt_id",
                (start, end),
            )
            result[(year, month)] = _digest_rows(rows)
    return result


def verify_release_history(
    session: LakeSession,
    *,
    release: FixedRelease,
    cache: LakeObjectCache,
    expected_history: Mapping[Month, str],
    selected_digests: Mapping[str, str],
) -> None:
    """Compare full changed-month history and selected original metadata/bytes."""
    manifest = release.dataset_manifests.get(EQUITY_WEB_SOURCE_RESPONSES.name)
    if expected_history and manifest is None:
        raise ValueError("equity web L1 dataset missing from fixed release")
    selected = selected_digests
    found: set[str] = set()
    for year, month in sorted(expected_history):
        assert manifest is not None
        partitions = tuple(
            part
            for part in manifest.partitions
            if (part.values["year"], part.values["month"]) == (year, month)
        )
        if len(partitions) != 1:
            raise ValueError("equity web L1 changed partition missing")
        paths = materialize_partitions(
            cache, dataset=EQUITY_WEB_SOURCE_RESPONSES, partitions=partitions
        )
        rows = (
            row
            for batch in iter_partition_rows(
                session, dataset=EQUITY_WEB_SOURCE_RESPONSES, paths=paths, columns=_NAMES
            )
            for row in batch
        )
        if _digest_rows(rows, selected=selected, found=found) != expected_history[(year, month)]:
            raise ValueError("equity web L1 publication changed received history")
    if found != set(selected):
        raise ValueError("equity web L1 selected receipt missing from fixed release")
