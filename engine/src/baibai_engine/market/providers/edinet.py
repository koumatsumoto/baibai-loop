from __future__ import annotations

import io
import json
import time
import zipfile
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

from baibai_engine.market.edinet_decode import safe_error_value, text_or_none
from baibai_engine.market.edinet_document_events import parse_doc_id
from baibai_engine.market.edinet_models import (
    EdinetMetricRecord,
    EDINETProviderError,
    EDINETRateLimitError,
)

EDINET_API_BASE = "https://api.edinet-fsa.go.jp/api/v2"


class EDINETProvider:
    """API v2 client. Subscription-Key is always passed as a query parameter."""

    def __init__(
        self,
        api_key: str | None,
        cache_dir: Path,
        session: requests.Session | None = None,
        *,
        sqlite_path: Path | None = None,
        cache_only: bool = False,
    ) -> None:
        self._api_key = api_key
        self._cache_dir = Path(cache_dir) / "edinet"
        self._session = session or requests.Session()
        self._sqlite_path = Path(sqlite_path) if sqlite_path is not None else None
        self._cache_only = cache_only

    def list_documents(
        self,
        on_date: date,
        *,
        force_refresh: bool = False,
        is_final: bool = False,
    ) -> list[dict[str, Any]]:
        if self._sqlite_path is not None and not force_refresh:
            from baibai_engine.market.edinet_metrics.store import read_edinet_documents

            cached = read_edinet_documents(self._sqlite_path, on_date)
            if cached is not None:
                return cached
        self._raise_if_cache_only("edinet_documents", on_date.isoformat())
        api_key = self._require_api_key("list_documents")
        query = urlencode(
            {
                "date": on_date.isoformat(),
                "type": 2,
                "Subscription-Key": api_key,
            }
        )
        url = f"{EDINET_API_BASE}/documents.json?{query}"
        data = self._request_json(url)
        results = data.get("results", [])
        if not isinstance(results, list):
            raise EDINETProviderError("EDINET documents.json returned unexpected results payload")
        metadata = data.get("metadata")
        if not isinstance(metadata, Mapping):
            raise EDINETProviderError("EDINET documents.json returned no metadata")
        resultset = metadata.get("resultset")
        if not isinstance(resultset, Mapping):
            raise EDINETProviderError("EDINET documents.json returned no resultset metadata")
        raw_result_count = resultset.get("count")
        if raw_result_count is None:
            raise EDINETProviderError("EDINET documents.json returned invalid resultset count")
        try:
            result_count = int(str(raw_result_count))
        except (TypeError, ValueError) as exc:
            raise EDINETProviderError(
                "EDINET documents.json returned invalid resultset count"
            ) from exc
        if result_count != len(results):
            raise EDINETProviderError(
                "EDINET documents.json resultset count mismatch: "
                f"metadata={result_count} results={len(results)}"
            )
        process_datetime = text_or_none(metadata.get("processDateTime"))
        documents = _coerce_document_items(results, source="EDINET documents.json")
        for document in documents:
            document["doc_date"] = on_date.isoformat()
        if self._sqlite_path is not None:
            from baibai_engine.storage.observations.ingest.edinet import store_edinet_documents

            store_edinet_documents(
                self._sqlite_path,
                on_date,
                documents,
                process_datetime=process_datetime,
                result_count=result_count,
                is_final=is_final,
                response_json=json.dumps(
                    data, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                ),
            )
        return documents

    def load_metric_records(self, asof_date: date) -> dict[str, EdinetMetricRecord]:
        if self._sqlite_path is not None:
            from baibai_engine.market.edinet_metrics.store import read_edinet_metrics

            cached = read_edinet_metrics(self._sqlite_path, asof_date)
            if cached is not None:
                return dict(cached)
        return {}

    def bootstrap_cache(self, start: date, end: date) -> dict[str, int]:
        fetched = self._refresh_document_state(end)
        cursor = start
        while cursor <= end:
            if cursor not in fetched:
                fetched[cursor] = len(self.list_documents(cursor, is_final=cursor < end))
            cursor += timedelta(days=1)
        return {"documents": sum(fetched.values())}

    def refresh_document_state(self, asof_date: date) -> dict[str, int]:
        """Refresh the mutable target day, then finalize every older unresolved day."""
        fetched = self._refresh_document_state(asof_date)
        return {"documents": sum(fetched.values())}

    def backfill_document_identity(self, start: date, end: date) -> dict[str, int]:
        """Re-list every day in the window so stored rows carry submitter and target codes.

        The cached rows are otherwise complete, so this bypasses the cache deliberately
        rather than dropping coverage: a day whose coverage were deleted would read as
        an unobserved day to every consumer until the whole window finished.
        """
        days = 0
        documents = 0
        cursor = start
        while cursor <= end:
            documents += len(self.list_documents(cursor, force_refresh=True, is_final=cursor < end))
            days += 1
            cursor += timedelta(days=1)
        return {"days": days, "documents": documents}

    def _refresh_document_state(self, end: date) -> dict[date, int]:
        fetched = {
            end: len(self.list_documents(end, force_refresh=True, is_final=False)),
        }
        if self._sqlite_path is not None:
            from baibai_engine.market.edinet_metrics.store import (
                read_unfinalized_edinet_document_dates,
            )

            for unresolved in read_unfinalized_edinet_document_dates(self._sqlite_path, before=end):
                fetched[unresolved] = len(
                    self.list_documents(unresolved, force_refresh=True, is_final=True)
                )
        return fetched

    def download_csv_zip(self, doc_id: str) -> bytes:
        return self._download_document_zip(doc_id, source_type=5)

    def download_xbrl_zip(self, doc_id: str) -> bytes:
        return self._download_document_zip(doc_id, source_type=1)

    def _download_document_zip(self, doc_id: str, *, source_type: int) -> bytes:
        from baibai_engine.market.edinet_sources import read_source_archive

        safe_id = parse_doc_id(doc_id)
        source_name = "csv" if source_type == 5 else "xbrl"
        if self._sqlite_path is not None:
            retained = read_source_archive(self._sqlite_path, safe_id, source_type)
            if retained is not None:
                if not _is_zip_bytes(retained):
                    raise EDINETProviderError(f"stored EDINET {source_name} archive is not a ZIP")
                return retained
        cache = self._cache_dir / f"{source_name}_zips" / f"{safe_id}.zip"
        if cache.exists():
            content = cache.read_bytes()
            if _is_zip_bytes(content):
                return self._retain_document_zip(safe_id, source_type, content)
            cache.unlink()
        self._raise_if_cache_only(f"edinet_{source_name}_zip", safe_id)
        query = urlencode(
            {
                "type": source_type,
                "Subscription-Key": self._require_api_key("download_document_zip"),
            }
        )
        content = self._request_bytes(f"{EDINET_API_BASE}/documents/{safe_id}?{query}")
        if not _is_zip_bytes(content):
            raise EDINETProviderError(
                f"EDINET {source_name.upper()} ZIP response was not a zip: "
                f"{_non_zip_response_message(content)}"
            )
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(content)
        return self._retain_document_zip(safe_id, source_type, content)

    def _retain_document_zip(self, doc_id: str, source_type: int, content: bytes) -> bytes:
        if self._sqlite_path is None:
            return content
        from baibai_engine.market.edinet_sources import retain_source_archive

        return retain_source_archive(
            self._sqlite_path,
            doc_id,
            source_type,
            content,
            stored_on=datetime.now(UTC).date(),
        )

    def _require_api_key(self, operation: str) -> str:
        if not self._api_key:
            raise EDINETProviderError(f"EDINET_API_KEY is required for {operation}")
        return self._api_key

    def _raise_if_cache_only(self, source: str, requirement: str) -> None:
        if not self._cache_only:
            return
        sqlite_label = self._sqlite_path.as_posix() if self._sqlite_path is not None else "<none>"
        raise EDINETProviderError(
            f"SQLite cache incomplete for {source} ({requirement}); "
            f"sqlite={sqlite_label}. `screening run` is cache-only: run "
            "`bootstrap-cache --asof` / `extract-edinet-metrics` or repair SQLite "
            "before running screening."
        )

    def _request_json(self, url: str) -> dict[str, Any]:
        with closing(self._request_response(url, timeout=30)) as response:
            payload = response.json()
        if not isinstance(payload, dict):
            raise EDINETProviderError("EDINET response JSON must be an object")
        return payload

    def _request_bytes(self, url: str) -> bytes:
        with closing(self._request_response(url, timeout=60)) as response:
            return bytes(response.content)

    def _request_response(self, url: str, *, timeout: int) -> requests.Response:
        """One retry budget for HTTP failures and EDINET's HTTP 200 body 429."""
        max_attempts = 6
        for attempt in range(max_attempts):
            try:
                response = self._session.get(url, timeout=timeout)
            except requests.RequestException as exc:
                if attempt < max_attempts - 1:
                    time.sleep(3 * (2**attempt))
                    continue
                message = self._sanitize_secret(str(exc))
                raise EDINETProviderError(
                    f"EDINET request raised: {type(exc).__name__}: {message}"
                ) from None
            rate_limited = response.status_code == 429 or (
                response.status_code == 200 and _non_zip_response_status(response.content) == "429"
            )
            if response.status_code < 400 and not rate_limited:
                return response
            try:
                if rate_limited and attempt >= max_attempts - 1:
                    raise EDINETRateLimitError("EDINET request rate limited after retries")
                if not rate_limited and response.status_code not in {500, 502, 503, 504}:
                    raise EDINETProviderError(
                        f"EDINET request failed with status {response.status_code}"
                    )
                backoff = 3 * (2**attempt)
                wait = _retry_after_seconds(response, backoff) if rate_limited else backoff
            finally:
                response.close()
            if attempt < max_attempts - 1:
                time.sleep(wait)
        raise EDINETProviderError("EDINET request failed after retries")

    def _sanitize_secret(self, text: str) -> str:
        if self._api_key and self._api_key in text:
            return text.replace(self._api_key, "<redacted>")
        return text


def _retry_after_seconds(response: requests.Response, backoff: int) -> float:
    value = response.headers.get("Retry-After", "").strip()
    if value.isdigit():
        return int(value)
    try:
        deadline = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return backoff
    if deadline.tzinfo is None:
        return backoff
    return max(0.0, (deadline - datetime.now(UTC)).total_seconds())


def _coerce_document_items(payload: list[Any], *, source: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, Mapping):
            raise EDINETProviderError(f"{source} contained a non-mapping document item")
        items.append(dict(item))
    return items


def _is_zip_bytes(content: bytes) -> bool:
    return zipfile.is_zipfile(io.BytesIO(content))


def _non_zip_response_status(content: bytes) -> str | None:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    status = payload.get("StatusCode") or payload.get("statusCode") or payload.get("status")
    return str(status) if status not in (None, "") else None


def _non_zip_response_message(content: bytes) -> str:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "zip_invalid"
    if not isinstance(payload, Mapping):
        return "zip_invalid"
    status = _non_zip_response_status(content)
    message = text_or_none(payload.get("message")) or text_or_none(payload.get("Message"))
    if status and message:
        return f"StatusCode={status} message={safe_error_value(message)}"
    if status:
        return f"StatusCode={status}"
    return "zip_invalid"
