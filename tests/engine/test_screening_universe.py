"""旧較正replay用の母集団・価格basis。現行五モデルの生成対象や選定政策とは別。"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from tests.helpers.master import TSE_33_SECTORS

from baibai_engine.machine_analysis.screening.discovery.candidates import build_candidate_contexts
from baibai_engine.machine_analysis.screening.metric_builder import build_metrics
from baibai_engine.machine_analysis.screening.rule_config import load_screening_rules
from baibai_engine.machine_analysis.screening.screening_run_serializer import stock_analysis_payload
from baibai_engine.machine_analysis.screening.stock_analysis_builder import build_stock_analysis
from baibai_engine.machine_analysis.screening.universe import (
    build_universe,
    candidate_comparison_population,
)
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.stocks.models import SecurityMaster
from baibai_engine.machine_analysis.observations.prices.price_models import JQuantsAdjustmentFactorEvent, JQuantsDailyBar


def _bars(
    code: str,
    close: float = 100.0,
    turnover: float = 100_000_000.0,
    *,
    end: date = date(2026, 4, 24),
) -> list[JQuantsDailyBar]:
    # Span 200 days ending at the requested as-of; the trailing 20 bars feed the
    # turnover / market-cap facts recorded on each snapshot.
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
    def test_latest_stale_price_uses_close_null_action_event_basis(self) -> None:
        security = SecurityMaster(
            ticker="130A",
            name="Suspended consolidation",
            market_segment="Prime",
            sector_33="情報・通信業",
            is_common_stock=True,
        )
        asof = date(2026, 4, 25)
        events = [JQuantsAdjustmentFactorEvent("130A", asof, 100.0, "2")]

        fixed = build_universe(
            asof_date=asof,
            securities=[security],
            bars_by_ticker={"130A": _bars("130A", close=1.0)},
            shares_outstanding_by_ticker={"130A": 1_000_000.0},
            jpx_flags_by_ticker={},
            adjustment_events_by_ticker={"130A": events},
        )
        mutation = build_universe(
            asof_date=asof,
            securities=[security],
            bars_by_ticker={"130A": _bars("130A", close=1.0)},
            shares_outstanding_by_ticker={"130A": 1_000_000.0},
            jpx_flags_by_ticker={},
            adjustment_events_by_ticker={"130A": ()},
        )

        self.assertEqual(fixed.snapshots["130A"].market_cap_oku, 1)
        self.assertEqual(mutation.snapshots["130A"].market_cap_oku, 0)

    def test_short_history_uses_asof_split_basis(self) -> None:
        security = SecurityMaster(
            ticker="130A",
            name="Newcomer",
            market_segment="Prime",
            sector_33="情報・通信業",
            is_common_stock=True,
        )
        asof = date(2026, 4, 24)
        bar = JQuantsDailyBar(
            ticker="130A",
            traded_at=asof - timedelta(days=1),
            close=100.0,
            turnover_value=100_000_000.0,
        )
        result = build_universe(
            asof_date=asof,
            securities=[security],
            bars_by_ticker={"130A": [bar]},
            shares_outstanding_by_ticker={"130A": 200_000_000.0},
            jpx_flags_by_ticker={},
            adjustment_events_by_ticker={
                "130A": [JQuantsAdjustmentFactorEvent("130A", asof, 0.5, "1")]
            },
        )
        self.assertEqual(result.snapshots["130A"].market_cap_oku, 100)
        self.assertIsNone(result.snapshots["130A"].avg_turnover_oku)

    def test_build_universe_keeps_eligible_security(self) -> None:
        security = SecurityMaster(
            ticker="130A",
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

    def test_future_bar_cannot_change_asof_market_cap_or_turnover(self) -> None:
        security = SecurityMaster(
            ticker="130A",
            name="Sample",
            market_segment="Prime",
            sector_33="情報・通信業",
            is_common_stock=True,
        )
        asof = date(2026, 4, 24)
        clean_bars = _bars("130A")
        future = JQuantsDailyBar(
            ticker="130A",
            traded_at=asof + timedelta(days=1),
            close=1_000.0,
            turnover_value=9_000_000_000.0,
        )

        clean = build_universe(
            asof_date=asof,
            securities=[security],
            bars_by_ticker={"130A": clean_bars},
            shares_outstanding_by_ticker={"130A": 100_000_000.0},
            jpx_flags_by_ticker={},
        )
        polluted = build_universe(
            asof_date=asof,
            securities=[security],
            bars_by_ticker={"130A": [*clean_bars, future]},
            shares_outstanding_by_ticker={"130A": 100_000_000.0},
            jpx_flags_by_ticker={},
        )

        self.assertEqual(polluted.snapshots["130A"], clean.snapshots["130A"])
        self.assertEqual(clean.snapshots["130A"].market_cap_oku, 100)
        self.assertEqual(clean.snapshots["130A"].avg_turnover_oku, 1.0)

    def test_build_universe_keeps_200_oku_band_security(self) -> None:
        security = SecurityMaster(
            ticker="201A",
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
            ticker="202A",
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
            ticker="7203",
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
        # Regulation flags remain facts in the universe snapshot.
        # Candidate discovery applies the required JPX regulation exclusions.
        self.assertIn("7203", result.snapshots)
        self.assertEqual(result.snapshots["7203"].jpx_flags, ("整理銘柄",))
        self.assertEqual(result.exclusion_counts, {})

    def test_build_universe_records_price_history_span_as_fact(self) -> None:
        security = SecurityMaster(
            ticker="300A",
            name="Limited Price History",
            market_segment="Growth",
            sector_33="情報・通信業",
            is_common_stock=True,
        )
        end = date(2026, 4, 24)
        # Available observations start 90 calendar days before asof.
        # This does not establish when the security was listed.
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
        self.assertEqual(result.snapshots["300A"].price_history_span_days, 90)

    def test_build_universe_keeps_short_history_with_null_20bar_turnover(self) -> None:
        security = SecurityMaster(
            ticker="400A",
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
        self.assertIn("400A", result.snapshots)
        self.assertIsNone(result.snapshots["400A"].avg_turnover_oku)
        self.assertEqual(result.snapshots["400A"].market_cap_oku, 400)
        self.assertNotIn("insufficient_bar_history", result.exclusion_counts)

    def test_price_scope_boundaries_do_not_fill_missing_capital_or_price(self) -> None:
        security = SecurityMaster(
            ticker="400A",
            name="Short History",
            market_segment="Growth",
            sector_33="情報・通信業",
            is_common_stock=True,
        )
        end = date(2026, 4, 24)
        for count in (0, 1, 5, 19, 20):
            with self.subTest(count=count):
                bars = [
                    JQuantsDailyBar(
                        ticker="400A",
                        traded_at=end - timedelta(days=count - index - 1),
                        close=100.0,
                        turnover_value=300_000_000.0,
                    )
                    for index in range(count)
                ]
                result = build_universe(
                    asof_date=end,
                    securities=[security],
                    bars_by_ticker={"400A": bars},
                    shares_outstanding_by_ticker={},
                    jpx_flags_by_ticker={},
                )
                if count == 0:
                    self.assertEqual(result.exclusion_flags["400A"], ("insufficient_bar_history",))
                    continue
                snapshot = result.snapshots["400A"]
                self.assertIsNone(snapshot.market_cap_oku)
                self.assertEqual(snapshot.price_history_span_days, count - 1)
                self.assertEqual(snapshot.avg_turnover_oku, 3.0 if count == 20 else None)

        for close in (0.0, -1.0):
            with self.subTest(close=close):
                result = build_universe(
                    asof_date=end,
                    securities=[security],
                    bars_by_ticker={
                        "400A": [
                            JQuantsDailyBar(
                                ticker="400A",
                                traded_at=end,
                                close=close,
                                turnover_value=300_000_000.0,
                            )
                        ]
                    },
                    shares_outstanding_by_ticker={"400A": 400_000_000.0},
                    jpx_flags_by_ticker={},
                )
                self.assertNotIn("400A", result.snapshots)
                self.assertEqual(result.exclusion_flags["400A"], ("invalid_price_basis",))

    def test_invalid_factor_only_excludes_when_needed_for_price_basis(self) -> None:
        security = SecurityMaster("400A", "Invalid Factor", "Growth", "情報・通信業", True)
        asof = date(2026, 4, 24)
        price_day = asof - timedelta(days=1)
        latest = JQuantsDailyBar("400A", price_day, 100.0, 300_000_000.0)
        for factor in (0.0, -1.0):
            for event_day in (price_day - timedelta(days=1), price_day, asof):
                with self.subTest(factor=factor, event_day=event_day):
                    event = JQuantsDailyBar(
                        "400A", event_day, 100.0, 300_000_000.0, adjustment_factor=factor
                    )
                    result = build_universe(
                        asof,
                        [security],
                        {"400A": [latest]},
                        {"400A": 400_000_000.0},
                        {},
                        adjustment_events_by_ticker={"400A": [event]},
                    )
                    if event_day > price_day:
                        self.assertEqual(result.exclusion_flags["400A"], ("invalid_price_basis",))
                    else:
                        self.assertEqual(result.snapshots["400A"].market_cap_oku, 400)

    def test_short_history_keeps_financial_analysis_and_approach(self) -> None:
        security = SecurityMaster(
            ticker="400A",
            name="Priced Newcomer",
            market_segment="Growth",
            sector_33="情報・通信業",
            is_common_stock=True,
        )
        asof = date(2026, 4, 24)
        summary = JQuantsFinancialSummary(
            # Calculation fixture: guidance has already been projected for this snapshot.
            forecast_status="confirmed",
            ticker="400A",
            disclosed_at=asof,
            forecast_eps=10.0,
            bps=50.0,
            shares_outstanding=1_000_000.0,
            treasury_shares=0.0,
            fiscal_period="FY",
            fiscal_year_end=asof,
            period_end=asof,
        )
        rules = load_screening_rules()
        for count in (1, 5, 19, 20):
            with self.subTest(count=count):
                bars = [
                    JQuantsDailyBar(
                        ticker="400A",
                        traded_at=asof - timedelta(days=count - index - 1),
                        close=100.0 + index,
                        turnover_value=300_000_000.0,
                    )
                    for index in range(count)
                ]
                universe = build_universe(
                    asof, [security], {"400A": bars}, {"400A": 1_000_000.0}, {}
                )
                self.assertEqual(
                    candidate_comparison_population(universe.snapshots, rules),
                    frozenset({"400A"}),
                )
                metrics = build_metrics(
                    asof,
                    {"400A": security},
                    {"400A": bars},
                    {"400A": [summary]},
                    {},
                    rules,
                    frozenset({"400A"}),
                )
                analysis = build_stock_analysis(
                    ticker="400A",
                    security=security,
                    financial=metrics.financials["400A"],
                    derived=metrics.derived["400A"],
                    universe_snapshot=universe.snapshots["400A"],
                )
                self.assertIsNotNone(analysis.per_forward)
                self.assertIsNotNone(analysis.pbr)
                self.assertIsNone(analysis.price_change_20d)
                self.assertIsNone(analysis.metrics["fv_self_range_yen"])
                self.assertEqual(analysis.avg_turnover_oku, 3.0 if count == 20 else None)
                discovery = build_candidate_contexts(
                    [stock_analysis_payload(analysis)],
                    rules=rules.candidate_discovery,
                    required_jpx_flags=rules.universe.required_jpx_flags,
                )
                self.assertEqual(
                    [ticker for ticker, context in discovery.contexts.items() if context.reasons],
                    ["400A"],
                )


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
                    ticker="130A",
                    name="Sample",
                    market_segment=segment,
                    sector_33="情報・通信業",
                    is_common_stock=True,
                )
                result = build_universe(
                    asof_date=date(2019, 11, 29),
                    securities=[security],
                    bars_by_ticker={"130A": _bars("130A", end=date(2019, 11, 29))},
                    shares_outstanding_by_ticker={"130A": 400_000_000.0},
                    jpx_flags_by_ticker={},
                )
                self.assertIn("130A", result.snapshots)

    def test_the_professional_market_stays_out_of_scope(self) -> None:
        # Widening the vocabulary must not widen the scope: TOKYO PRO MARKET is not
        # a market a private investor buys on ordinary terms, in either era.
        security = SecurityMaster(
            ticker="130A",
            name="Sample",
            market_segment="TOKYO PRO MARKET",
            sector_33="情報・通信業",
            is_common_stock=True,
        )
        result = build_universe(
            asof_date=date(2019, 11, 29),
            securities=[security],
            bars_by_ticker={"130A": _bars("130A", end=date(2019, 11, 29))},
            shares_outstanding_by_ticker={"130A": 400_000_000.0},
            jpx_flags_by_ticker={},
        )
        self.assertNotIn("130A", result.snapshots)
        self.assertEqual(result.exclusion_counts.get("market_out_of_scope"), 1)


class NativeProductScopeTests(unittest.TestCase):
    def test_product_category_not_sector_name_decides_ordinary_stock(self) -> None:
        from baibai_engine.machine_analysis.observations.stocks.master_snapshot import normalize_master_record

        for category in ("011", "012", "013", "014", "021", "022", "023", "024"):
            with self.subTest(category=category):
                security = normalize_master_record(
                    {
                        "Code": "130A0",
                        "CoName": "Source security",
                        "MktNm": "Prime",
                        "S33Nm": "情報・通信業",
                        "ProdCat": category,
                    }
                )
                assert security is not None
                result = build_universe(
                    date(2026, 4, 24), [security], {"130A": _bars("130A")}, {}, {}
                )
                self.assertEqual("130A" in result.snapshots, category == "011")
                if category != "011":
                    self.assertEqual(result.exclusion_flags["130A"], ("non_common_stock",))

    def test_sector_vocabulary_does_not_override_native_ordinary_classification(self) -> None:
        for sector in (*sorted(TSE_33_SECTORS), "新業種", "その他"):
            with self.subTest(sector=sector):
                security = SecurityMaster("130A", "Sample", "Prime", sector, True)
                result = build_universe(
                    date(2026, 4, 24), [security], {"130A": _bars("130A")}, {}, {}
                )
                self.assertIn("130A", result.snapshots)

    def test_preferred_issue_is_not_projected_as_the_same_common_ticker(self) -> None:
        from baibai_engine.machine_analysis.observations.stocks.master_snapshot import normalize_master_record

        self.assertIsNone(
            normalize_master_record(
                {
                    "Code": "130A1",
                    "CoName": "Preferred",
                    "MktNm": "Prime",
                    "S33Nm": "情報・通信業",
                    "ProdCat": "011",
                }
            )
        )
