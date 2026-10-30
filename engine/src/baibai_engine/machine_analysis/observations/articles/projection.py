"""Project one retained response using the provider-native contract, without live IO.

Meaning extraction is a separate step writing ArticleExtractionArtifact; this
owner only records article identity, text coordinates, native dates and coverage.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime

from pypdf.errors import PdfReadError

from baibai_engine.data_collection.providers.articles.native import (
    PARSER_REVISION,
    TEXT_REVISION,
    NativeArticleUnsupported,
    is_index_locator,
    parse_document,
    parse_index,
)
from baibai_engine.data_collection.receipts.articles.store import read_body, read_receipt
from baibai_engine.machine_analysis.observations.articles.contracts import (
    ArticleObservation,
    ArticleProjectionRun,
    ArticleTime,
)
from baibai_engine.machine_analysis.observations.articles.store import (
    ArticleStoreError,
    read_observations,
    save_projection,
)


def native_time(value: str | None, *, source: str) -> ArticleTime:
    if not value:
        return ArticleTime()
    date_match = re.fullmatch(r"(\d{4})[-/](\d{2})[-/](\d{2})", value)
    if date_match:
        try:
            day = date(*(int(part) for part in date_match.groups()))
        except ValueError:
            return ArticleTime(raw=value)
        return ArticleTime(raw=value, value=day.isoformat(), precision="date")
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
    index_observation_id: str | None = None,
    generated_at: datetime | None = None,
) -> tuple[ArticleProjectionRun, tuple[ArticleObservation, ...]]:
    """Save native run status, including valid empty and failed parse, independently of L1."""
    receipt = read_receipt(conn, receipt_id)
    if index_observation_id is not None and kind != "document":
        raise ValueError("index observation applies only to document projection")
    index_observation = None
    if index_observation_id is not None:
        index_observation = read_observations(conn, [index_observation_id])[0]
        index_receipt = read_receipt(
            conn,
            index_observation.receipt_id,
            receipt_sha256=index_observation.receipt_sha256,
        )
        fetch_url = index_observation.native_metadata.get("fetch_url")
        if not isinstance(fetch_url, str):
            fetch_url = index_observation.original_url
        if (
            index_observation.source != receipt.source
            or fetch_url != receipt.requested_url
            or index_receipt.received_at > receipt.received_at
            or not is_index_locator(receipt.source, index_observation.native_locator)
        ):
            raise ArticleStoreError("index observation does not identify this document receipt")
    elif kind == "document" and receipt.source == "nhk_news" and receipt.body_ref is not None:
        # A directly fetched orphan has URL identity. A retained feed match must
        # instead name its exact observation; choosing a GUID by URL is ambiguous.
        for (raw,) in conn.execute(
            "SELECT observation_json FROM article_observations "
            "WHERE json_extract(observation_json,'$.source')='nhk_news' "
            "AND json_extract(observation_json,'$.original_url')=?",
            (receipt.requested_url,),
        ):
            candidate = ArticleObservation.model_validate_json(raw)
            if not is_index_locator(candidate.source, candidate.native_locator):
                continue
            origin = read_receipt(
                conn, candidate.receipt_id, receipt_sha256=candidate.receipt_sha256
            )
            if origin.received_at <= receipt.received_at:
                raise ArticleStoreError("NHK document requires explicit index observation")
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
                        title=index_observation.title if index_observation else title,
                        encoding=receipt.encoding or "utf-8",
                    ),
                )
                if index_observation is not None:
                    native = (
                        native[0].model_copy(
                            update={
                                "article_id": index_observation.article_id,
                                "metadata": {
                                    **native[0].metadata,
                                    "index_observation_id": index_observation_id,
                                },
                            }
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
        except NativeArticleUnsupported:
            status = "unsupported"
            error_code = "body_region_unsupported"
        except (ValueError, LookupError, PdfReadError) as exc:
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
    return save_projection(conn, run, observations), observations
