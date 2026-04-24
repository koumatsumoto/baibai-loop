from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.screening.render import JST, RenderError, build_output_path, render_screened_markdown
from baibai_loop.screening.schema import ScreenedRunDocument, ScreenedTicker, TTMQuality, normalize_ticker


class ScreeningRenderTests(unittest.TestCase):
    def test_build_output_path_uses_asof_date(self) -> None:
        self.assertEqual(
            build_output_path(date(2026, 4, 24)),
            Path("screened/2026/04/2026-04-24.md"),
        )

    def test_normalize_ticker_supports_alpha_numeric_codes(self) -> None:
        self.assertEqual(normalize_ticker("130a"), "130A")
        with self.assertRaises(ValueError):
            normalize_ticker("130AA")

    def test_render_screened_markdown_contains_ttm_quality_block(self) -> None:
        document = ScreenedRunDocument(
            run_date=date(2026, 4, 24),
            asof_date=date(2026, 4, 24),
            universe_size=321,
            filters={
                "min_market_cap_oku": 300,
                "min_avg_turnover_oku": 2,
                "exclude_listed_under_months": 6,
            },
            tickers=[
                ScreenedTicker(
                    ticker="130A",
                    name="Sample Co",
                    per_forward=8.234,
                    per_trailing=9.876,
                    pbr=0.723,
                    ev_ebitda=4.84,
                    p_s=0.613,
                    pcfr=5.12,
                    sector_33="情報・通信業",
                    threshold_hit=("price_down_60d_and_valuation_sigma_down",),
                    ttm_quality={
                        "ev_ebitda": TTMQuality.EXACT,
                        "p_s": TTMQuality.APPROXIMATED,
                        "pcfr": TTMQuality.UNAVAILABLE,
                    },
                )
            ],
            run_at=datetime(2026, 4, 24, 9, 0, tzinfo=JST),
            fact_memo_lines=("複数閾値 hit 銘柄はなし",),
            provider_status_lines=("データソース: J-Quants Light + EDINET + JPX",),
            universe_exclusion_lines=("除外件数: 42 銘柄",),
            ttm_quality_counts={"exact": 1, "approximated": 1, "unavailable": 1},
            fallback_lines=("欠損件数: 0",),
        )

        rendered = render_screened_markdown(document)

        self.assertIn('run_date: "2026-04-24"', rendered)
        self.assertIn('asof_date: "2026-04-24"', rendered)
        self.assertIn('ticker: "130A"', rendered)
        self.assertIn("ttm_quality:", rendered)
        self.assertIn("approximated", rendered)
        self.assertIn("ttm_quality 集計: exact=1, approximated=1, unavailable=1", rendered)
        self.assertIn("## 3. 実行環境", rendered)

    def test_render_requires_jst_run_at(self) -> None:
        document = ScreenedRunDocument(
            run_date=date(2026, 4, 24),
            asof_date=date(2026, 4, 24),
            universe_size=0,
            filters={},
            tickers=(),
            run_at=datetime(2026, 4, 24, 0, 0, tzinfo=timezone.utc),
        )

        with self.assertRaises(RenderError):
            render_screened_markdown(document)
