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


def _bars(
    code: str, close: float = 100.0, turnover: float = 100_000_000.0
) -> list[JQuantsDailyBar]:
    # Span 200 days ending at 2026-04-24; the trailing 20 bars feed the
    # turnover / market-cap facts recorded on each snapshot.
    end = date(2026, 4, 24)
    total = 200
    start = end - timedelta(days=total - 1)
    return [
        JQuantsDailyBar(
            ticker=code,
            traded_at=start + timedelta(days=index),
            close=close,
            turnover_value=turnover,
        )
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
        self.assertEqual(result.snapshots["130A"].avg_turnover_oku, 1.0)

    def test_build_universe_keeps_200_oku_band_security(self) -> None:
        security = SecurityMaster(
            code="201A",
            name="Small Cap",
            market_segment="Standard",
            sector_33="情報・通信業",
            is_common_stock=True,
        )
        result = build_universe(
            asof_date=date(2026, 4, 24),
            securities=[security],
            bars_by_ticker={"201A": _bars("201A", close=100.0, turnover=300_000_000.0)},
            shares_outstanding_by_ticker={"201A": 200_000_000.0},
            jpx_flags_by_ticker={},
        )
        self.assertIn("201A", result.snapshots)
        self.assertEqual(result.snapshots["201A"].market_cap_oku, 200)

    def test_build_universe_records_thin_turnover_as_fact(self) -> None:
        security = SecurityMaster(
            code="202A",
            name="Thin Trading",
            market_segment="Standard",
            sector_33="情報・通信業",
            is_common_stock=True,
        )
        result = build_universe(
            asof_date=date(2026, 4, 24),
            securities=[security],
            bars_by_ticker={"202A": _bars("202A", close=100.0, turnover=99_000_000.0)},
            shares_outstanding_by_ticker={"202A": 300_000_000.0},
            jpx_flags_by_ticker={},
        )
        # Liquidity is an analysis-layer parameter: the snapshot keeps the fact
        # instead of excluding the security from the screen scope.
        self.assertIn("202A", result.snapshots)
        self.assertEqual(result.snapshots["202A"].avg_turnover_oku, 1.0)
        self.assertEqual(result.exclusion_counts, {})

    def test_build_universe_records_jpx_flags_as_fact(self) -> None:
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
        # Regulation flags are recorded as facts; the exclusion decision moves
        # to the selection liquidity rules.
        self.assertIn("7203", result.snapshots)
        self.assertEqual(result.snapshots["7203"].jpx_flags, ("整理銘柄",))
        self.assertEqual(result.exclusion_counts, {})

    def test_build_universe_records_listing_span_as_fact(self) -> None:
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
            JQuantsDailyBar(
                ticker="300A",
                traded_at=end - timedelta(days=90 - idx),
                close=100.0,
                turnover_value=300_000_000.0,
            )
            for idx in range(90)
        ]
        result = build_universe(
            asof_date=end,
            securities=[security],
            bars_by_ticker={"300A": bars},
            shares_outstanding_by_ticker={"300A": 400_000_000.0},
            jpx_flags_by_ticker={},
        )
        self.assertIn("300A", result.snapshots)
        self.assertEqual(result.snapshots["300A"].listing_span_days, 90)

    def test_build_universe_excludes_insufficient_bar_history(self) -> None:
        security = SecurityMaster(
            code="400A",
            name="Few Bars",
            market_segment="Growth",
            sector_33="情報・通信業",
            is_common_stock=True,
        )
        end = date(2026, 4, 24)
        bars = [
            JQuantsDailyBar(
                ticker="400A",
                traded_at=end - timedelta(days=10 - idx),
                close=100.0,
                turnover_value=300_000_000.0,
            )
            for idx in range(10)
        ]
        result = build_universe(
            asof_date=end,
            securities=[security],
            bars_by_ticker={"400A": bars},
            shares_outstanding_by_ticker={"400A": 400_000_000.0},
            jpx_flags_by_ticker={},
        )
        self.assertNotIn("400A", result.snapshots)
        self.assertEqual(result.exclusion_counts.get("insufficient_bar_history"), 1)
