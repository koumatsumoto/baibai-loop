from __future__ import annotations

import unittest
from datetime import timedelta

from baibai_engine.market.bars import JQuantsDailyBar
from baibai_engine.market.jquants_models import JQuantsFinancialSummary
from baibai_engine.market.models import SecurityMaster
from baibai_engine.screening.metrics import (
    MetricBuildResult,
    build_metrics,
)


class MedianPopulationTests(unittest.TestCase):
    def test_sector_median_uses_only_population_tickers(self) -> None:
        from datetime import date as _date

        from baibai_engine.market.bars import JQuantsDailyBar
        from baibai_engine.market.jquants_models import (
            JQuantsFinancialSummary,
        )

        asof = _date(2026, 4, 24)

        def bars(code: str, close: float) -> list[JQuantsDailyBar]:

            return [
                JQuantsDailyBar(
                    ticker=code,
                    traded_at=asof - timedelta(days=30 - i),
                    close=close,
                    turnover_value=2.0e8,
                )
                for i in range(30)
            ]

        def summary(code: str, eps: float) -> JQuantsFinancialSummary:
            shares = 1e8
            return JQuantsFinancialSummary(
                ticker=code,
                disclosed_at=_date(2026, 2, 1),
                eps_ttm=eps,
                profit=eps * shares,
                shares_outstanding=shares,
                treasury_shares=0.0,
                type_of_current_period="FY",
                period_start=_date(2025, 1, 1),
                period_end=_date(2025, 12, 31),
            )

        securities = {
            code: SecurityMaster(
                ticker=code,
                name=f"name-{code}",
                market_segment="プライム",
                sector_33="機械",
                is_common_stock=True,
            )
            # 12 tickers so the sector clears the n>=10 sector-median threshold.
            for code in [f"11{i:02d}" for i in range(12)]
        }
        bars_by = {code: bars(code, close=100.0) for code in securities}
        # Liquid population PERs are all 10 (eps 10); the out-of-population
        # ticker has PER 100 (eps 1) and would distort the median if included.
        summaries = {code: [summary(code, eps=10.0)] for code in securities}
        summaries["1111"] = [summary("1111", eps=1.0)]
        population = frozenset(code for code in securities if code != "1111")

        result = build_metrics(
            asof_date=asof,
            securities_by_ticker=securities,
            bars_by_ticker=bars_by,
            summaries_by_ticker=summaries,
            edinet_by_ticker={},
            median_population=population,
        )

        gap = result.derived["1111"].sector_median_gap.get("per_trailing")
        assert gap is not None
        # PER 100 vs liquid-population median 10 -> gap = 9.0; a full-population
        # median would shift the baseline and lower the gap.
        self.assertAlmostEqual(gap, 9.0, places=6)

    def _two_sector_metrics(self) -> MetricBuildResult:
        """A thick sector at PER 10 and a thin one at PER 4, priced by the same close.

        The market median is 10 because the thick sector outnumbers the thin one, so the
        thin sector's own median (4) and the market's (10) disagree. Whichever baseline
        answers is then readable from the gap alone, and the basis label has to agree
        with it.
        """
        from datetime import date as _date

        from baibai_engine.market.bars import JQuantsDailyBar
        from baibai_engine.market.jquants_models import (
            JQuantsFinancialSummary,
        )
        from baibai_engine.market.models import SecurityMaster

        asof = _date(2026, 4, 24)
        shares = 1e8

        def bars(code: str) -> list[JQuantsDailyBar]:
            return [
                JQuantsDailyBar(
                    ticker=code,
                    traded_at=asof - timedelta(days=30 - index),
                    close=100.0,
                    turnover_value=2.0e8,
                )
                for index in range(30)
            ]

        def summary(code: str, eps: float) -> JQuantsFinancialSummary:
            return JQuantsFinancialSummary(
                ticker=code,
                disclosed_at=_date(2026, 2, 1),
                eps_ttm=eps,
                profit=eps * shares,
                shares_outstanding=shares,
                treasury_shares=0.0,
                type_of_current_period="FY",
                period_start=_date(2025, 1, 1),
                period_end=_date(2025, 12, 31),
            )

        thick = {f"11{index:02d}": "機械" for index in range(12)}
        thin = {f"22{index:02d}": "海運業" for index in range(3)}
        securities = {
            code: SecurityMaster(
                ticker=code,
                name=f"name-{code}",
                market_segment="プライム",
                sector_33=sector,
                is_common_stock=True,
            )
            for code, sector in (thick | thin).items()
        }
        summaries = {
            code: [summary(code, eps=10.0 if code in thick else 25.0)] for code in securities
        }
        return build_metrics(
            asof_date=asof,
            securities_by_ticker=securities,
            bars_by_ticker={code: bars(code) for code in securities},
            summaries_by_ticker=summaries,
            edinet_by_ticker={},
            median_population=frozenset(securities),
        )

    def test_a_sector_above_the_floor_is_compared_against_itself(self) -> None:
        result = self._two_sector_metrics()
        derived = result.derived["1100"]
        self.assertEqual(derived.sector_median_basis["per_trailing"], "sector")
        self.assertAlmostEqual(derived.sector_median_value["per_trailing"], 10.0, places=6)
        # Its own sector answers, so a name at the sector's own multiple has no gap.
        self.assertAlmostEqual(derived.sector_median_gap["per_trailing"], 0.0, places=6)

    def test_an_axis_with_no_baseline_at_all_records_no_basis(self) -> None:
        # EV/EBITDA needs an EDINET figure the fixture does not supply, so neither the
        # sector nor the market can answer. Writing "market" there would put every row of
        # a market-wide blank axis alongside the rows that genuinely fell through, and the
        # two are not the same observation.
        result = self._two_sector_metrics()
        derived = result.derived["1100"]
        self.assertIsNone(derived.sector_median_value["ev_ebitda"])
        self.assertNotIn("ev_ebitda", derived.sector_median_basis)
        # The axes that did answer still carry theirs.
        self.assertIn("per_trailing", derived.sector_median_basis)

    def test_a_sector_below_the_floor_is_compared_against_the_market_and_says_so(self) -> None:
        result = self._two_sector_metrics()
        derived = result.derived["2200"]
        self.assertEqual(derived.sector_median_basis["per_trailing"], "market")
        # The market's 10, not the thin sector's own 4.
        self.assertAlmostEqual(derived.sector_median_value["per_trailing"], 10.0, places=6)
        # PER 4 against a baseline of 10 reads as 60% cheap, which is what sector
        # composition alone produces here — the label is what separates the two readings.
        self.assertAlmostEqual(derived.sector_median_gap["per_trailing"], -0.6, places=6)
