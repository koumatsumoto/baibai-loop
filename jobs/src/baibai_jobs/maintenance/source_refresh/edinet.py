from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from baibai_engine.data_collection.providers.edinet.archive_native import (
    EdinetArchiveL1,
    FixedEdinetArchives,
    NativeArchive,
)
from baibai_engine.data_collection.providers.edinet.archives import (
    read_source_archive,
    retain_source_archive,
)
from baibai_engine.data_collection.providers.edinet.http import EDINETHTTP
from baibai_engine.data_collection.providers.edinet.models import (
    EdinetAcquisitionError,
    EdinetResponse,
)
from baibai_engine.data_collection.providers.edinet.receipts import (
    read_list_responses,
    retain_list_response,
)
from baibai_engine.machine_analysis.observations.disclosures.edinet_document_events import (
    parse_doc_id,
)
from baibai_engine.machine_analysis.observations.disclosures.edinet_list_rebuild import (
    rebuild_edinet_document_day,
)
from baibai_engine.machine_analysis.observations.financials.edinet_decode import (
    safe_error_value,
    text_or_none,
)
from baibai_engine.machine_analysis.observations.financials.edinet_models import EDINETProviderError
from baibai_engine.storage.objects.r2 import Boto3R2Store
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_engine.storage.sqlite.source_cache.schema import connect_current, open_connection

if TYPE_CHECKING:
    import requests

_JST = ZoneInfo("Asia/Tokyo")


