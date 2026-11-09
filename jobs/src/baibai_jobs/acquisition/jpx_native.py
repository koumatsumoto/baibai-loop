"""Capture JPX originals into a receipt-only stage, without L2 projection."""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

from baibai_engine.data_collection.providers.jpx.discovery import (
    EARNINGS_INDEX_URL,
    REGULATION_URLS,
    earnings_urls,
)
from baibai_engine.data_collection.providers.jpx.http import JPXHTTP
from baibai_engine.data_collection.providers.jpx.receipt_store import open_receipts
from baibai_engine.data_collection.providers.jpx.receipts import (
    complete_bundle,
    create_bundle,
    declare_bundle_resources,
    retain_bundle_response,
)
from baibai_engine.storage.json import write_json_atomic


def acquire(source: str, asof: date, directory: Path, client: JPXHTTP) -> bool:
    if source not in {"jpx-earnings", "jpx-regulations"}:
        raise ValueError("unsupported JPX source")
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "receipts.sqlite").exists() or (directory / "result.json").exists():
        raise FileExistsError("use a new private handoff directory")
    started = datetime.now(UTC)
    error: str | None = None
    with closing(open_receipts(directory / "receipts.sqlite")) as conn:
        collection = "earnings_calendar" if source == "jpx-earnings" else "regulations"
        resources = None if source == "jpx-earnings" else tuple(REGULATION_URLS.items())
        bundle = create_bundle(conn, collection, asof, started, resources)
        try:
            if source == "jpx-earnings":
                index = client.fetch(EARNINGS_INDEX_URL)
                retain_bundle_response(conn, bundle, "index", index)
                urls = earnings_urls(index.body, index.requested_url, index.content_type)
                resources = (
                    ("index", index.requested_url),
                    *((f"workbook:{number}", url) for number, url in enumerate(urls)),
                )
                declare_bundle_resources(conn, bundle, resources)
                pending = resources[1:]
            else:
                assert resources is not None
                pending = resources
            for name, url in pending:
                retain_bundle_response(conn, bundle, name, client.fetch(url))
            complete_bundle(conn, bundle)
        except Exception as exc:
            # Keep each original already received, even if a later request fails.
            error = type(exc).__name__
    write_json_atomic(
        directory / "result.json",
        {
            "source": source,
            "asof": asof.isoformat(),
            "started_at": started.isoformat(),
            "bundle_ids": [bundle],
            "acquisition": {"status": "failed" if error else "received", "error": error},
        },
    )
    return error is None
