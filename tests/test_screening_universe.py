from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.screening.providers.jquants import JQuantsDailyBar
from baibai_loop.screening.schema import SecurityMaster
from baibai_loop.screening.universe import build_universe


def _bars(code: str, close: float = 100.0, turnover: float = 300_000_000.0) -> list[JQuantsDailyBar]:
    # Span 200 days ending at 2026-04-24 so listing_span >= LISTED_UNDER_DAYS (182).
    # Only the trailing 20 bars populate turnover/close for universe filters.
    end = date(2026, 4, 24)
    total = 200
    start = end - timedelta(days=total - 1)
    return [
        JQuantsDailyBar(ticker=code, traded_at=start + timedelta(days=index), close=close, turnover_value=turnover)
        for index in range(total)
    ]


class ScreeningUniverseTests(unittest.TestCase):
    def test_build_universe_keeps_eligible_security(self) -> None:
        security = SecurityMaster(
            code="130A",
            name="Sample",
            market_segment="Prime",
            sector_33="情報・通信業",
            is_common_stock=True,
        )
        result = build_universe(
            asof_date=date(2026, 4, 24),
            securities=[security],
            bars_by_ticker={"130A": _bars("130A")},
            shares_outstanding_by_ticker={"130A": 400_000_000.0},
            jpx_flags_by_ticker={},
        )
        self.assertIn("130A", result.snapshots)
        self.assertEqual(result.snapshots["130A"].market_cap_oku, 400)
        self.assertEqual(result.snapshots["130A"].avg_turnover_oku, 3.0)

    def test_build_universe_excludes_jpx_regulated_security(self) -> None:
        security = SecurityMaster(
            code="7203",
            name="Sample",
            market_segment="Prime",
            sector_33="輸送用機器",
            is_common_stock=True,
        )
        result = build_universe(
            asof_date=date(2026, 4, 24),
            securities=[security],
            bars_by_ticker={"7203": _bars("7203")},
            shares_outstanding_by_ticker={"7203": 400_000_000.0},
            jpx_flags_by_ticker={"7203": ("整理銘柄",)},
        )
        self.assertNotIn("7203", result.snapshots)
        self.assertEqual(result.exclusion_counts["jpx_regulation"], 1)

    def test_build_universe_flags_recent_listing_as_under_6_months(self) -> None:
        security = SecurityMaster(
            code="300A",
            name="Newly Listed",
            market_segment="Growth",
            sector_33="情報・通信業",
            is_common_stock=True,
        )
        end = date(2026, 4, 24)
        # 90 days of bars starting ~90 days before asof; listing_span < 182 threshold.
        bars = [
            JQuantsDailyBar(ticker="300A", traded_at=end - timedelta(days=90 - idx), close=100.0, turnover_value=300_000_000.0)
            for idx in range(90)
        ]
        result = build_universe(
            asof_date=end,
            securities=[security],
            bars_by_ticker={"300A": bars},
            shares_outstanding_by_ticker={"300A": 400_000_000.0},
            jpx_flags_by_ticker={},
        )
        self.assertNotIn("300A", result.snapshots)
        self.assertEqual(result.exclusion_counts.get("listed_under_6_months"), 1)

