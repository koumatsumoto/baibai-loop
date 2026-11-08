"""Offline provider doubles for explicit Jobs maintenance and CLI wiring."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from baibai_engine.machine_analysis.observations.disclosures.jpx_models import (
    JPXEarningsCalendarEntry,
    JPXEarningsCalendarSnapshot,
    JPXRegulationSnapshot,
)


@dataclass
class FakeEDINETProvider:
    zip_by_doc_id: dict[str, bytes] | None = None
    download_calls: list[str] = field(default_factory=list)

    def close(self) -> None:
        pass

    def download_csv_zip(self, doc_id: str) -> bytes:
        self.download_calls.append(doc_id)
        return (self.zip_by_doc_id or {})[doc_id]


@dataclass
class FakeJPXProvider:
    source_names: tuple[str, ...] = (
        "上場廃止警告",
        "取引停止",
        "整理銘柄",
        "特別注意銘柄",
    )
    earnings_requests: list[date] = field(default_factory=list)
    regulation_requests: list[date] = field(default_factory=list)
    close_count: int = 0

    def refresh_earnings_calendar_snapshot(self, asof_date: date) -> JPXEarningsCalendarSnapshot:
        self.earnings_requests.append(asof_date)
        return JPXEarningsCalendarSnapshot(
            entries=(
                JPXEarningsCalendarEntry(
                    ticker="130A", announcement_date=asof_date + timedelta(days=7)
                ),
            ),
            source_urls=("https://www.jpx.co.jp/test/kessan.xlsx",),
            raw_record_count=1,
            excluded_record_count=0,
            superseded_record_count=None,
        )

    def refresh_regulation_snapshot(self, asof_date: date) -> JPXRegulationSnapshot:
        self.regulation_requests.append(asof_date)
        return JPXRegulationSnapshot(flags_by_ticker={}, source_names=self.source_names)

    def close(self) -> None:
        self.close_count += 1
