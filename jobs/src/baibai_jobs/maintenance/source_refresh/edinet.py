from __future__ import annotations

import hashlib
import io
import os
import zipfile
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from baibai_engine.data_collection.providers.edinet.archive_native import (
    EdinetArchiveL1,
    FixedEdinetArchives,
    NativeArchive,
)
from baibai_engine.data_collection.providers.edinet.archives import (
    retain_source_archive,
)
from baibai_engine.data_collection.providers.edinet.http import EDINETHTTP
from baibai_engine.data_collection.providers.edinet.models import (
    EdinetAcquisitionError,
    EdinetResponse,
)
from baibai_engine.machine_analysis.observations.disclosures.edinet_document_events import (
    parse_doc_id,
)
from baibai_engine.machine_analysis.observations.financials.edinet_models import EDINETProviderError
from baibai_engine.storage.objects.r2 import Boto3R2Store
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_engine.storage.sqlite.source_cache.schema import open_connection

if TYPE_CHECKING:
    import requests


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

    def download_csv_zip(self, doc_id: str) -> bytes:
        return self._download_native_zip(parse_doc_id(doc_id), source_type=5)

    def fetch_archive(self, doc_id: str, source_type: int) -> EdinetResponse:
        """The native acquisition owner retains this actual response before L2 work."""
        return self._http_client("download_document_zip").fetch_archive(doc_id, source_type)

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
