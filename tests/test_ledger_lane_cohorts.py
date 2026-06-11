from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import yaml

from baibai_loop.ledger.lane_cohorts import (
    ALL_CANDIDATES_COHORT,
    LaneCohortAggregate,
    LaneCohortResult,
    LaneCohortWeek,
    lane_cohorts_to_payload,
    render_lane_cohort_summary,
    run_lane_cohorts,
)
from baibai_loop.ledger.weeks import WeekSpec
from baibai_loop.screening.sqlite_cache import open_connection

_ASOF = date(2026, 5, 1)


def _write_candidates(path: Path, candidates: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"candidates": candidates}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _insert_constant_growth_bars(
    sqlite_path: Path,
    ticker: str,
    *,
    entry_price: float,
    weekly_growth: float,
    weeks: int = 5,
) -> None:
    conn = open_connection(sqlite_path)
    try:
        rows = []
        for week in range(weeks + 1):
            price = entry_price * (1 + weekly_growth) ** week
            rows.append((ticker, (_ASOF + timedelta(days=week * 7)).isoformat(), price, price))
        conn.executemany(
            "INSERT OR REPLACE INTO jquants_daily_bars(ticker, traded_at, close, adjustment_close)"
            " VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _make_week(root: Path) -> WeekSpec:
    candidates_path = root / "2026" / "05" / "2026-05-01.yaml"
    _write_candidates(
        candidates_path,
        [
            {
                "ticker": "AAAA",
                "evidence_hits": [
                    {"name": "sales-discount-growth", "playbook_id": "sales-discount-growth"},
                ],
            },
            {
                "ticker": "BBBB",
                "evidence_hits": [
                    {
                        "name": "cashflow-yield-discount",
                        "playbook_id": "cashflow-yield-discount",
                    },
                    {
                        "name": "sales-discount-growth",
                        "playbook_id": "sales-discount-growth",
                        "sizing_eligible": False,
                    },
                ],
            },
            {
                "ticker": "CCCC",
                "evidence_hits": [
                    {
                        "name": "fcf-yield-discount",
                        "playbook_id": "fcf-yield-discount",
                        "source_status": "degraded",
                    },
                ],
            },
        ],
    )
    return WeekSpec(asof=_ASOF, candidates_path=candidates_path)


class RunLaneCohortsTests(unittest.TestCase):
    def test_aggregates_per_lane_with_relative_and_win_rate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sqlite_path = root / "market.sqlite"
            # benchmark +1%/week, AAAA +5%/week, BBBB -3%/week, CCCC flat.
            _insert_constant_growth_bars(sqlite_path, "1321", entry_price=100.0, weekly_growth=0.01)
            _insert_constant_growth_bars(sqlite_path, "AAAA", entry_price=50.0, weekly_growth=0.05)
            _insert_constant_growth_bars(sqlite_path, "BBBB", entry_price=80.0, weekly_growth=-0.03)
            _insert_constant_growth_bars(sqlite_path, "CCCC", entry_price=120.0, weekly_growth=0.0)
            week = _make_week(root / "candidates")

            result = run_lane_cohorts([week], sqlite_path=sqlite_path, horizon_weeks=(1,))

            self.assertEqual(result.eval_cap, _ASOF + timedelta(days=35))
            self.assertEqual(len(result.weeks), 1)
            week_result = result.weeks[0]
            self.assertEqual(week_result.candidate_count, 3)
            # BBBB's sales hit is sizing_eligible=false and CCCC's lane is
            # degraded, so lane cohorts shrink while all_candidates keeps all.
            self.assertEqual(
                week_result.lane_counts,
                {"cashflow-yield-discount": 1, "sales-discount-growth": 1},
            )
            by_lane = {aggregate.lane: aggregate for aggregate in week_result.aggregates}
            self.assertEqual(by_lane[ALL_CANDIDATES_COHORT].member_count, 3)
            self.assertEqual(by_lane[ALL_CANDIDATES_COHORT].resolved_count, 3)

            sales = by_lane["sales-discount-growth"]
            assert sales.mean_return is not None
            assert sales.mean_relative is not None
            self.assertAlmostEqual(sales.mean_return, 0.05, places=6)
            self.assertAlmostEqual(sales.mean_relative, 0.04, places=6)
            self.assertEqual(sales.win_rate_vs_benchmark, 1.0)

            cashflow = by_lane["cashflow-yield-discount"]
            assert cashflow.mean_relative is not None
            self.assertAlmostEqual(cashflow.mean_relative, -0.04, places=6)
            self.assertEqual(cashflow.win_rate_vs_benchmark, 0.0)

    def test_unresolved_horizon_yields_zero_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sqlite_path = root / "market.sqlite"
            _insert_constant_growth_bars(
                sqlite_path, "1321", entry_price=100.0, weekly_growth=0.01, weeks=1
            )
            _insert_constant_growth_bars(
                sqlite_path, "AAAA", entry_price=50.0, weekly_growth=0.05, weeks=1
            )
            week = _make_week(root / "candidates")

            result = run_lane_cohorts([week], sqlite_path=sqlite_path, horizon_weeks=(1, 4))

            by_key = {
                (aggregate.lane, aggregate.horizon_weeks): aggregate
                for aggregate in result.weeks[0].aggregates
            }
            self.assertEqual(by_key[("sales-discount-growth", 1)].resolved_count, 1)
            unresolved = by_key[("sales-discount-growth", 4)]
            self.assertEqual(unresolved.resolved_count, 0)
            self.assertIsNone(unresolved.mean_return)
            self.assertIsNone(unresolved.win_rate_vs_benchmark)


class LaneCohortPayloadTests(unittest.TestCase):
    def _result(self) -> LaneCohortResult:
        return LaneCohortResult(
            horizon_weeks=(1,),
            eval_cap=date(2026, 6, 5),
            benchmark_ticker="1321",
            weeks=(
                LaneCohortWeek(
                    week=_ASOF,
                    candidates_path="x.yaml",
                    candidate_count=2,
                    lane_counts={"sales-discount-growth": 2},
                    aggregates=(
                        LaneCohortAggregate(
                            lane="sales-discount-growth",
                            horizon_weeks=1,
                            member_count=2,
                            resolved_count=2,
                            mean_return=0.01,
                            median_return=0.01,
                            mean_relative=-0.02,
                            win_rate_vs_benchmark=0.5,
                        ),
                    ),
                ),
            ),
        )

    def test_payload_serializes_aggregates(self) -> None:
        payload = lane_cohorts_to_payload(self._result())
        self.assertEqual(payload["eval_cap"], "2026-06-05")
        weeks = payload["weeks"]
        assert isinstance(weeks, list)
        self.assertEqual(weeks[0]["lane_counts"], {"sales-discount-growth": 2})
        self.assertEqual(weeks[0]["aggregates"][0]["win_rate_vs_benchmark"], 0.5)

    def test_summary_renders_one_row_per_aggregate(self) -> None:
        summary = render_lane_cohort_summary(self._result())
        lines = summary.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("sales-discount-growth", lines[1])
        self.assertIn("+1.0", lines[1])


if __name__ == "__main__":
    unittest.main()
