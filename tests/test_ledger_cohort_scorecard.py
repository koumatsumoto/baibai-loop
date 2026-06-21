from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import yaml

from baibai_loop.ledger.cohort_scorecard import (
    LaneDecision,
    _bootstrap_mean_ci,
    _decide,
    run_lane_scorecard,
    scorecard_to_payload,
)
from baibai_loop.ledger.lane_cohorts import ALL_CANDIDATES_COHORT
from baibai_loop.ledger.weeks import discover_week_specs
from baibai_loop.screening.sqlite_cache import open_connection

_ASOF = date(2026, 5, 1)
_BENCHMARK = "1321"


def _write_candidates(path: Path, candidates: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"candidates": candidates}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _insert_entry_and_forward(
    sqlite_path: Path,
    ticker: str,
    *,
    return_ratio: float,
    horizon_weeks: int = 4,
) -> None:
    entry = 100.0
    forward = entry * (1 + return_ratio)
    target = _ASOF + timedelta(days=horizon_weeks * 7)
    conn = open_connection(sqlite_path)
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO jquants_daily_bars(ticker, traded_at, close, adjustment_close)"
            " VALUES (?, ?, ?, ?)",
            [
                (ticker, _ASOF.isoformat(), entry, entry),
                (ticker, target.isoformat(), forward, forward),
            ],
        )
        conn.commit()
    finally:
        conn.close()


class BootstrapCiTests(unittest.TestCase):
    def test_bootstrap_ci_is_deterministic_for_same_seed(self) -> None:
        sample = [0.01, -0.02, 0.03, 0.04, -0.01, 0.02]
        first = _bootstrap_mean_ci(sample, iterations=500, seed=7)
        second = _bootstrap_mean_ci(sample, iterations=500, seed=7)
        self.assertEqual(first, second)

    def test_bootstrap_ci_is_independent_of_sample_order(self) -> None:
        sample = [0.01, -0.02, 0.03, 0.04, -0.01, 0.02]
        shuffled = [0.02, 0.04, -0.02, 0.03, 0.01, -0.01]
        self.assertEqual(
            _bootstrap_mean_ci(sample, iterations=500, seed=7),
            _bootstrap_mean_ci(shuffled, iterations=500, seed=7),
        )

    def test_bootstrap_ci_for_all_positive_sample_excludes_zero(self) -> None:
        ci_low, ci_high, prob_negative = _bootstrap_mean_ci(
            [0.04, 0.05, 0.06, 0.05], iterations=1000, seed=3
        )
        self.assertGreater(ci_low, 0.0)
        self.assertGreaterEqual(ci_high, ci_low)
        self.assertEqual(prob_negative, 0.0)

    def test_bootstrap_ci_for_empty_sample_returns_zeros(self) -> None:
        self.assertEqual(_bootstrap_mean_ci([], iterations=100, seed=1), (0.0, 0.0, 0.0))


class DecideTests(unittest.TestCase):
    def test_all_candidates_lane_is_baseline(self) -> None:
        decision, _ = _decide(
            lane=ALL_CANDIDATES_COHORT,
            resolved=500,
            ci_low=-0.01,
            ci_high=0.01,
            baseline_mean=0.0,
            min_resolved=30,
        )
        self.assertIs(decision, LaneDecision.BASELINE)

    def test_underpowered_lane_is_review(self) -> None:
        decision, reason = _decide(
            lane="cash-rich-asset-discount",
            resolved=10,
            ci_low=0.05,
            ci_high=0.09,
            baseline_mean=0.0,
            min_resolved=30,
        )
        self.assertIs(decision, LaneDecision.REVIEW)
        self.assertIn("underpowered", reason)

    def test_ci_above_baseline_is_keep(self) -> None:
        decision, _ = _decide(
            lane="cash-rich-asset-discount",
            resolved=300,
            ci_low=-0.082,
            ci_high=-0.057,
            baseline_mean=-0.091,
            min_resolved=30,
        )
        self.assertIs(decision, LaneDecision.KEEP)

    def test_ci_below_baseline_is_kill_candidate(self) -> None:
        decision, _ = _decide(
            lane="weak-lane",
            resolved=300,
            ci_low=-0.12,
            ci_high=-0.10,
            baseline_mean=-0.05,
            min_resolved=30,
        )
        self.assertIs(decision, LaneDecision.KILL_CANDIDATE)

    def test_ci_spanning_baseline_is_review(self) -> None:
        decision, _ = _decide(
            lane="valuation-reversion",
            resolved=300,
            ci_low=-0.11,
            ci_high=-0.08,
            baseline_mean=-0.091,
            min_resolved=30,
        )
        self.assertIs(decision, LaneDecision.REVIEW)


