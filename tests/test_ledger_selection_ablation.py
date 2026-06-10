from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import yaml

from baibai_loop.ledger.screening_replay import WeekSpec
from baibai_loop.ledger.selection_ablation import (
    DEFAULT_VARIANTS,
    FULL_VARIANT,
    run_selection_ablation,
)
from baibai_loop.screening.rule_config import DEFAULT_RULES_PATH, load_screening_rules
from baibai_loop.screening.selection import (
    RankingToggles,
    build_selection_payload,
    candidate_record_from_mapping,
)
from baibai_loop.screening.sqlite_cache import open_connection

_ASOF = date(2026, 5, 1)

# Fast-eligible under balanced (5d price trigger + two guard families) on the
# lowest-ranked lane, versus a calm candidate with identical fundamentals on
# the highest-ranked lane: the fast boost is the only reason the fast one wins.
_FAST_CANDIDATE = {
    "ticker": "9999",
    "name": "fast oversold",
    "sector_33": "情報・通信業",
    "market_cap_oku": 500,
    "price_change_5d": -0.10,
    "price_change_20d": -0.12,
    "evidence_hits": [{"name": "sales-discount-growth"}],
    "metrics": {"ocf_yield": 0.12, "net_cash_to_market_cap": 0.3},
}

_CALM_CANDIDATE = {
    "ticker": "1111",
    "name": "calm value",
    "sector_33": "機械",
    "market_cap_oku": 500,
    "price_change_5d": 0.01,
    "price_change_20d": 0.02,
    "evidence_hits": [{"name": "valuation-reversion"}],
    "metrics": {"ocf_yield": 0.12, "net_cash_to_market_cap": 0.3},
}


class RankingTogglesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = load_screening_rules(DEFAULT_RULES_PATH)
        self.candidates = (
            candidate_record_from_mapping(_FAST_CANDIDATE),
            candidate_record_from_mapping(_CALM_CANDIDATE),
        )

    def _tickers(self, ranking_toggles: RankingToggles | None) -> list[str]:
        payload = build_selection_payload(
            asof_date=_ASOF,
            candidates=self.candidates,
            macro_context=None,
            rules=self.rules,
            top=10,
            profile="balanced",
            candidates_ref="test.yaml",
            macro_context_ref=None,
            ranking_toggles=ranking_toggles,
        )
        recommendations = payload["recommendations"]
        assert isinstance(recommendations, list)
        return [item["ticker"] for item in recommendations]

    def test_default_toggles_match_no_toggles(self) -> None:
        self.assertEqual(self._tickers(None), self._tickers(RankingToggles()))

    def test_all_on_keeps_fast_boost_first(self) -> None:
        self.assertEqual(self._tickers(RankingToggles()), ["9999", "1111"])

    def test_fast_boost_off_falls_back_to_lane_rank(self) -> None:
        self.assertEqual(self._tickers(RankingToggles(fast_boost=False)), ["1111", "9999"])

    def test_lane_rank_off_keeps_fast_boost_dominant(self) -> None:
        self.assertEqual(self._tickers(RankingToggles(lane_rank=False)), ["9999", "1111"])


def _insert_bars(sqlite_path: Path, ticker: str, *, entry: float, weekly_growth: float) -> None:
    conn = open_connection(sqlite_path)
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO jquants_daily_bars(ticker, traded_at, close, adjustment_close)"
            " VALUES (?, ?, ?, ?)",
            [
                (
                    ticker,
                    (_ASOF + timedelta(days=week * 7)).isoformat(),
                    entry * (1 + weekly_growth) ** week,
                    entry * (1 + weekly_growth) ** week,
                )
                for week in range(6)
            ],
        )
        conn.commit()
    finally:
        conn.close()


class RunSelectionAblationTests(unittest.TestCase):
    def test_variants_cover_features_and_score_forward_returns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            candidates_root = root / "candidates"
            candidates_path = candidates_root / "2026" / "05" / "2026-05-01.yaml"
            candidates_path.parent.mkdir(parents=True)
            candidates_path.write_text(
                yaml.safe_dump(
                    {"candidates": [_FAST_CANDIDATE, _CALM_CANDIDATE]},
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            sqlite_path = root / "market.sqlite"
            _insert_bars(sqlite_path, "1321", entry=100.0, weekly_growth=0.01)
            _insert_bars(sqlite_path, "9999", entry=50.0, weekly_growth=-0.02)
            _insert_bars(sqlite_path, "1111", entry=80.0, weekly_growth=0.05)

            result = run_selection_ablation(
                [WeekSpec(asof=_ASOF, candidates_path=candidates_path)],
                rules=load_screening_rules(DEFAULT_RULES_PATH),
                sqlite_path=sqlite_path,
                candidates_root=candidates_root,
                ledger_root=root / "records",
                top=1,
                horizon_weeks=(1,),
            )

            by_variant = {item.variant: item for item in result.week_results}
            self.assertEqual(set(by_variant), {variant.name for variant in DEFAULT_VARIANTS})
            # full keeps the fast boost so the oversold loser leads; ablating the
            # boost (or dropping its lane) switches to the calm winner.
            self.assertEqual(by_variant[FULL_VARIANT].recommended_tickers, ("9999",))
            self.assertEqual(by_variant["no_fast_boost"].recommended_tickers, ("1111",))
            self.assertEqual(
                by_variant["drop_lane:sales-discount-growth"].recommended_tickers, ("1111",)
            )
            self.assertEqual(by_variant["no_fast_boost"].overlap_with_full, 0.0)
            self.assertEqual(by_variant[FULL_VARIANT].overlap_with_full, 1.0)

            aggregates = {(item.variant, item.horizon_weeks): item for item in result.aggregates}
            full = aggregates[(FULL_VARIANT, 1)]
            no_fast = aggregates[("no_fast_boost", 1)]
            assert full.mean_relative is not None
            assert no_fast.mean_relative is not None
            self.assertLess(full.mean_relative, 0)
            self.assertGreater(no_fast.mean_relative, 0)


if __name__ == "__main__":
    unittest.main()
