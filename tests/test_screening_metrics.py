from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.screening.metrics import build_metrics
from baibai_loop.screening.providers.jquants import JQuantsDailyBar, JQuantsFinancialSummary
from baibai_loop.screening.schema import SecurityMaster


def _daily_bars(code: str, end: date, total_days: int) -> list[JQuantsDailyBar]:
    start = end - timedelta(days=total_days - 1)
    return [
        JQuantsDailyBar(
            ticker=code,
            traded_at=start + timedelta(days=index),
            close=100.0 + index,
            turnover_value=300_000_000.0,
        )
        for index in range(total_days)
    ]


def _summary(code: str, disclosed_at: date) -> JQuantsFinancialSummary:
    return JQuantsFinancialSummary(
        ticker=code,
        disclosed_at=disclosed_at,
        forecast_eps=20.0,
        eps_ttm=18.0,
        bps=120.0,
        shares_outstanding=400_000_000.0,
        sales=1_000_000_000.0,
        operating_profit=100_000_000.0,
        ordinary_profit=None,
        profit=None,
    )


class ScreeningMetricsTests(unittest.TestCase):
    def test_build_metrics_excludes_future_bars_from_history(self) -> None:
        """look-ahead bias regression guard: bars after asof must not influence derived metrics."""
        asof = date(2026, 4, 24)
        security = SecurityMaster(
            code="130A",
            name="Alpha",
            market_segment="Prime",
            sector_33="情報・通信業",
            is_common_stock=True,
        )
        base_bars = _daily_bars("130A", asof, 400)
        future_bars = [
            JQuantsDailyBar(
                ticker="130A",
                traded_at=asof + timedelta(days=index + 1),
                close=99999.0,
                turnover_value=300_000_000.0,
            )
            for index in range(20)
        ]
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": security},
            bars_by_ticker={"130A": base_bars + future_bars},
            summaries_by_ticker={"130A": [_summary("130A", asof - timedelta(days=30))]},
            edinet_by_ticker={},
        )
        self.assertIn("130A", result.financials)
        # latest close picked from bars <= asof, not from the polluted 99999.0 future bars.
        per_trailing = result.financials["130A"].per_trailing
        self.assertIsNotNone(per_trailing)
        assert per_trailing is not None
        self.assertAlmostEqual(per_trailing, (100.0 + 399) / 18.0, places=5)
        # short_history_flag must be derived from bars <= asof only. 400d history < 750d ⇒ True.
        self.assertTrue(result.derived["130A"].short_history_flag)

    def test_build_metrics_uses_bars_span_for_short_history_when_established(self) -> None:
        """An established listing (800 days of bars) must not be flagged as short_history."""
        asof = date(2026, 4, 24)
        security = SecurityMaster(
            code="130A",
            name="Alpha",
            market_segment="Prime",
            sector_33="情報・通信業",
            is_common_stock=True,
        )
        bars = _daily_bars("130A", asof, 800)
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": security},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": [_summary("130A", asof - timedelta(days=30))]},
            edinet_by_ticker={},
        )
        self.assertFalse(result.derived["130A"].short_history_flag)


if __name__ == "__main__":
    unittest.main()
