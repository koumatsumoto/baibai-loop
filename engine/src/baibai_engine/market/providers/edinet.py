from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from baibai_engine.collection.edinet.archives import read_source_archive, retain_source_archive
from baibai_engine.collection.edinet.http import EDINETHTTP
from baibai_engine.collection.edinet.models import EdinetAcquisitionError
from baibai_engine.collection.edinet.receipts import read_list_responses, retain_list_response
from baibai_engine.market.edinet_decode import safe_error_value, text_or_none
from baibai_engine.market.edinet_document_events import parse_doc_id
from baibai_engine.market.edinet_list_rebuild import rebuild_edinet_document_day
from baibai_engine.market.edinet_models import EDINETProviderError
from baibai_engine.storage.observations.read import connect_read_only
from baibai_engine.storage.observations.schema import connect_current, open_connection

if TYPE_CHECKING:
    import requests

_JST = ZoneInfo("Asia/Tokyo")


class EDINETProvider:
    """Read-through EDINET projection using explicit source acquisition."""

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
        self._session = session
        self._http: EDINETHTTP | None = None
        self._sqlite_path = Path(sqlite_path) if sqlite_path is not None else None
        self._cache_only = cache_only

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def _http_client(self, operation: str) -> EDINETHTTP:
        if self._http is None:
            self._http = EDINETHTTP(self._require_api_key(operation), session=self._session)
        return self._http

    def list_documents(
        self,
        on_date: date,
        *,
        force_refresh: bool = False,
    ) -> list[dict[str, Any]]:
        sqlite_path = self._sqlite_path
        if sqlite_path is None:
            raise EDINETProviderError("EDINET list acquisition requires sqlite_path")
        if not force_refresh and sqlite_path.exists():
            connection = connect_current(sqlite_path)
            if connection is not None:
                with closing(connection):
                    receipts = read_list_responses(connection, doc_date=on_date)
                if receipts:
                    projection = rebuild_edinet_document_day(sqlite_path, on_date)
                    if projection.error is None:
                        return list(projection.documents)
        self._raise_if_cache_only("edinet_documents", on_date.isoformat())
        received = self._http_client("list_documents").fetch_list(on_date)
        with closing(open_connection(sqlite_path)) as connection:
            retain_list_response(
                connection,
                doc_date=on_date,
                received_at=received.received_at,
                body=received.body,
            )
        projection = rebuild_edinet_document_day(sqlite_path, on_date)
        if projection.error is not None:
            raise EDINETProviderError(projection.error)
        return list(projection.documents)

    def bootstrap_cache(self, start: date, end: date) -> dict[str, int]:
        fetched = self._refresh_document_state(end)
        cursor = start
        while cursor <= end:
            if cursor not in fetched:
                fetched[cursor] = len(self.list_documents(cursor))
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
        today = datetime.now(_JST).date()
        today_documents = self.list_documents(today, force_refresh=True)
        cursor = start
        while cursor <= end:
            documents += len(
                today_documents
                if cursor == today
                else self.list_documents(cursor, force_refresh=True)
            )
            days += 1
            cursor += timedelta(days=1)
        return {"days": days, "documents": documents}

    def _refresh_document_state(self, end: date) -> dict[date, int]:
        today = datetime.now(_JST).date()
        today_documents = self.list_documents(today, force_refresh=True)
        fetched = {
            end: len(
                today_documents if end == today else self.list_documents(end, force_refresh=True)
            ),
        }
        if self._sqlite_path is not None:
            from baibai_engine.market.edinet_metrics.store import (
                read_unfinalized_edinet_document_dates,
            )

            for unresolved in read_unfinalized_edinet_document_dates(self._sqlite_path, before=end):
                fetched[unresolved] = len(self.list_documents(unresolved, force_refresh=True))
        return fetched

    def download_csv_zip(self, doc_id: str) -> bytes:
        return self._download_document_zip(doc_id, source_type=5)

    def download_xbrl_zip(self, doc_id: str) -> bytes:
        return self._download_document_zip(doc_id, source_type=1)

    def _download_document_zip(self, doc_id: str, *, source_type: int) -> bytes:
        safe_id = parse_doc_id(doc_id)
        source_name = "csv" if source_type == 5 else "xbrl"
        if self._sqlite_path is not None and self._sqlite_path.exists():
            with closing(connect_read_only(self._sqlite_path)) as connection:
                retained = read_source_archive(connection, safe_id, source_type)
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
        content = (
            self._http_client("download_document_zip").fetch_archive(safe_id, source_type).body
        )
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
        with closing(open_connection(self._sqlite_path)) as connection:
            return retain_source_archive(
                connection,
                doc_id,
                source_type,
                content,
                stored_on=datetime.now(UTC).date(),
            )

    def _require_api_key(self, operation: str) -> str:
        if not self._api_key:
            raise EdinetAcquisitionError(f"EDINET_API_KEY is required for {operation}")
        return self._api_key

    def _raise_if_cache_only(self, source: str, requirement: str) -> None:
        if not self._cache_only:
            return
        sqlite_label = self._sqlite_path.as_posix() if self._sqlite_path is not None else "<none>"
        raise EDINETProviderError(
            f"SQLite cache incomplete for {source} ({requirement}); "
            f"sqlite={sqlite_label}. `screening run` is cache-only: run "
            "`bootstrap-cache --asof` / `refresh-edinet-metrics` or repair SQLite "
            "before running screening."
        )


def _is_zip_bytes(content: bytes) -> bool:
    return zipfile.is_zipfile(io.BytesIO(content))


def _non_zip_response_message(content: bytes) -> str:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "zip_invalid"
    if not isinstance(payload, Mapping):
        return "zip_invalid"
    raw_status = payload.get("StatusCode") or payload.get("statusCode") or payload.get("status")
    status = str(raw_status) if raw_status not in (None, "") else None
    message = text_or_none(payload.get("message")) or text_or_none(payload.get("Message"))
    if status and message:
        return f"StatusCode={status} message={safe_error_value(message)}"
    if status:
        return f"StatusCode={status}"
    return "zip_invalid"
