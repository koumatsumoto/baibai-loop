"""Provider adapter protocols and the bundle injected into CLI commands."""

from __future__ import annotations

from typing import Protocol

from baibai_engine.data_collection.providers.edinet.archive_native import FixedEdinetArchives


class EDINETMaintenance(Protocol):
    def close(self) -> None: ...

    def download_csv_zip(self, doc_id: str) -> bytes: ...

    def retained_archives(self) -> FixedEdinetArchives: ...
