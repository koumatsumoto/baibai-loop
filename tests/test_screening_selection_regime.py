from __future__ import annotations

import unittest
from collections.abc import Mapping
from datetime import date

from baibai_loop.screening.cli import build_parser
from baibai_loop.screening.regime import MarketRegime, MarketRegimeSnapshot
from baibai_loop.screening.rule_config import DEFAULT_RULES_PATH, load_screening_rules
from baibai_loop.screening.selection import (
    build_selection_payload,
    build_selection_sweep_payload,
    candidate_record_from_mapping,
)

_ASOF = date(2026, 5, 29)

# Fast-dislocation eligible under the balanced profile: a 5d price trigger plus
# two fundamental guards from two families (cash_flow + balance_sheet). The
# evidence lane sales-discount-growth ranks low in the configured lane order so
# the fast boost is the only reason this candidate can outrank the calm one.
_FAST_CANDIDATE: Mapping[str, object] = {
    "ticker": "9999",
    "name": "fast oversold",
    "sector_33": "情報・通信業",
    "market_cap_oku": 500,
    "price_change_5d": -0.10,
    "price_change_20d": -0.12,
    "evidence_hits": [{"name": "sales-discount-growth"}],
    "metrics": {"ocf_yield": 0.12, "net_cash_to_market_cap": 0.3},
}

# Same fundamentals as the fast candidate but no price decline: without the
# fast boost the configured lane order decides and valuation-reversion
# outranks sales-discount-growth.
_CALM_CANDIDATE: Mapping[str, object] = {
    "ticker": "1111",
    "name": "calm value",
    "sector_33": "機械",
    "market_cap_oku": 500,
    "price_change_5d": 0.01,
    "price_change_20d": 0.02,
    "evidence_hits": [{"name": "valuation-reversion"}],
    "metrics": {"ocf_yield": 0.12, "net_cash_to_market_cap": 0.3},
}


def _snapshot(regime: MarketRegime) -> MarketRegimeSnapshot:
    return MarketRegimeSnapshot(
        asof=_ASOF,
        benchmark_ticker="1321",
        eval_date=_ASOF,
        benchmark_return_20d=0.05,
        benchmark_return_60d=0.08,
        breadth_pct_above_ma20=0.62,
        breadth_sample_size=1500,
        regime=regime,
    )


class SelectionRegimeLensTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = load_screening_rules(DEFAULT_RULES_PATH)
        self.candidates = (
            candidate_record_from_mapping(_FAST_CANDIDATE),
            candidate_record_from_mapping(_CALM_CANDIDATE),
        )

    def _payload(self, market_regime: MarketRegimeSnapshot | None) -> dict[str, object]:
        return build_selection_payload(
            asof_date=_ASOF,
            candidates=self.candidates,
            macro_context=None,
            rules=self.rules,
            top=10,
            profile="balanced",
            candidates_ref="test.yaml",
            macro_context_ref=None,
            market_regime=market_regime,
        )

    def _recommended_tickers(self, payload: dict[str, object]) -> list[str]:
        recommendations = payload["recommendations"]
        assert isinstance(recommendations, list)
        return [item["ticker"] for item in recommendations]

    def _diagnostics(self, payload: dict[str, object]) -> Mapping[str, object]:
        selection = payload["selection"]
        assert isinstance(selection, Mapping)
        diagnostics = selection["diagnostics"]
        assert isinstance(diagnostics, Mapping)
        return diagnostics

    def test_without_regime_fast_candidate_keeps_boost(self) -> None:
        payload = self._payload(None)
        self.assertEqual(self._recommended_tickers(payload), ["9999", "1111"])
        diagnostics = self._diagnostics(payload)
        self.assertEqual(diagnostics["fast_dislocation_boost"], "active")
        self.assertIsNone(diagnostics["market_regime"])

    def test_neutral_regime_keeps_existing_ranking(self) -> None:
        payload = self._payload(_snapshot(MarketRegime.NEUTRAL_RANGE))
        self.assertEqual(self._recommended_tickers(payload), ["9999", "1111"])
        self.assertEqual(self._diagnostics(payload)["fast_dislocation_boost"], "active")

    def test_selloff_regime_keeps_existing_ranking(self) -> None:
        payload = self._payload(_snapshot(MarketRegime.RISK_OFF_SELLOFF))
        self.assertEqual(self._recommended_tickers(payload), ["9999", "1111"])

    def test_rally_regime_neutralizes_fast_boost(self) -> None:
        payload = self._payload(_snapshot(MarketRegime.RISK_ON_RALLY))
        self.assertEqual(self._recommended_tickers(payload), ["1111", "9999"])
        diagnostics = self._diagnostics(payload)
        self.assertEqual(diagnostics["fast_dislocation_boost"], "neutralized")
        warnings = diagnostics["warnings"]
        assert isinstance(warnings, list)
        self.assertIn("fast_dislocation_boost_neutralized_risk_on_rally", warnings)
        market_regime = diagnostics["market_regime"]
        assert isinstance(market_regime, Mapping)
        self.assertEqual(market_regime["regime"], "risk_on_rally")

    def test_rally_regime_keeps_candidates_instead_of_gating(self) -> None:
        payload = self._payload(_snapshot(MarketRegime.RISK_ON_RALLY))
        self.assertEqual(len(self._recommended_tickers(payload)), 2)

    def _recommendations(self, payload: dict[str, object]) -> list[Mapping[str, object]]:
        recommendations = payload["recommendations"]
        assert isinstance(recommendations, list)
        return recommendations

    def test_snapshot_supplies_benchmark_relative_20d_and_laggard_tag(self) -> None:
        payload = self._payload(_snapshot(MarketRegime.NEUTRAL_RANGE))
        by_ticker = {item["ticker"]: item for item in self._recommendations(payload)}
        fast = by_ticker["9999"]
        self.assertAlmostEqual(float(str(fast["benchmark_relative_20d"])), -0.17)
        self.assertIn("benchmark_laggard_20d", fast["risk_tags"])
        calm = by_ticker["1111"]
        self.assertAlmostEqual(float(str(calm["benchmark_relative_20d"])), -0.03)
        self.assertIn("benchmark_laggard_20d", calm["risk_tags"])

    def test_without_snapshot_benchmark_relative_20d_degrades_to_none(self) -> None:
        payload = self._payload(None)
        for item in self._recommendations(payload):
            self.assertIsNone(item["benchmark_relative_20d"])
            self.assertNotIn("benchmark_laggard_20d", item["risk_tags"])

    def test_price_history_gap_tag_marks_old_listing_with_sparse_bars(self) -> None:
        gappy = dict(_CALM_CANDIDATE)
        gappy["listing_span_days"] = 1200
        gappy["price_history_coverage_750d"] = 0.27
        fresh = dict(_FAST_CANDIDATE)
        fresh["listing_span_days"] = 300
        fresh["price_history_coverage_750d"] = 0.27
        payload = build_selection_payload(
            asof_date=_ASOF,
            candidates=(
                candidate_record_from_mapping(gappy),
                candidate_record_from_mapping(fresh),
            ),
            macro_context=None,
            rules=self.rules,
            top=10,
            profile="balanced",
            candidates_ref="test.yaml",
            macro_context_ref=None,
            market_regime=None,
        )
        by_ticker = {item["ticker"]: item for item in self._recommendations(payload)}
        self.assertIn("price_history_gap", by_ticker["1111"]["risk_tags"])
        # A genuinely new listing is short_history territory, not a gap.
        self.assertNotIn("price_history_gap", by_ticker["9999"]["risk_tags"])

    def test_sweep_payload_records_market_regime(self) -> None:
        payload = build_selection_sweep_payload(
            asof_date=_ASOF,
            candidates=self.candidates,
            macro_context=None,
            rules=self.rules,
            top=10,
            profiles=("balanced",),
            candidates_ref="test.yaml",
            macro_context_ref=None,
            market_regime=_snapshot(MarketRegime.RISK_ON_RALLY),
        )
        market_regime = payload["market_regime"]
        assert isinstance(market_regime, Mapping)
        self.assertEqual(market_regime["regime"], "risk_on_rally")


class RegimeLensCliArgumentTests(unittest.TestCase):
    def test_select_parser_defaults_enable_regime_lens(self) -> None:
        args = build_parser().parse_args(["select", "--asof", "2026-05-29"])
        self.assertFalse(args.no_regime_lens)
        self.assertTrue(args.sqlite_path.endswith("market.sqlite"))

    def test_select_parser_accepts_no_regime_lens_and_sqlite_path(self) -> None:
        args = build_parser().parse_args(
            ["select", "--asof", "2026-05-29", "--no-regime-lens", "--sqlite-path", "x.sqlite"]
        )
        self.assertTrue(args.no_regime_lens)
        self.assertEqual(args.sqlite_path, "x.sqlite")


if __name__ == "__main__":
    unittest.main()