class RunScorecardTests(unittest.TestCase):
    def _build_fixture(self, root: Path) -> Path:
        sqlite_path = root / "market.sqlite"
        _insert_entry_and_forward(sqlite_path, _BENCHMARK, return_ratio=0.0)
        good_returns = [0.04, 0.05, 0.06, 0.04, 0.05, 0.06]
        candidates: list[dict[str, object]] = []
        for index, ret in enumerate(good_returns):
            ticker = f"G{index:03d}"
            _insert_entry_and_forward(sqlite_path, ticker, return_ratio=ret)
            candidates.append(
                {
                    "ticker": ticker,
                    "evidence_hits": [{"name": "good-lane", "playbook_id": "good-lane"}],
                }
            )
        for index in range(6):
            ticker = f"N{index:03d}"
            _insert_entry_and_forward(sqlite_path, ticker, return_ratio=0.0)
            candidates.append(
                {
                    "ticker": ticker,
                    "evidence_hits": [{"name": "neutral-lane", "playbook_id": "neutral-lane"}],
                }
            )
        _write_candidates(root / "2026" / "05" / "2026-05-01.yaml", candidates)
        return sqlite_path

    def test_lane_beating_baseline_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            sqlite_path = self._build_fixture(root)
            result = run_lane_scorecard(
                discover_week_specs(root),
                sqlite_path=sqlite_path,
                horizon_weeks=[4],
                min_resolved=5,
                bootstrap_iterations=1000,
            )
            by_lane = {(s.lane, s.horizon_weeks): s for s in result.scores}
            good = by_lane[("good-lane", 4)]
            self.assertEqual(good.resolved_count, 6)
            self.assertAlmostEqual(good.mean_relative, 0.05, places=6)
            self.assertAlmostEqual(good.baseline_mean_relative, 0.025, places=6)
            self.assertIs(good.decision, LaneDecision.KEEP)
            neutral = by_lane[("neutral-lane", 4)]
            self.assertIs(neutral.decision, LaneDecision.KILL_CANDIDATE)
            self.assertIs(by_lane[(ALL_CANDIDATES_COHORT, 4)].decision, LaneDecision.BASELINE)

    def test_scorecard_run_is_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            sqlite_path = self._build_fixture(root)
            weeks = discover_week_specs(root)
            first = scorecard_to_payload(
                run_lane_scorecard(
                    weeks, sqlite_path=sqlite_path, horizon_weeks=[4], min_resolved=5
                )
            )
            second = scorecard_to_payload(
                run_lane_scorecard(
                    weeks, sqlite_path=sqlite_path, horizon_weeks=[4], min_resolved=5
                )
            )
            self.assertEqual(first, second)

    def test_underpowered_lane_is_reviewed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            sqlite_path = self._build_fixture(root)
            result = run_lane_scorecard(
                discover_week_specs(root),
                sqlite_path=sqlite_path,
                horizon_weeks=[4],
                min_resolved=100,
                bootstrap_iterations=500,
            )
            good = next(s for s in result.scores if s.lane == "good-lane")
            self.assertIs(good.decision, LaneDecision.REVIEW)

    def test_proposal_recommended_queue_scored_against_baseline(self) -> None:
        from unittest import mock

        from baibai_loop.ledger.cohort_scorecard import (
            RECOMMENDED_QUEUE_COHORT,
            run_proposal_scorecard,
        )
        from baibai_loop.ledger.forward_return import HorizonReturn, TickerForwardReturn
        from baibai_loop.ledger.screening_replay import ProfileWeekResult, ReplayResult
        from baibai_loop.screening.rule_config import DEFAULT_RULES_PATH, load_screening_rules

        target = _ASOF + timedelta(days=28)
        forward = tuple(
            TickerForwardReturn(
                ticker=f"G{index:03d}",
                asof=_ASOF,
                entry_price=100.0,
                horizons=(
                    HorizonReturn(
                        weeks=4,
                        target_date=target,
                        resolved=True,
                        price=105.0,
                        return_ratio=0.05,
                        relative=0.05,
                    ),
                ),
            )
            for index in range(6)
        )
        fake_replay = ReplayResult(
            profiles=("balanced",),
            horizon_weeks=(4,),
            eval_cap=target,
            benchmark_ticker=_BENCHMARK,
            regime_lens=True,
            results=(
                ProfileWeekResult(
                    week=_ASOF,
                    profile="balanced",
                    is_holdout=False,
                    market_regime=None,
                    recommended_tickers=tuple(f"G{index:03d}" for index in range(6)),
                    recommended=(),
                    fast_dislocation_count=0,
                    long_hold_counts={},
                    suppressed_count=0,
                    distributions={},
                    forward_returns=forward,
                    forward_aggregates=(),
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            sqlite_path = self._build_fixture(root)
            with mock.patch(
                "baibai_loop.ledger.cohort_scorecard.run_replay", return_value=fake_replay
            ):
                result = run_proposal_scorecard(
                    discover_week_specs(root),
                    sqlite_path=sqlite_path,
                    rules=load_screening_rules(DEFAULT_RULES_PATH),
                    candidates_root=root,
                    ledger_root=root,
                    horizon_weeks=[4],
                    min_resolved=5,
                    bootstrap_iterations=500,
                )
        by_lane = {score.lane: score for score in result.scores}
        self.assertIn(RECOMMENDED_QUEUE_COHORT, by_lane)
        self.assertIn(ALL_CANDIDATES_COHORT, by_lane)
        recommended = by_lane[RECOMMENDED_QUEUE_COHORT]
        self.assertEqual(recommended.resolved_count, 6)
        self.assertAlmostEqual(recommended.mean_relative, 0.05, places=6)
        self.assertAlmostEqual(recommended.baseline_mean_relative, 0.025, places=6)
        self.assertIs(recommended.decision, LaneDecision.KEEP)


if __name__ == "__main__":
    unittest.main()
