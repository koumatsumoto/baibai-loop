from __future__ import annotations

import sys
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.foundation.time import JST
from baibai_loop.foundation.yaml_io import safe_load
from baibai_loop.screening.render import (
    RenderError,
    build_output_path,
    render_screened_yaml,
)
from baibai_loop.screening.schema import (
    EvidenceHit,
    ScreenedCandidate,
    ScreenedRunDocument,
    TTMQuality,
    normalize_ticker,
)


class ScreeningRenderTests(unittest.TestCase):
    def test_build_output_path_uses_asof_date(self) -> None:
        self.assertEqual(
            build_output_path(date(2026, 4, 24)),
            Path("records/02-candidates/2026/04/2026-04-24.yaml"),
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
                "min_market_cap_oku": 200,
                "min_avg_turnover_oku": 3,
                "exclude_listed_under_days": 182,
            },
            candidates=[
                ScreenedCandidate(
                    ticker="130A",
                    name="Sample Co",
                    per_forward=8.234,
                    per_trailing=9.876,
                    pbr=0.723,
                    ev_ebitda=4.84,
                    p_s=0.613,
                    pcfr=5.12,
                    sector_33="情報・通信業",
                    evidence_hits=(
                        EvidenceHit(
                            name="valuation-reversion",
                            playbook_id="valuation-reversion",
                            reasons=("price_down_60d_and_valuation_sigma_down",),
                        ),
                    ),
                    ttm_quality={
                        "ev_ebitda": TTMQuality.EXACT,
                        "p_s": TTMQuality.APPROXIMATED,
                        "pcfr": TTMQuality.UNAVAILABLE,
                        "ocf_yield": TTMQuality.UNAVAILABLE,
                        "sales": TTMQuality.APPROXIMATED,
                    },
                )
            ],
            run_at=datetime(2026, 4, 24, 9, 0, tzinfo=JST),
            run_id="screening-20260424",
            provider_status_lines=("データソース: J-Quants Light + EDINET + JPX",),
            universe_exclusion_lines=("除外件数: 42 銘柄",),
            ttm_quality_counts={"exact": 1, "approximated": 1, "unavailable": 1},
            evidence_hits_summary={"valuation-reversion": 1},
            fallback_lines=("欠損件数: 0",),
        )

        rendered = render_screened_yaml(document)

        self.assertIn('run_date: "2026-04-24"', rendered)
        self.assertIn('asof_date: "2026-04-24"', rendered)
        self.assertIn('run_id: "screening-20260424"', rendered)
        self.assertIn('ticker: "130A"', rendered)
        self.assertIn("ttm_quality:", rendered)
        self.assertIn("approximated", rendered)
        self.assertIn("freshness_warnings:", rendered)
        self.assertIn("ttm_quality_counts:", rendered)
        self.assertIn('- "jpx-public-regulation"', rendered)

    def test_render_screened_yaml_matches_canonical_structure(self) -> None:
        document = ScreenedRunDocument(
            run_date=date(2026, 4, 24),
            asof_date=date(2026, 4, 24),
            universe_size=321,
            filters={
                "min_market_cap_oku": 200,
                "min_avg_turnover_oku": 3,
                "exclude_listed_under_days": 182,
            },
            candidates=[
                ScreenedCandidate(
                    ticker="130A",
                    name="...",
                    per_forward=8.2,
                    per_trailing=9.5,
                    pbr=0.72,
                    ev_ebitda=4.8,
                    p_s=0.6,
                    pcfr=5.1,
                    sector_33="業種名",
                    evidence_hits=(
                        EvidenceHit(
                            name="valuation-reversion",
                            playbook_id="valuation-reversion",
                            reasons=(
                                "sector_median_discount_and_self_range_bottom",
                                "price_down_60d_and_valuation_sigma_down",
                                "sector_rotation_short_sell",
                            ),
                            metrics={"price_change_60d": -0.155},
                        ),
                    ),
                    ttm_quality={
                        "ev_ebitda": TTMQuality.EXACT,
                        "p_s": TTMQuality.APPROXIMATED,
                        "pcfr": TTMQuality.UNAVAILABLE,
                        "ocf_yield": TTMQuality.UNAVAILABLE,
                        "sales": TTMQuality.APPROXIMATED,
                    },
                    market_cap_oku=585,
                    avg_turnover_oku=2.3,
                    price_change_20d=-0.072,
                    price_change_60d=-0.155,
                    sector_relative_strength_percentile=0.35,
                    next_earnings_date=date(2026, 5, 13),
                )
            ],
            run_at=datetime(2026, 4, 24, 9, 0, tzinfo=JST),
            run_id="screening-20260424",
            provider_status_lines=(
                "データソース: J-Quants Light（日足・財務サマリー・業績予想）+ JPX",
            ),
            ttm_quality_counts={"exact": 1, "approximated": 1, "unavailable": 1},
            evidence_hits_summary={"valuation-reversion": 1},
            fallback_lines=("取得失敗の有無: [有の場合は対象銘柄と理由を列挙]",),
        )

        rendered = render_screened_yaml(document)

        payload = safe_load(rendered)
        self.assertEqual(payload["run_id"], "screening-20260424")
        candidate = payload["candidates"][0]
        self.assertEqual(candidate["ticker"], "130A")
        self.assertEqual(candidate["sector_33"], "業種名")
        self.assertEqual(candidate["price_change_20d"], -0.072)
        self.assertEqual(candidate["evidence_hits"][0]["playbook_id"], "valuation-reversion")
        self.assertEqual(
            candidate["evidence_hits"][0]["metrics"]["price_change_60d"],
            -0.155,
        )

    def test_render_evidence_keeps_core_playbook_metrics(self) -> None:
        document = ScreenedRunDocument(
            run_date=date(2026, 4, 24),
            asof_date=date(2026, 4, 24),
            universe_size=1,
            filters={},
            candidates=[
                ScreenedCandidate(
                    ticker="130A",
                    name="Sample Co",
                    per_forward=12.0,
                    per_trailing=13.0,
                    pbr=1.1,
                    ev_ebitda=6.0,
                    p_s=0.6,
                    pcfr=5.0,
                    sector_33="情報・通信業",
                    evidence_hits=(
                        EvidenceHit(
                            name="sales-discount-growth",
                            playbook_id="sales-discount-growth",
                            metrics={"ps_sector_gap": -0.6, "sales_yoy": 0.1},
                            reasons=("sales_discount_growth",),
                        ),
                    ),
                    ttm_quality={
                        "ev_ebitda": TTMQuality.EXACT,
                        "p_s": TTMQuality.EXACT,
                        "pcfr": TTMQuality.UNAVAILABLE,
                        "ocf_yield": TTMQuality.UNAVAILABLE,
                        "sales": TTMQuality.EXACT,
                    },
                )
            ],
            run_at=datetime(2026, 4, 24, 9, 0, tzinfo=JST),
            run_id="screening-20260424",
        )

        payload = safe_load(render_screened_yaml(document))

        evidence = payload["candidates"][0]["evidence_hits"][0]
        self.assertEqual(evidence["playbook_id"], "sales-discount-growth")
        self.assertEqual(evidence["metrics"]["sales_yoy"], 0.1)

    def test_render_requires_jst_run_at(self) -> None:
        document = ScreenedRunDocument(
            run_date=date(2026, 4, 24),
            asof_date=date(2026, 4, 24),
            universe_size=0,
            filters={},
            candidates=(),
            run_at=datetime(2026, 4, 24, 0, 0, tzinfo=UTC),
            run_id="screening-20260424",
        )

        with self.assertRaises(RenderError):
            render_screened_yaml(document)
