"""Shared TradingView collection errors and safe diagnostics, independent of CLI."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Literal

import httpx2
from mcp.client.auth.exceptions import OAuthFlowError
from mcp.shared.exceptions import MCPError

from baibai_engine.collection.tradingview.auth import (
    AuthenticationError,
    SecretPersistenceError,
)
from baibai_engine.external_assessment.tradingview.observations import (
    COLUMNS,
    FetchError,
    ProviderHTTPError,
    ProviderPayloadError,
)
from baibai_engine.market.jquants_decode import JQuantsProviderError
from baibai_engine.storage.observations.schema import SQLiteSchemaError


@dataclass(frozen=True)
class CollectionProgress:
    snapshot_date: str
    phase: Literal["prepare", "fetch", "normalize", "between_chunks", "store", "complete"]
    expected_universe: int
    chunks_total: int
    chunks_completed: int = 0
    chunk_index: int | None = None
    chunk_size: int | None = None
    columns_count: int = len(COLUMNS)
    interval_seconds: float = 15.0
    response_bytes: int = 0
    provider_elapsed_seconds: float = 0.0
    max_chunk_elapsed_seconds: float = 0.0
    elapsed_seconds: float = 0.0
    first_fetched_at_utc: str | None = None
    last_fetched_at_utc: str | None = None
    requests_attempted: int = 0
    symbols_requested: int = 0
    master_asof: str | None = None


class CollectionFailure(FetchError):
    def __init__(self, cause: BaseException, progress: CollectionProgress) -> None:
        self.cause = cause
        self.progress = progress
        super().__init__("TradingView collection failed")


class TimeGuardError(FetchError):
    """Observation is outside the allowed date or close time."""


class SourceDataError(FetchError):
    """Required local source data is unavailable or invalid."""


def failure_category(exc: BaseException) -> str:
    """Return fixed labels only, including errors wrapped by SDK task groups."""
    if isinstance(exc, CollectionFailure):
        return failure_category(exc.cause)
    if isinstance(exc, BaseExceptionGroup):
        categories = [failure_category(child) for child in exc.exceptions]
        # Prefer actionable root failures over accompanying transport cleanup errors.
        for category in (
            "secret_persistence",
            "auth",
            "time_guard",
            "storage",
            "provider_rate_limit",
            "provider_response",
            "provider_transport",
        ):
            if category in categories:
                return category
        return "internal"
    if isinstance(exc, SecretPersistenceError):
        return "secret_persistence"
    if isinstance(exc, (AuthenticationError, OAuthFlowError)):
        return "auth"
    if isinstance(exc, TimeGuardError):
        return "time_guard"
    if isinstance(exc, (SourceDataError, SQLiteSchemaError, JQuantsProviderError, sqlite3.Error)):
        return "storage"
    if isinstance(exc, (httpx2.HTTPStatusError, ProviderHTTPError)):
        status = exc.status_code if isinstance(exc, ProviderHTTPError) else exc.response.status_code
        if status in (401, 403):
            return "auth"
        if status == 429:
            return "provider_rate_limit"
        return "provider_response"
    if isinstance(exc, (httpx2.TransportError, TimeoutError)):
        return "provider_transport"
    if isinstance(exc, (FetchError, json.JSONDecodeError)):
        return "provider_response"
    return "internal"


# This explicit boundary is shared by the writer and the workflow log reader.
PROGRESS_FIELDS = (
    "snapshot_date",
    "master_asof",
    "phase",
    "expected_universe",
    "chunks_total",
    "chunks_completed",
    "requests_attempted",
    "symbols_requested",
    "chunk_index",
    "chunk_size",
    "columns_count",
    "interval_seconds",
    "response_bytes",
    "provider_elapsed_seconds",
    "max_chunk_elapsed_seconds",
    "elapsed_seconds",
    "first_fetched_at_utc",
    "last_fetched_at_utc",
    "oauth_rotations",
)


def safe_progress(payload: dict[str, object]) -> dict[str, object]:
    return {
        key: payload[key]
        for key in PROGRESS_FIELDS
        if key in payload
        and (payload[key] is None or type(payload[key]) in (str, int, float, bool))
    }


def _diagnostic_error(exc: BaseException, category: str) -> BaseException:
    if isinstance(exc, BaseExceptionGroup):
        for child in exc.exceptions:
            if failure_category(child) == category:
                return _diagnostic_error(child, category)
    return exc


def _retry_not_before(response: httpx2.Response) -> str | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        value = value.strip()
        if value.isascii() and value.isdigit():
            deadline = datetime.now(UTC) + timedelta(seconds=int(value))
        else:
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                return None
        return deadline.astimezone(UTC).isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def failure_line(exc: BaseException) -> str:
    category = failure_category(exc)
    line = f"TradingView acquisition failed; category={category}"
    detail = _diagnostic_error(exc, category)
    if isinstance(detail, CollectionFailure):
        line += "".join(
            f"; {key}={value}" for key, value in safe_progress(asdict(detail.progress)).items()
        )
        detail = _diagnostic_error(detail.cause, category)
    if isinstance(detail, ProviderHTTPError):
        line += f"; provider_http_status={detail.status_code}"
    if isinstance(detail, httpx2.HTTPStatusError):
        line += f"; mcp_http_status={detail.response.status_code}"
        retry_at = _retry_not_before(detail.response)
        if retry_at is not None:
            line += f"; retry_not_before_utc={retry_at}"
    if isinstance(detail, ProviderPayloadError):
        line += f"; validation_reason={detail.reason}; validation_field={detail.field or '-'}"
    if category == "internal":
        # Identify the selected SDK leaf without its message, data or traceback.
        line += f"; exception_type={type(detail).__name__}"
        if isinstance(detail, MCPError):
            line += f"; mcp_error_code={detail.code}"
    return line
