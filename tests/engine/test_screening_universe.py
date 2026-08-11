from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_engine.screening.providers.jquants import JQuantsDailyBar
from baibai_engine.screening.schema import SecurityMaster
from baibai_engine.screening.universe import (
    TSE_33_SECTORS,
    UniverseSourceDriftError,
    build_universe,
)


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


class HistoricalMarketSegmentTests(unittest.TestCase):
    def test_pre_restructuring_segments_are_in_scope(self) -> None:
        # A point-in-time master from before the April 2022 renaming carries the
        # older segment names for the same markets. Reading them as out of scope
        # empties the universe for every cohort of that era, which is the whole
        # history the long horizons are measured over.
        for segment in (
            "東証一部",
            "東証二部",
            "マザーズ",
            "JASDAQ スタンダード",
            "JASDAQ グロース",
        ):
            with self.subTest(segment=segment):
                security = SecurityMaster(
                    code="130A",
                    name="Sample",
                    market_segment=segment,
                    sector_33="情報・通信業",
                    is_common_stock=True,
                )
                result = build_universe(
                    asof_date=date(2019, 11, 29),
                    securities=[security],
                    bars_by_ticker={"130A": _bars("130A")},
                    shares_outstanding_by_ticker={"130A": 400_000_000.0},
                    jpx_flags_by_ticker={},
                )
                self.assertIn("130A", result.snapshots)

    def test_the_professional_market_stays_out_of_scope(self) -> None:
        # Widening the vocabulary must not widen the scope: TOKYO PRO MARKET is not
        # a market a private investor buys on ordinary terms, in either era.
        security = SecurityMaster(
            code="130A",
            name="Sample",
            market_segment="TOKYO PRO MARKET",
            sector_33="情報・通信業",
            is_common_stock=True,
        )
        result = build_universe(
            asof_date=date(2019, 11, 29),
            securities=[security],
            bars_by_ticker={"130A": _bars("130A")},
            shares_outstanding_by_ticker={"130A": 400_000_000.0},
            jpx_flags_by_ticker={},
        )
        self.assertNotIn("130A", result.snapshots)
        self.assertEqual(result.exclusion_counts.get("market_out_of_scope"), 1)


class SectorClassificationScopeTests(unittest.TestCase):
    """Instrument type is decided by the sector code, so both sides are pinned.

    The master's own `is_common_stock` reads true for every row it has ever held, which is
    why an ETF and a preferred-investment security sat inside the eligible market segments.
    The sector code is the identifier the source actually fills in, and a screen that reads
    it has to keep every 33-sector name while removing the ones outside the classification.
    """

    def _result(self, sector: str, *, is_common: bool = True) -> object:
        security = SecurityMaster(
            code="130A",
            name="Sample",
            market_segment="Prime",
            sector_33=sector,
            is_common_stock=is_common,
        )
        return build_universe(
            asof_date=date(2026, 4, 24),
            securities=[security],
            bars_by_ticker={"130A": _bars("130A")},
            shares_outstanding_by_ticker={"130A": 400_000_000.0},
            jpx_flags_by_ticker={},
        )

    def test_a_name_outside_the_classification_leaves_the_universe(self) -> None:
        result = self._result("その他")
        self.assertNotIn("130A", result.snapshots)
        self.assertEqual(result.exclusion_counts.get("sector_out_of_classification"), 1)

    def test_an_unrecognised_sector_label_stops_the_build(self) -> None:
        """A label nobody knows is source drift, and drift here empties the population.

        The exclusion matches sector names exactly, so a renamed sector reads as "none of
        these are common stock" and removes every name carrying it. The count simply
        falls, which no caller can tell from a quiet market, so an unknown label has to
        stop the build instead of being excluded like a known one.
        """
        with self.assertRaises(UniverseSourceDriftError) as raised:
            self._result("-")
        self.assertIn("-", str(raised.exception))

    def test_a_renamed_sector_stops_the_build_before_it_empties_the_universe(self) -> None:
        with self.assertRaises(UniverseSourceDriftError):
            self._result("情報通信業")  # the real label carries a nakaguro

    def test_every_tse_sector_stays_in_scope(self) -> None:
        # The positive side across the whole classification: an exclusion that removed a
        # real sector would still pass a test that only checked one name.
        for sector in sorted(TSE_33_SECTORS):
            with self.subTest(sector=sector):
                result = self._result(sector)
                self.assertIn("130A", result.snapshots)
                self.assertNotIn("sector_out_of_classification", result.exclusion_counts)

    def test_the_master_flag_still_excludes_on_its_own(self) -> None:
        # Kept separate from the sector test: the two conditions answer different
        # questions and one going quiet must not silence the other.
        result = self._result("情報・通信業", is_common=False)
        self.assertNotIn("130A", result.snapshots)
        self.assertEqual(result.exclusion_counts.get("non_common_stock"), 1)
