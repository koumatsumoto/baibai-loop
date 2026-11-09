"""Offline provider doubles for explicit Jobs maintenance and CLI wiring."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakeEDINETProvider:
    zip_by_doc_id: dict[str, bytes] | None = None
    download_calls: list[str] = field(default_factory=list)

    def close(self) -> None:
        pass

    def download_csv_zip(self, doc_id: str) -> bytes:
        self.download_calls.append(doc_id)
        return (self.zip_by_doc_id or {})[doc_id]
