"""Project one retained response using the provider-native contract, without live IO.

Meaning extraction is a separate step writing ArticleExtractionArtifact; this
owner only records article identity, text coordinates, native dates and coverage.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from baibai_engine.data_collection.providers.articles.native import (
    PARSER_REVISION,
    TEXT_REVISION,
    parse_document,
    parse_index,
)
from baibai_engine.data_collection.receipts.articles.store import read_body, read_receipt
from baibai_engine.machine_analysis.observations.articles.contracts import (
    ArticleObservation,
    ArticleProjectionRun,
    ArticleTime,
)
from baibai_engine.machine_analysis.observations.articles.store import save_projection


def native_time(value: str | None, *, source: str) -> ArticleTime:
    if not value:
        return ArticleTime()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return ArticleTime(raw=value, value=value, precision="date")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (ValueError, TypeError):
            return ArticleTime(raw=value)
    origin = "explicit_offset"
    if parsed.tzinfo is None:
        if source not in {"tsr_data", "tsr_flash"}:
            return ArticleTime(raw=value)
        from zoneinfo import ZoneInfo

        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Tokyo"))
        origin = "provider_japan_local_time"
    return ArticleTime(
        raw=value,
        value=parsed.isoformat(),
        precision="second" if re.search(r"\d{2}:\d{2}:\d{2}", value) else "minute",
        timezone_origin=origin,
    )


def project_receipt(
    conn: sqlite3.Connection,
    receipt_id: str,
    *,
    kind: str = "index",
    title: str = "",
    generated_at: datetime | None = None,
) -> tuple[ArticleProjectionRun, tuple[ArticleObservation, ...]]:
    """Save native run status, including valid empty and failed parse, independently of L1."""
    receipt = read_receipt(conn, receipt_id)
    generated_at = generated_at or datetime.now(UTC)
    observations: tuple[ArticleObservation, ...] = ()
    error_code = None
    if receipt.body_ref is None:
        status = "unsupported"
        error_code = f"receipt_{receipt.status}"
    else:
        body = read_body(conn, receipt.body_ref)
        try:
            if kind == "index":
                native = parse_index(
                    receipt.source,
                    body,
                    url=receipt.requested_url,
                    encoding=receipt.encoding or "utf-8",
                )
            elif kind == "document":
                native = (
                    parse_document(
                        receipt.source,
                        body,
                        url=receipt.requested_url,
                        title=title,
                        encoding=receipt.encoding or "utf-8",
                    ),
                )
            else:
                raise ValueError("unsupported projection kind")
            observations = tuple(
                ArticleObservation(
                    article_id=item.article_id,
                    source=receipt.source,
                    receipt_id=receipt.receipt_id,
                    receipt_sha256=receipt.sha256,
                    body_ref=receipt.body_ref,
                    native_locator=item.locator,
                    original_url=item.url,
                    canonical_url=item.canonical_url,
                    title=item.title,
                    published=native_time(item.published_raw, source=receipt.source),
                    updated=native_time(item.updated_raw, source=receipt.source),
                    content_scope=item.content_scope,
                    missing_parts=item.missing_parts,
                    parser_revision=PARSER_REVISION,
                    text_revision=TEXT_REVISION,
                    text=item.text,
                    native_metadata=item.metadata,
                )
                for item in native
            )
            status = "parsed" if observations else "empty"
        except Exception as exc:
            # Retain L1 and status without logging source text or manufacturing an empty success.
            status = "parse_failed"
            error_code = type(exc).__name__
    run = ArticleProjectionRun.model_validate(
        {
            "receipt_id": receipt.receipt_id,
            "receipt_sha256": receipt.sha256,
            "parser_revision": PARSER_REVISION,
            "generated_at": generated_at,
            "status": status,
            "observation_ids": tuple(item.observation_id for item in observations),
            "error_code": error_code,
        }
    )
    save_projection(conn, run, observations)
    return run, observations
