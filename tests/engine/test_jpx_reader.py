from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from baibai_engine.market.jpx_models import (
    JPXEarningsCalendarEntry,
    JPXEarningsCalendarSnapshot,
    JPXProviderError,
)
from baibai_engine.market.jpx_reader import JPXReader
from baibai_engine.storage.observations.ingest.jpx import (
    store_jpx_earnings_calendar_snapshot,
    store_jpx_regulations,
)


def test_reader_returns_saved_snapshots_without_acquisition(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    asof = date(2026, 5, 8)
    store_jpx_regulations(
        sqlite_path,
        asof,
        flags_by_ticker={"1301": ("取引停止",)},
        source_names=("取引停止",),
        fetched_at_utc="2026-05-08T00:00:00+00:00",
    )
    store_jpx_earnings_calendar_snapshot(
        sqlite_path,
        JPXEarningsCalendarSnapshot(
            entries=(JPXEarningsCalendarEntry("1301", date(2026, 6, 1)),),
            source_urls=("https://www.jpx.co.jp/test/kessan.xlsx",),
            raw_record_count=1,
            excluded_record_count=0,
            superseded_record_count=0,
        ),
        fetched_at_utc="2026-05-08T00:00:00+00:00",
    )

    with patch("requests.Session", side_effect=AssertionError("HTTP session constructed")):
        reader = JPXReader(sqlite_path)
        assert reader.get_regulation_snapshot(asof).flags_by_ticker == {"1301": ("取引停止",)}
        assert reader.get_earnings_calendar_snapshot(asof).entries == (
            JPXEarningsCalendarEntry("1301", date(2026, 6, 1)),
        )
        with pytest.raises(JPXProviderError, match="stored JPX earnings calendar is unavailable"):
            reader.get_earnings_calendar_snapshot(date(2026, 5, 22))
        assert (
            JPXReader(sqlite_path, allow_stale_snapshot=True)
            .get_earnings_calendar_snapshot(date(2026, 5, 22))
            .entries[0]
            .ticker
            == "1301"
        )


def test_reader_reports_missing_saved_snapshots(tmp_path: Path) -> None:
    reader = JPXReader(tmp_path / "missing.sqlite")
    with pytest.raises(JPXProviderError, match="stored JPX regulation snapshot is unavailable"):
        reader.get_regulation_snapshot(date(2026, 5, 8))
    with pytest.raises(JPXProviderError, match="stored JPX earnings calendar is unavailable"):
        reader.get_earnings_calendar_snapshot(date(2026, 5, 8))
