from __future__ import annotations

import sys
import textwrap
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.screening.render import (
    JST,
    RenderError,
    build_output_path,
    render_screened_yaml,
)
from baibai_loop.screening.schema import (
    ScreenedRunDocument,
    ScreenedTicker,
    TTMQuality,
    normalize_ticker,
)


class ScreeningRenderTests(unittest.TestCase):
    def test_build_output_path_uses_asof_date(self) -> None:
        self.assertEqual(
            build_output_path(date(2026, 4, 24)),
            Path("screened/2026/04/2026-04-24.yaml"),
        )

    def test_normalize_ticker_supports_alpha_numeric_codes(self) -> None:
        self.assertEqual(normalize_ticker("130a"), "130A")
        with self.assertRaises(ValueError):
            normalize_ticker("130AA")

    def test_render_screened_yaml_contains_ttm_quality_block(self) -> None:
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

        rendered = render_screened_yaml(document)

        self.assertIn('run_date: "2026-04-24"', rendered)
        self.assertIn('asof_date: "2026-04-24"', rendered)
        self.assertIn('ticker: "130A"', rendered)
        self.assertIn("ttm_quality:", rendered)
        self.assertIn("approximated", rendered)
        self.assertIn("ttm_quality_counts:", rendered)
        self.assertIn("fact_memo_lines:", rendered)
        self.assertIn('- "jpx-public-regulation"', rendered)

    def test_render_screened_yaml_matches_canonical_structure(self) -> None:
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
                    name="...",
                    per_forward=8.2,
                    per_trailing=9.5,
                    pbr=0.72,
                    ev_ebitda=4.8,
                    p_s=0.6,
                    pcfr=5.1,
                    sector_33="業種名",
                    threshold_hit=(
                        "sector_median_under_20pct_and_self_range_bottom_20pct",
                        "price_down_60d_and_valuation_sigma_down",
                        "sector_rotation_short_sell",
                    ),
                    ttm_quality={
                        "ev_ebitda": TTMQuality.EXACT,
                        "p_s": TTMQuality.APPROXIMATED,
                        "pcfr": TTMQuality.UNAVAILABLE,
                    },
                    market_cap_oku=585,
                    avg_turnover_oku=2.3,
                    price_change_60d=-0.155,
                    price_change_4w=-0.072,
                    sector_relative_strength_percentile=0.35,
                    metrics_breakdown={
                        "per_trailing": {
                            "sector_median_gap": -0.21,
                            "self_range_percentile": 0.14,
                            "sigma_gap": -1.4,
                        },
                        "pbr": {
                            "sector_median_gap": -0.18,
                            "self_range_percentile": 0.20,
                            "sigma_gap": -1.1,
                        },
                        "ev_ebitda": {
                            "sector_median_gap": None,
                            "self_range_percentile": None,
                            "sigma_gap": None,
                        },
                    },
                    next_earnings_date=date(2026, 5, 13),
                )
            ],
            run_at=datetime(2026, 4, 24, 9, 0, tzinfo=JST),
            fact_memo_lines=("[事実 1]", "[事実 2]"),
            provider_status_lines=(
                "データソース: J-Quants Light（日足・財務サマリー・業績予想）+ EDINET + JPX",
            ),
            ttm_quality_counts={"exact": 1, "approximated": 1, "unavailable": 1},
            fallback_lines=("取得失敗の有無: [有の場合は対象銘柄と理由を列挙]",),
        )

        rendered = render_screened_yaml(document)

        expected = textwrap.dedent(
            """\
            run_date: "2026-04-24"
            asof_date: "2026-04-24"
            universe_size: 321
            filters:
              min_market_cap_oku: 300
              min_avg_turnover_oku: 2
              exclude_listed_under_months: 6
            generated_by: "screening-cli-v1"
            data_sources:
            - "j-quants-light"
            - "edinet-api-v2@2026-01-29"
            - "jpx-public-regulation"
            run_at: "2026-04-24T09:00:00+09:00"
            tickers:
            - ticker: "130A"
              name: "..."
              per_forward: 8.2
              per_trailing: 9.5
              pbr: 0.72
              ev_ebitda: 4.8
              p_s: 0.6
              pcfr: 5.1
              sector_33: "業種名"
              market_cap_oku: 585
              avg_turnover_oku: 2.3
              price_change_60d: -0.155
              price_change_4w: -0.072
              sector_relative_strength_percentile: 0.35
              metrics_breakdown:
                per_trailing:
                  sector_median_gap: -0.21
                  self_range_percentile: 0.14
                  sigma_gap: -1.4
                pbr:
                  sector_median_gap: -0.18
                  self_range_percentile: 0.2
                  sigma_gap: -1.1
                ev_ebitda:
                  sector_median_gap: null
                  self_range_percentile: null
                  sigma_gap: null
              next_earnings_date: "2026-05-13"
              ttm_quality:
                ev_ebitda: exact
                p_s: approximated
                pcfr: unavailable
              threshold_hit:
              - sector_median_under_20pct_and_self_range_bottom_20pct
              - price_down_60d_and_valuation_sigma_down
              - sector_rotation_short_sell
            fact_memo_lines:
            - "[事実 1]"
            - "[事実 2]"
            provider_status_lines:
            - "データソース: J-Quants Light（日足・財務サマリー・業績予想）+ EDINET + JPX"
            universe_exclusion_lines: []
            ttm_quality_counts:
              exact: 1
              approximated: 1
              unavailable: 1
            fallback_lines:
            - "取得失敗の有無: [有の場合は対象銘柄と理由を列挙]"
            """
        ).rstrip()
        self.assertEqual(rendered.rstrip(), expected)

    def test_render_requires_jst_run_at(self) -> None:
        document = ScreenedRunDocument(
            run_date=date(2026, 4, 24),
            asof_date=date(2026, 4, 24),
            universe_size=0,
            filters={},
            tickers=(),
            run_at=datetime(2026, 4, 24, 0, 0, tzinfo=UTC),
        )

        with self.assertRaises(RenderError):
            render_screened_yaml(document)