class EDINETRefresh:
    """Acquire EDINET sources and refresh projections in a selected SQLite store."""

    def __init__(
        self,
        api_key: str | None,
        *,
        sqlite_path: Path,
        session: requests.Session | None = None,
    ) -> None:
        self._api_key = api_key
        self._session = session
        self._http: EDINETHTTP | None = None
        self._native_store: Boto3R2Store | None = None
        self._native_fixed: FixedEdinetArchives | None = None
        self._sqlite_path = Path(sqlite_path)

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None
        if self._native_store is not None:
            self._native_store.close()
            self._native_store = None
            self._native_fixed = None

    def retained_archives(self) -> FixedEdinetArchives:
        """Keep extraction on the last input selected or published by this acquisition."""
        if self._native_fixed is None:
            raise ValueError("EDINET native archives have not been selected by acquisition")
        return self._native_fixed

    def _native_archives(self) -> EdinetArchiveL1:
        if self._native_store is None:
            self._native_store = Boto3R2Store(
                bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores"
            )
        owner = EdinetArchiveL1(
            self._native_store, self._sqlite_path.parent / "edinet-archive-work"
        )
        owner.require_clean_work()
        return owner

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
        from baibai_jobs.edinet_cutover import edinet_list_native_enabled

        if edinet_list_native_enabled():
            raise EdinetAcquisitionError(
                "EDINET list writer moved to edinet-list-native; "
                "acquire there before metric refresh or saved extraction"
            )
        sqlite_path = self._sqlite_path
        if not force_refresh and sqlite_path.exists():
            connection = connect_current(sqlite_path)
            if connection is not None:
                with closing(connection):
                    receipts = read_list_responses(connection, doc_date=on_date)
                if receipts:
                    projection = rebuild_edinet_document_day(sqlite_path, on_date)
                    if projection.error is None:
                        return list(projection.documents)
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
        """Refresh the target day, then recheck older days lacking source proof."""
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
        from baibai_engine.machine_analysis.observations.disclosures.edinet_document_store import (
            read_unrechecked_edinet_document_dates,
        )

        for unresolved in read_unrechecked_edinet_document_dates(self._sqlite_path, before=end):
            fetched[unresolved] = len(self.list_documents(unresolved, force_refresh=True))
        return fetched

    def download_csv_zip(self, doc_id: str) -> bytes:
        return self._download_document_zip(doc_id, source_type=5)

    def download_xbrl_zip(self, doc_id: str) -> bytes:
        return self._download_document_zip(doc_id, source_type=1)

    def fetch_archive(self, doc_id: str, source_type: int) -> EdinetResponse:
        """The native acquisition owner retains this actual response before L2 work."""
        return self._http_client("download_document_zip").fetch_archive(doc_id, source_type)

    def _download_document_zip(self, doc_id: str, *, source_type: int) -> bytes:
        safe_id = parse_doc_id(doc_id)
        from baibai_jobs.edinet_cutover import edinet_list_native_enabled

        if edinet_list_native_enabled():
            return self._download_native_zip(safe_id, source_type=source_type)
        source_name = "csv" if source_type == 5 else "xbrl"
        if self._sqlite_path.exists():
            with closing(connect_read_only(self._sqlite_path)) as connection:
                retained = read_source_archive(connection, safe_id, source_type)
            if retained is not None:
                if not _is_zip_bytes(retained):
                    raise EDINETProviderError(f"stored EDINET {source_name} archive is not a ZIP")
                return retained
        received = self._http_client("download_document_zip").fetch_archive(safe_id, source_type)
        content = received.body
        if not _is_zip_bytes(content):
            raise EDINETProviderError(
                f"EDINET {source_name.upper()} ZIP response was not a zip: "
                f"{_non_zip_response_message(content)}"
            )
        return self._retain_document_zip(
            safe_id, source_type, content, stored_on=received.received_at.astimezone(UTC).date()
        )

    def _download_native_zip(self, doc_id: str, *, source_type: int) -> bytes:
        """Explicit metrics/facts/TOB refresh retains L1 before its SQL consumer copy."""
        if source_type not in (1, 5):
            raise ValueError("unsupported EDINET archive type")
        owner = self._native_archives()
        if self._native_fixed is None:
            self._native_fixed = owner.freeze()
        fixed = self._native_fixed
        metadata = fixed.metadata(doc_id, source_type)
        if self._sqlite_path.exists():
            with closing(connect_read_only(self._sqlite_path)) as connection:
                row = connection.execute(
                    "SELECT stored_on,zip_content FROM edinet_source_archives "
                    "WHERE doc_id=? AND source_type=?",
                    (doc_id, source_type),
                ).fetchone()
            if row is not None:
                recorded = date.fromisoformat(row[0])
                if (
                    metadata is None
                    or recorded.isoformat() != row[0]
                    or recorded < metadata.stored_on
                    or len(row[1]) != metadata.size
                    or hashlib.sha256(row[1]).hexdigest() != metadata.sha256
                ):
                    raise ValueError("EDINET local original differs from fixed native archive")
                if recorded != metadata.stored_on:
                    with closing(open_connection(self._sqlite_path)) as connection, connection:
                        connection.execute(
                            "UPDATE edinet_source_archives SET stored_on=? "
                            "WHERE doc_id=? AND source_type=?",
                            (metadata.stored_on.isoformat(), doc_id, source_type),
                        )
                return bytes(row[1])
        if metadata is None:
            received = self._http_client("download_document_zip").fetch_archive(doc_id, source_type)
            if not _is_zip_bytes(received.body):
                raise EDINETProviderError("EDINET archive response is not a ZIP")
            fixed = owner.publish(
                [NativeArchive.from_response(doc_id, 1 if source_type == 1 else 5, received)]
            )
            self._native_fixed = fixed
            content = received.body
            metadata = fixed.metadata(doc_id, source_type)
        else:
            content = fixed.read_archive(doc_id, source_type)
        assert metadata is not None
        return self._retain_document_zip(doc_id, source_type, content, stored_on=metadata.stored_on)

    def _retain_document_zip(
        self, doc_id: str, source_type: int, content: bytes, *, stored_on: date
    ) -> bytes:
        with closing(open_connection(self._sqlite_path)) as connection:
            return retain_source_archive(
                connection,
                doc_id,
                source_type,
                content,
                stored_on=stored_on,
            )

    def _require_api_key(self, operation: str) -> str:
        if not self._api_key:
            raise EdinetAcquisitionError(f"EDINET_API_KEY is required for {operation}")
        return self._api_key


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
