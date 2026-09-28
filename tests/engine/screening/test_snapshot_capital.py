from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from tests.engine.screening.metric_fixtures import (
    _daily_bars,
    _edinet_metric_record,
    _security,
    _summary,
)
from tests.engine.screening.metric_snapshot_fixtures import SnapshotFixtures

from baibai_engine.market.bars import JQuantsDailyBar
from baibai_engine.screening.metrics import (
    build_metrics,
    build_normalized_profit_signals,
    build_shares_outstanding_index,
)
from baibai_engine.screening.metrics.capital import (
    _resolve_capital_basis,
    _shares_excluding_treasury,
)
from baibai_engine.screening.metrics.periods import _ttm_value
from baibai_engine.screening.rule_config import load_screening_rules
from baibai_engine.screening.schema import TTMQuality


class ScreeningMetricsTests(SnapshotFixtures):
    def test_february_fy_fallback_restores_yoy_and_share_count(self) -> None:
        asof = date(2025, 4, 30)
        rows = [
            _summary(
                "130A",
                date(2024, 4, 10),
                fiscal_period="FY",
                fiscal_year_end=date(2024, 2, 29),
                eps_ttm=20.0,
                shares_outstanding=100_000_000.0,
                sales=100.0,
            ),
            _summary(
                "130A",
                date(2025, 4, 10),
                fiscal_period="FY",
                fiscal_year_end=date(2025, 2, 28),
                eps_ttm=30.0,
                shares_outstanding=98_000_000.0,
                sales=120.0,
            ),
        ]
        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": _daily_bars("130A", asof, 40)},
            summaries_by_ticker={"130A": rows},
            edinet_by_ticker={},
        ).financials["130A"]
        self.assertEqual(snapshot.eps_yoy, 0.5)
        self.assertAlmostEqual(snapshot.sales_yoy, 0.2)
        self.assertAlmostEqual(snapshot.net_share_change_yoy, -0.02)
        self.assertAlmostEqual(snapshot.tradable_share_change_yoy, -0.02)

    def test_normalized_profit_uses_split_basis_and_rejects_nonpositive_mean(self) -> None:
        asof = date(2026, 6, 30)
        bars = [
            JQuantsDailyBar(
                ticker="130A",
                traded_at=date(2024, 1, 10),
                close=50.0,
                turnover_value=1_000_000.0,
                adjustment_factor=0.5,
            )
        ]
        summaries = [
            _summary(
                "130A",
                date(2023, 5, 10),
                fiscal_period="FY",
                fiscal_year_end=date(2023, 3, 31),
                eps_ttm=40.0,
            ),
            _summary(
                "130A",
                date(2024, 5, 10),
                fiscal_period="FY",
                fiscal_year_end=date(2024, 3, 31),
                eps_ttm=20.0,
            ),
            _summary(
                "130A",
                date(2025, 5, 10),
                fiscal_period="FY",
                fiscal_year_end=date(2025, 3, 31),
                eps_ttm=20.0,
            ),
        ]
        split_safe = build_normalized_profit_signals(summaries, bars, asof, close=100.0)
        loss_mean = build_normalized_profit_signals(
            [
                replace(summary, eps_ttm=value)
                for summary, value in zip(summaries, (-40.0, 0.0, 10.0), strict=True)
            ],
            (),
            asof,
            close=100.0,
        )

        self.assertAlmostEqual(split_safe.normalized_per_3fy or 0.0, 5.0)
        self.assertIsNone(loss_mean.normalized_per_3fy)

    def test_market_cap_adjusts_shares_for_split_after_disclosure(self) -> None:
        """開示後の分割 (権利落ち bar の adjustment_factor) を株数へ補正する。

        開示時点の株数 100 株・1:2 分割 (factor 0.5) 後の終値 50 のとき、
        naive な 100 x 50 = 5,000 ではなく 200 x 50 = 10,000 が market cap。
        """
        asof = date(2026, 7, 1)
        security = _security()
        bars = []
        for index in range(30):
            traded_at = asof - timedelta(days=29 - index)
            bars.append(
                JQuantsDailyBar(
                    ticker="130A",
                    traded_at=traded_at,
                    close=100.0 if index < 27 else 50.0,
                    turnover_value=300_000_000.0,
                    adjustment_factor=0.5 if index == 27 else 1.0,
                )
            )
        summaries = [
            _summary(
                "130A",
                asof - timedelta(days=20),
                fiscal_period="FY",
                period_start=date(2025, 4, 1),
                period_end=date(2026, 3, 31),
                shares_outstanding=100.0,
            )
        ]
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": security},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        )
        financial = result.financials["130A"]
        assert financial.market_cap is not None
        self.assertAlmostEqual(financial.market_cap, 50.0 * 200.0, places=3)
        self.assertAlmostEqual(financial.shares_outstanding or 0.0, 200.0, places=3)
        # 分割を跨ぐ行の forecast_eps は基準 (分割考慮前/後) を機械判別できないため
        # None に落ち、per_forward は出ない (偽値を出さない)。
        self.assertIsNone(financial.per_forward)

    def test_market_cap_excludes_treasury_shares(self) -> None:
        """時価総額の分母は市場が値付けできる株数 (発行済 - 自己株式) である。"""
        # 自己株 100 株 (10%)、非支配株主持分 200 (自己資本 800 / 純資産 1,000)。
        snapshot = self._capital_snapshot(
            shares_outstanding=1_000.0,
            treasury_shares=100.0,
            equity_to_asset_ratio=0.4,
            bps=800.0 / 900.0,
            total_assets=2_000.0,
            equity=1_000.0,
            close=1.0,
        )

        self.assertAlmostEqual(snapshot.market_cap or 0.0, 900.0, places=6)
        self.assertAlmostEqual(snapshot.market_price_yen or 0.0, 1.0, places=6)
        self.assertAlmostEqual(snapshot.shares_ex_treasury or 0.0, 900.0, places=6)
        self.assertAlmostEqual(snapshot.equity_ratio or 0.0, 0.4, places=6)

    def test_market_cap_is_absent_when_treasury_is_unobserved(self) -> None:
        """自己株式数が欠損する行で発行済を代用しない。

        代用すると、どれだけ過大か分からない時価総額が現金比率・利回り・時価総額 gate へ
        入る。答えないことで、その銘柄は母集団から外れる。
        """
        snapshot = self._capital_snapshot(
            shares_outstanding=1_000.0,
            treasury_shares=None,
            equity_to_asset_ratio=0.4,
            bps=1.0,
            total_assets=2_000.0,
            equity=1_000.0,
            close=1.0,
        )

        self.assertIsNone(snapshot.market_cap)

    def test_equity_ratio_is_absent_rather_than_the_net_asset_ratio(self) -> None:
        """自己資本比率が観測できない行で純資産比率へ代用しない。

        純資産は非支配株主持分を含むので、代用は少数株主持分の大きい銘柄で自己資本比率を
        数 pt 過大にする。過大は screening gate を通しやすくする向きなので黙って埋めない。
        """
        snapshot = self._capital_snapshot(
            shares_outstanding=1_000.0,
            treasury_shares=0.0,
            equity_to_asset_ratio=None,
            bps=1.0,
            total_assets=2_000.0,
            equity=1_000.0,
            close=1.0,
        )

        self.assertIsNone(snapshot.equity_ratio)

    def test_broken_treasury_count_leaves_no_market_cap(self) -> None:
        """発行済を超える自己株式数で負や過大な時価総額を作らない。"""
        snapshot = self._capital_snapshot(
            shares_outstanding=1_000.0,
            treasury_shares=1_200.0,
            equity_to_asset_ratio=0.4,
            bps=1.0,
            total_assets=2_000.0,
            equity=1_000.0,
            close=1.0,
        )

        self.assertIsNone(snapshot.market_cap)

    def test_treasury_shares_follow_the_split_basis_of_issued_shares(self) -> None:
        """分割を跨ぐ行で自己株式数も換算する。片方だけだと差が壊れる。"""
        asof = date(2026, 7, 1)
        bars = [
            JQuantsDailyBar(
                ticker="130A",
                traded_at=asof - timedelta(days=29 - index),
                close=100.0 if index < 27 else 50.0,
                turnover_value=300_000_000.0,
                adjustment_factor=0.5 if index == 27 else 1.0,
            )
            for index in range(30)
        ]
        summary = replace(
            _summary(
                "130A",
                asof - timedelta(days=20),
                fiscal_period="FY",
                period_start=date(2025, 4, 1),
                period_end=date(2026, 3, 31),
                shares_outstanding=100.0,
            ),
            treasury_shares=10.0,
            equity_to_asset_ratio=0.4,
        )
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": [summary]},
            edinet_by_ticker={},
        )

        financial = result.financials["130A"]
        # 1:2 分割後は発行済 200 株・自己株 20 株なので、時価総額は 50 x 180。
        self.assertAlmostEqual(financial.market_cap or 0.0, 50.0 * 180.0, places=3)

    def test_universe_share_index_matches_the_snapshot_market_cap_basis(self) -> None:
        """「時価総額」という同じ語が 2 つの値を指さないことを固定する。

        universe の時価総額は `build_shares_outstanding_index` から作られて時価総額 gate の
        判定値になり、`FinancialSnapshot.market_cap` は倍率と利回りの分母になる。別々に
        計算されているので、株数の基準が片方だけ動くと同じ語が食い違う。
        """
        asof = date(2026, 7, 1)
        close = 1_000.0
        bars = [
            JQuantsDailyBar(
                ticker="130A",
                traded_at=asof - timedelta(days=29 - index),
                close=close,
                turnover_value=300_000_000.0,
            )
            for index in range(30)
        ]
        summary = replace(
            _summary(
                "130A",
                asof - timedelta(days=20),
                fiscal_period="FY",
                period_start=date(2025, 4, 1),
                period_end=date(2026, 3, 31),
                shares_outstanding=1_000_000.0,
            ),
            treasury_shares=250_000.0,
            equity_to_asset_ratio=0.5,
        )
        summaries_by_ticker = {"130A": [summary]}
        bars_by_ticker = {"130A": bars}

        index = build_shares_outstanding_index(summaries_by_ticker, bars_by_ticker, asof)
        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker=bars_by_ticker,
            summaries_by_ticker=summaries_by_ticker,
            edinet_by_ticker={},
        ).financials["130A"]

        self.assertAlmostEqual(index["130A"] or 0.0, 750_000.0, places=3)
        assert snapshot.market_cap is not None
        self.assertAlmostEqual(snapshot.market_cap, close * (index["130A"] or 0.0), places=3)

    def test_shares_excluding_treasury_rejects_invalid_values(self) -> None:
        self.assertIsNone(_shares_excluding_treasury(1_000.0, -1.0))
        self.assertIsNone(_shares_excluding_treasury(0.0, 0.0))
        self.assertIsNone(_shares_excluding_treasury(1_000.0, 1_000.0))
        self.assertEqual(_shares_excluding_treasury(1_000.0, 100.0), 900.0)

    def test_treasury_carry_stops_after_issued_shares_decrease(self) -> None:
        """消却後の発行済から消却前の自己株をもう一度引かない。"""
        asof = date(2026, 7, 1)
        bars = _daily_bars("130A", asof, 40)
        summaries = [
            _summary(
                "130A",
                asof - timedelta(days=300),
                fiscal_period="FY",
                shares_outstanding=1_000.0,
                treasury_shares=300.0,
            ),
            _summary(
                "130A",
                asof - timedelta(days=20),
                shares_outstanding=700.0,
                treasury_shares=None,
            ),
        ]

        index = build_shares_outstanding_index({"130A": summaries}, {"130A": bars}, asof)
        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        ).financials["130A"]

        self.assertIsNone(index["130A"])
        self.assertIsNone(snapshot.market_cap)
        self.assertEqual(snapshot.shares_outstanding, 700.0)
        self.assertEqual(
            snapshot.capital_basis_failure_reason,
            "indeterminate_positive_treasury_after_later_issued_observation",
        )

    def test_treasury_carry_stops_after_issued_shares_increase(self) -> None:
        """増資と同時の自己株処分を観測できないため、古い自己株を再控除しない。"""
        asof = date(2026, 7, 1)
        bars = _daily_bars("130A", asof, 40)
        summaries = [
            _summary(
                "130A",
                asof - timedelta(days=300),
                fiscal_period="FY",
                shares_outstanding=1_000.0,
                treasury_shares=100.0,
            ),
            _summary(
                "130A",
                asof - timedelta(days=20),
                shares_outstanding=1_100.0,
                treasury_shares=None,
            ),
        ]

        index = build_shares_outstanding_index({"130A": summaries}, {"130A": bars}, asof)
        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        ).financials["130A"]

        self.assertIsNone(index["130A"])
        self.assertIsNone(snapshot.shares_ex_treasury)
        self.assertEqual(
            snapshot.capital_basis_failure_reason,
            "indeterminate_positive_treasury_after_later_issued_observation",
        )

    def test_known_valid_old_treasury_still_fails_closed_without_current_provenance(self) -> None:
        """一次資料で旧10株が有効でも、lossy入力だけでは無効例と区別できない。"""

        rows = [
            _summary(
                "4889",
                date(2026, 2, 13),
                shares_outstanding=12_986_700.0,
                treasury_shares=10.0,
            ),
            _summary(
                "4889",
                date(2026, 5, 13),
                shares_outstanding=13_776_900.0,
                treasury_shares=None,
                average_shares=12_878_188.0,
            ),
        ]

        result = _resolve_capital_basis(rows)

        self.assertIsNone(result.shares_ex_treasury)
        self.assertEqual(
            result.failure_reason,
            "indeterminate_positive_treasury_after_later_issued_observation",
        )

    def test_treasury_carry_stops_after_same_gross_issued_is_reobserved(self) -> None:
        """自己株0が欠損になるsourceで、処分前の正値を同じgrossから再控除しない。"""
        asof = date(2026, 7, 1)
        bars = _daily_bars("130A", asof, 40)
        summaries = [
            _summary(
                "130A",
                asof - timedelta(days=300),
                fiscal_period="FY",
                shares_outstanding=1_000.0,
                treasury_shares=100.0,
            ),
            _summary(
                "130A",
                asof - timedelta(days=20),
                shares_outstanding=1_000.0,
                treasury_shares=None,
                average_shares=950.0,
            ),
        ]

        index = build_shares_outstanding_index({"130A": summaries}, {"130A": bars}, asof)
        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        ).financials["130A"]

        self.assertIsNone(index["130A"])
        self.assertIsNone(snapshot.market_cap)
        self.assertEqual(
            snapshot.capital_basis_failure_reason,
            "indeterminate_positive_treasury_after_later_issued_observation",
        )

    def test_treasury_carry_does_not_revive_after_decrease_then_increase(self) -> None:
        """消却前の自己株basisは、後日の増資でgrossが回復しても復活しない。"""
        asof = date(2026, 7, 1)
        bars = _daily_bars("130A", asof, 40)
        summaries = [
            _summary(
                "130A",
                asof - timedelta(days=300),
                fiscal_period="FY",
                shares_outstanding=1_000.0,
                treasury_shares=100.0,
            ),
            _summary(
                "130A",
                asof - timedelta(days=200),
                shares_outstanding=900.0,
                treasury_shares=None,
            ),
            _summary(
                "130A",
                asof - timedelta(days=20),
                shares_outstanding=1_100.0,
                treasury_shares=None,
            ),
        ]

        index = build_shares_outstanding_index({"130A": summaries}, {"130A": bars}, asof)
        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        ).financials["130A"]

        self.assertIsNone(index["130A"])
        self.assertIsNone(snapshot.market_cap)
        self.assertEqual(snapshot.shares_outstanding, 1_100.0)
        self.assertEqual(
            snapshot.capital_basis_failure_reason,
            "indeterminate_positive_treasury_after_later_issued_observation",
        )

    def test_new_treasury_observation_resets_incompatible_prior_basis(self) -> None:
        """消却後の新しい自己株観測があれば、その新資本状態から再開する。"""
        asof = date(2026, 7, 1)
        bars = _daily_bars("130A", asof, 40)
        summaries = [
            _summary(
                "130A",
                asof - timedelta(days=300),
                fiscal_period="FY",
                shares_outstanding=1_000.0,
                treasury_shares=100.0,
            ),
            _summary(
                "130A",
                asof - timedelta(days=200),
                shares_outstanding=900.0,
                treasury_shares=None,
            ),
            _summary(
                "130A",
                asof - timedelta(days=20),
                fiscal_period="FY",
                shares_outstanding=1_100.0,
                treasury_shares=50.0,
            ),
        ]

        index = build_shares_outstanding_index({"130A": summaries}, {"130A": bars}, asof)
        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        ).financials["130A"]

        self.assertEqual(index["130A"], 1_050.0)
        self.assertEqual(snapshot.shares_ex_treasury, 1_050.0)
        self.assertIsNone(snapshot.capital_basis_failure_reason)

    def test_explicit_zero_treasury_resets_incompatible_prior_basis(self) -> None:
        """新しい0株観測は、古い正の自己株stateを安全に置き換える。"""

        rows = [
            _summary(
                "130A",
                date(2025, 12, 11),
                shares_outstanding=39_063_600.0,
                treasury_shares=1_988_126.0,
            ),
            _summary(
                "130A",
                date(2026, 3, 12),
                shares_outstanding=41_194_972.0,
                treasury_shares=None,
            ),
            _summary(
                "130A",
                date(2026, 6, 11),
                shares_outstanding=41_194_972.0,
                treasury_shares=0.0,
            ),
        ]

        result = _resolve_capital_basis(rows)

        self.assertEqual(result.shares_ex_treasury, 41_194_972.0)
        self.assertIsNone(result.failure_reason)

    def test_zero_treasury_carry_continues_across_issued_shares_decrease(self) -> None:
        """自己株0なら発行済が減っても二重控除は起きないので値を維持する。"""
        asof = date(2026, 7, 1)
        bars = _daily_bars("130A", asof, 40)
        summaries = [
            _summary(
                "130A",
                asof - timedelta(days=300),
                fiscal_period="FY",
                shares_outstanding=1_000.0,
                treasury_shares=0.0,
            ),
            _summary(
                "130A",
                asof - timedelta(days=20),
                shares_outstanding=900.0,
                treasury_shares=None,
            ),
        ]

        index = build_shares_outstanding_index({"130A": summaries}, {"130A": bars}, asof)
        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        ).financials["130A"]

        self.assertEqual(index["130A"], 900.0)
        self.assertEqual(snapshot.shares_ex_treasury, 900.0)
        self.assertIsNone(snapshot.capital_basis_failure_reason)

    def test_zero_treasury_carry_does_not_require_issued_source_basis(self) -> None:
        """自己株0ならsource issuedが欠損しても差し引きは安全である。"""
        asof = date(2026, 7, 1)
        bars = _daily_bars("130A", asof, 40)
        treasury_only = replace(
            _summary(
                "130A",
                asof - timedelta(days=300),
                fiscal_period="FY",
                shares_outstanding=1_000.0,
                treasury_shares=0.0,
            ),
            shares_outstanding=None,
        )
        latest_issued = _summary(
            "130A",
            asof - timedelta(days=20),
            shares_outstanding=900.0,
            treasury_shares=None,
        )

        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": [treasury_only, latest_issued]},
            edinet_by_ticker={},
        ).financials["130A"]

        self.assertEqual(snapshot.shares_ex_treasury, 900.0)
        self.assertIsNone(snapshot.capital_basis_failure_reason)

    def test_average_share_alias_shape_has_no_market_cap(self) -> None:
        """期中平均+自己株が過去grossへ戻る既存行から自己株を二重控除しない。"""
        asof = date(2026, 7, 1)
        bars = _daily_bars("130A", asof, 40)
        prior = _summary(
            "130A",
            asof - timedelta(days=300),
            fiscal_period="FY",
            shares_outstanding=1_000.0,
            average_shares=900.0,
            treasury_shares=100.0,
        )
        contaminated = _summary(
            "130A",
            asof - timedelta(days=20),
            fiscal_period="1Q",
            shares_outstanding=900.0,
            average_shares=900.0,
            treasury_shares=100.0,
        )

        summaries = [prior, contaminated]
        index = build_shares_outstanding_index({"130A": summaries}, {"130A": bars}, asof)
        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        ).financials["130A"]

        self.assertIsNone(index["130A"])
        self.assertIsNone(snapshot.market_cap)
        self.assertEqual(
            snapshot.capital_basis_failure_reason,
            "issued_matches_average_with_positive_treasury",
        )

    def test_invalid_capital_values_have_no_market_cap(self) -> None:
        """負の自己株や発行済以上の自己株を有効な株式数へ変換しない。"""
        asof = date(2026, 7, 1)
        bars = _daily_bars("130A", asof, 40)

        for treasury_shares in (-1.0, 1_000.0):
            with self.subTest(treasury_shares=treasury_shares):
                summary = _summary(
                    "130A",
                    asof - timedelta(days=20),
                    fiscal_period="FY",
                    shares_outstanding=1_000.0,
                    treasury_shares=treasury_shares,
                )
                snapshot = build_metrics(
                    asof_date=asof,
                    securities_by_ticker={"130A": _security()},
                    bars_by_ticker={"130A": bars},
                    summaries_by_ticker={"130A": [summary]},
                    edinet_by_ticker={},
                ).financials["130A"]

                self.assertIsNone(snapshot.market_cap)
                self.assertEqual(
                    snapshot.capital_basis_failure_reason,
                    "invalid_issued_or_treasury_shares",
                )

    def test_treasury_carry_requires_issued_basis_at_its_source(self) -> None:
        """自己株観測行の発行済が無ければ後日の発行済との両立を推測しない。"""
        asof = date(2026, 7, 1)
        bars = _daily_bars("130A", asof, 40)
        treasury_only = replace(
            _summary(
                "130A",
                asof - timedelta(days=300),
                fiscal_period="FY",
                shares_outstanding=1_000.0,
                treasury_shares=100.0,
            ),
            shares_outstanding=None,
        )
        latest_issued = _summary(
            "130A",
            asof - timedelta(days=20),
            shares_outstanding=1_100.0,
            treasury_shares=None,
        )

        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": [treasury_only, latest_issued]},
            edinet_by_ticker={},
        ).financials["130A"]

        self.assertIsNone(snapshot.market_cap)
        self.assertEqual(
            snapshot.capital_basis_failure_reason,
            "treasury_observation_without_issued_basis",
        )

    def test_treasury_carry_rejects_invalid_capital_at_its_source(self) -> None:
        """無効行の自己株だけを後日の正常な発行済へcarryしない。"""
        asof = date(2026, 7, 1)
        bars = _daily_bars("130A", asof, 40)
        invalid_source = _summary(
            "130A",
            asof - timedelta(days=300),
            fiscal_period="FY",
            shares_outstanding=1_000.0,
            treasury_shares=1_000.0,
        )
        latest_issued = _summary(
            "130A",
            asof - timedelta(days=20),
            shares_outstanding=1_100.0,
            treasury_shares=None,
        )

        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": [invalid_source, latest_issued]},
            edinet_by_ticker={},
        ).financials["130A"]

        self.assertIsNone(snapshot.market_cap)
        self.assertEqual(
            snapshot.capital_basis_failure_reason,
            "invalid_treasury_source_capital_basis",
        )

    def test_common_equity_uses_assets_and_ratio_from_one_disclosure(self) -> None:
        """PBR の円経路は別々の資本状態の TA と EqAR を掛け合わせない。"""
        asof = date(2026, 6, 1)
        bars = [
            JQuantsDailyBar(
                ticker="130A",
                traded_at=asof - timedelta(days=29 - index),
                close=100.0,
                turnover_value=300_000_000.0,
            )
            for index in range(30)
        ]
        same_state = _summary(
            "130A",
            asof - timedelta(days=100),
            fiscal_period="3Q",
            total_assets=80_000_000_000.0,
            equity_to_asset_ratio=0.6,
        )
        assets_only = replace(
            _summary(
                "130A",
                asof - timedelta(days=10),
                fiscal_period="4Q",
                total_assets=160_000_000_000.0,
                equity_to_asset_ratio=None,
            ),
            bps=None,
        )

        financial = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": [same_state, assets_only]},
            edinet_by_ticker={},
        ).financials["130A"]

        # 40bn market cap / (80bn assets * 0.6 EqAR) = 0.8333。別日の
        # 160bn assets と古い 0.6 を掛けると 0.4167 へ半減する。
        self.assertAlmostEqual(financial.pbr or 0.0, 40_000_000_000 / 48_000_000_000)

    def test_common_equity_prefers_newer_bps_over_older_complete_yen_route(self) -> None:
        """同一行の円経路でも、より新しいBPSより古ければ資本状態を巻き戻さない。"""
        asof = date(2026, 6, 1)
        bars = [
            JQuantsDailyBar(
                ticker="130A",
                traded_at=asof - timedelta(days=29 - index),
                close=1_000.0,
                turnover_value=300_000_000.0,
            )
            for index in range(30)
        ]
        older_complete = replace(
            _summary(
                "130A",
                asof - timedelta(days=100),
                fiscal_period="3Q",
                total_assets=80_000_000_000.0,
                equity_to_asset_ratio=0.5,
                shares_outstanding=1_000_000.0,
                treasury_shares=0.0,
            ),
            bps=40_000.0,
        )
        newer_bps = replace(
            _summary(
                "130A",
                asof - timedelta(days=10),
                fiscal_period="4Q",
                total_assets=160_000_000_000.0,
                equity_to_asset_ratio=None,
                shares_outstanding=1_000_000.0,
                treasury_shares=0.0,
            ),
            bps=20_000.0,
            equity=None,
        )

        financial = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": [older_complete, newer_bps]},
            edinet_by_ticker={},
        ).financials["130A"]

        # 1bn market cap / (20,000 BPS * 1m shares) = 0.05。古い同一行の
        # 80bn assets * 0.5 EqAR を復活させると 0.025 へ半減する。
        self.assertAlmostEqual(financial.pbr or 0.0, 0.05)

    def test_split_crossing_composition_normalizes_per_share_basis(self) -> None:
        """分割を跨ぐ YoY・株数変化は、行を asof 基準へ正規化してから行う。

        1911 型の再現: 前年 Q1 (分割前基準 eps 98.65・株数 206M) → 1:3 分割
        (factor 1/3) → 通期・直近 Q1 は分割後基準。正規化なしだと eps_yoy が
        27.33/98.65 の偽減益、net_share_change_yoy が +200% の偽希薄化になる。

        trailing 倍率は円で合成するので分割 factor を必要としない。円の利益額は分割で
        動かず、1 株当たりへの換算は最後に 1 回だけ行うためである。
        """
        asof = date(2026, 7, 1)
        security = _security()
        bars = []
        for index in range(400):
            traded_at = asof - timedelta(days=399 - index)
            bars.append(
                JQuantsDailyBar(
                    ticker="130A",
                    traded_at=traded_at,
                    close=1328.0,
                    turnover_value=300_000_000.0,
                    adjustment_factor=(1.0 / 3.0 if traded_at == date(2025, 6, 27) else 1.0),
                )
            )
        summaries = [
            _summary(
                "130A",
                date(2025, 4, 30),
                eps_ttm=98.65,
                fiscal_period="1Q",
                fiscal_year_end=date(2025, 12, 31),
                period_start=date(2025, 1, 1),
                period_end=date(2025, 3, 31),
                shares_outstanding=206_068_168.0,
            ),
            _summary(
                "130A",
                date(2026, 2, 13),
                eps_ttm=174.13,
                fiscal_period="FY",
                fiscal_year_end=date(2025, 12, 31),
                period_start=date(2025, 1, 1),
                period_end=date(2025, 12, 31),
                shares_outstanding=618_555_804.0,
            ),
            _summary(
                "130A",
                date(2026, 5, 7),
                eps_ttm=27.33,
                fiscal_period="1Q",
                fiscal_year_end=date(2026, 12, 31),
                period_start=date(2026, 1, 1),
                period_end=date(2026, 3, 31),
                shares_outstanding=618_555_804.0,
            ),
        ]
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": security},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        )
        financial = result.financials["130A"]
        normalized_prior_q1 = 98.65 / 3.0
        expected_profit_ttm = 27.33 * 618_555_804.0 + 174.13 * 618_555_804.0 - 98.65 * 206_068_168.0
        assert financial.per_trailing is not None
        self.assertAlmostEqual(
            financial.per_trailing,
            (1328.0 * 618_555_804.0) / expected_profit_ttm,
            places=6,
        )
        # eps_yoy は正規化済み累計同士 (27.33 vs 32.88) の比較になる。
        assert financial.eps_yoy is not None
        self.assertAlmostEqual(financial.eps_yoy, 27.33 / normalized_prior_q1 - 1.0, places=4)
        # 分割は希薄化ではない: 正規化後の前年株数は 206.07M x 3 = 618.20M 相当で、
        # 変化は実際の微増資分 (+0.057%) だけになる (正規化なしだと +200% の偽希薄化)。
        assert financial.net_share_change_yoy is not None
        self.assertAlmostEqual(
            financial.net_share_change_yoy,
            618_555_804.0 / (206_068_168.0 * 3.0) - 1.0,
            places=6,
        )

    def test_ttm_composition_survives_a_share_count_change_between_periods(self) -> None:
        """株数が期をまたいで動いた会社でも trailing 収益は合成できる。

        4502 型の再現: 買収の新株発行で株数が 783M -> 961M (期中平均) -> 1,556M と動いた
        3 期。1 株当たりで合成すると各項の分母が違うため
        21.32 + 113.50 - 161.76 = -26.94 となり、黒字の会社が赤字に見えて収益 anchor を
        失う。円で合成すれば分母は 1 つで、実際の TTM 純利益が出る。
        """
        asof = date(2019, 11, 15)
        security = _security()
        bars = _daily_bars("130A", asof, 30)
        prior_same_shares, prior_fy_shares, latest_shares = (
            783_061_325.0,
            961_462_555.0,
            1_556_472_795.0,
        )
        summaries = [
            _summary(
                "130A",
                date(2018, 10, 31),
                eps_ttm=161.76,
                shares_outstanding=prior_same_shares,
                fiscal_period="2Q",
                fiscal_year_end=date(2019, 3, 31),
                period_start=date(2018, 4, 1),
                period_end=date(2018, 9, 30),
            ),
            _summary(
                "130A",
                date(2019, 5, 14),
                eps_ttm=113.5,
                shares_outstanding=prior_fy_shares,
                fiscal_period="FY",
                fiscal_year_end=date(2019, 3, 31),
                period_start=date(2018, 4, 1),
                period_end=date(2019, 3, 31),
            ),
            _summary(
                "130A",
                date(2019, 10, 31),
                eps_ttm=21.32,
                shares_outstanding=latest_shares,
                fiscal_period="2Q",
                fiscal_year_end=date(2020, 3, 31),
                period_start=date(2019, 4, 1),
                period_end=date(2019, 9, 30),
            ),
        ]
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": security},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        )
        financial = result.financials["130A"]
        self.assertLess(21.32 + 113.5 - 161.76, 0.0)
        expected_profit_ttm = (
            21.32 * latest_shares + 113.5 * prior_fy_shares - 161.76 * prior_same_shares
        )
        self.assertGreater(expected_profit_ttm, 0.0)
        assert financial.eps is not None
        self.assertAlmostEqual(financial.eps, expected_profit_ttm / latest_shares, places=6)
        latest_close = 100.0 + 29
        assert financial.per_trailing is not None
        self.assertAlmostEqual(
            financial.per_trailing,
            (latest_close * latest_shares) / expected_profit_ttm,
            places=6,
        )

    def test_ttm_composition_refuses_per_share_fields(self) -> None:
        """`直近累計 + 前期通期 - 前年同期間累計` は円の総額でしか成立しない。

        各項が自分の期の株数で割られていると和・差が成立せず、株数が動いた会社で黒字が
        赤字に見える。呼び出し側の誤りとして受け付けない。
        """
        summaries = [
            _summary(
                "130A",
                date(2026, 5, 15),
                fiscal_period="FY",
                period_start=date(2025, 4, 1),
                period_end=date(2026, 3, 31),
            )
        ]
        rules = load_screening_rules()
        for field in ("eps_ttm", "bps", "dps_actual_annual", "forecast_eps"):
            with self.subTest(field=field), self.assertRaises(ValueError) as caught:
                _ttm_value(summaries, field, rules.ttm)
            self.assertIn("per share", str(caught.exception))
        # 円の総額は通る。
        value, quality = _ttm_value(summaries, "profit", rules.ttm)
        self.assertIsNotNone(value)
        self.assertEqual(quality, TTMQuality.EXACT)

    def test_edinet_values_are_refused_when_they_describe_another_entity(self) -> None:
        """8253 型の再現: 単体の貸借対照表を連結の時価総額と組み合わせない。

        抽出器が 1 つの書類を単体基準で読むと、負債・現金・EBITDA が親会社単独の値に
        なる。時価総額と TTM 系列は短信由来なので、比率の分子と分母が別の会社を指す。
        実データでは総資産が 1/1000 になる行がある。両側が総資産を持つので照合できる。
        """
        financial = self._entity_scale_snapshot(
            _edinet_metric_record(
                "130A", total_assets=10_000_000.0, consolidation_basis="non_consolidated"
            )
        )
        self.assertIsNone(financial.net_cash)
        self.assertIsNone(financial.net_cash_to_market_cap)
        self.assertIsNone(financial.debt)
        self.assertIsNone(financial.cash)
        self.assertIsNone(financial.ev_ebitda)
        self.assertIsNone(financial.ttm_quality_ev_ebitda)
        assert financial.edinet_failure_reasons is not None
        self.assertIn("entity_scale_mismatch", financial.edinet_failure_reasons)
        # 実体を判断するための出所は残す。落としたのは値であって記録ではない。
        self.assertEqual(financial.edinet_source_doc_id, "S100TEST")
        self.assertEqual(financial.consolidation_basis, "non_consolidated")
        # 短信由来の指標は残り、銘柄は母集団に留まる。
        self.assertIsNotNone(financial.market_cap)
        self.assertIsNotNone(financial.per_trailing)

    def test_edinet_values_survive_when_the_balance_sheets_agree(self) -> None:
        # 同じ実体を指す行は素通しする。単体基準でも、総資産が一致すれば連結財務諸表を
        # 持たない会社であって取り違えではない。
        for basis in ("consolidated", "non_consolidated"):
            with self.subTest(basis=basis):
                financial = self._entity_scale_snapshot(
                    _edinet_metric_record(
                        "130A", total_assets=10_100_000_000.0, consolidation_basis=basis
                    )
                )
                self.assertEqual(financial.debt, 300.0)
                self.assertIsNotNone(financial.net_cash)
                assert financial.edinet_failure_reasons is None or (
                    "entity_scale_mismatch" not in financial.edinet_failure_reasons
                )

    def test_an_unverifiable_non_consolidated_record_is_refused(self) -> None:
        # 総資産を持たない行は照合できない。単体基準の母集団は実測で 17.6% が桁でずれて
        # おり、どれがずれているかを他の field では言えないので答えない。連結基準は照合
        # できた全行が一致するため素通しする。
        refused = self._entity_scale_snapshot(
            _edinet_metric_record("130A", total_assets=None, consolidation_basis="non_consolidated")
        )
        self.assertIsNone(refused.debt)
        kept = self._entity_scale_snapshot(
            _edinet_metric_record("130A", total_assets=None, consolidation_basis="consolidated")
        )
        self.assertEqual(kept.debt, 300.0)

    def test_accruals_use_the_reported_profit_line_not_a_share_count_product(self) -> None:
        """accruals の純利益は報告値。1 株当たり x 発行済株数で作らない。

        自己株式を 30% 積み上げた会社では、発行済株数を掛けた再構成が純利益を 1.43 倍に
        し、accruals が 0.04 から 0.09 へ動く。判定はこの水準で行うので、再構成の誤差だけ
        で earnings-quality の結論が変わる。
        """
        asof = date(2026, 7, 1)
        security = _security()
        bars = _daily_bars("130A", asof, 30)
        summaries = [
            _summary(
                "130A",
                date(2026, 5, 15),
                eps_ttm=10.0,
                shares_outstanding=100_000_000.0,
                treasury_shares=30_000_000.0,
                cfo=100_000_000.0,
                total_assets=10_000_000_000.0,
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                period_start=date(2025, 4, 1),
                period_end=date(2026, 3, 31),
            )
        ]
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": security},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        )
        financial = result.financials["130A"]
        reported_profit = 10.0 * (100_000_000.0 - 30_000_000.0)
        assert financial.accruals_to_assets is not None
        self.assertAlmostEqual(
            financial.accruals_to_assets,
            (reported_profit - 100_000_000.0) / 10_000_000_000.0,
            places=9,
        )
        reconstructed = 10.0 * 100_000_000.0
        self.assertNotAlmostEqual(
            financial.accruals_to_assets,
            (reconstructed - 100_000_000.0) / 10_000_000_000.0,
            places=3,
        )

    def test_build_metrics_preserves_edinet_source_metadata(self) -> None:
        asof = date(2026, 4, 24)
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": _daily_bars("130A", asof, 800)},
            summaries_by_ticker={"130A": [_summary("130A", asof)]},
            edinet_by_ticker={"130A": _edinet_metric_record()},
        )

        snapshot = result.financials["130A"]
        self.assertEqual(snapshot.edinet_source_doc_id, "S100TEST")
        self.assertEqual(snapshot.edinet_document_type, "120")
        self.assertEqual(snapshot.edinet_source_submit_datetime, "2026-04-01 12:00")
        self.assertEqual(snapshot.edinet_source_period_start, date(2025, 4, 1))
        self.assertEqual(snapshot.edinet_source_period_end, date(2026, 3, 31))

    def test_build_metrics_historical_ev_ebitda_normalizes_split_via_factor(self) -> None:
        # 1:2 分割 (権利落ち = asof、factor 0.5) を跨ぐ価格履歴。調整は不変イベントの
        # adjustment_factor から組む。adjustment_close は incremental cache で
        # 遡及の有無が混在し series にならないため、わざと stale な値 (raw close の
        # まま) を入れて「無視されること」を検証する。
        # 正規化後の履歴 = [160x0.5, 200x0.5, 120] = [80, 100, 120]。
        asof = date(2026, 4, 24)
        bars = [
            JQuantsDailyBar(
                "130A",
                asof - timedelta(days=2),
                close=160.0,
                turnover_value=300_000_000.0,
                adjustment_close=160.0,
            ),
            JQuantsDailyBar(
                "130A",
                asof - timedelta(days=1),
                close=200.0,
                turnover_value=300_000_000.0,
                adjustment_close=200.0,
            ),
            JQuantsDailyBar(
                "130A",
                asof,
                close=120.0,
                turnover_value=300_000_000.0,
                adjustment_close=120.0,
                adjustment_factor=0.5,
            ),
        ]
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={
                "130A": [
                    _summary(
                        "130A",
                        asof,
                        eps_ttm=10.0,
                        fiscal_period="FY",
                        fiscal_year_end=date(2026, 3, 31),
                        shares_outstanding=10.0,
                    )
                ]
            },
            edinet_by_ticker={"130A": _edinet_metric_record()},
        )

        derived = result.derived["130A"]
        # adjusted close history [80, 100, 120] -> EV/EBITDA history [5.0, 6.0, 7.0]
        expected_history = [
            ((price * 10.0) + 300.0 - 100.0) / 200.0 for price in (80.0, 100.0, 120.0)
        ]
        avg = sum(expected_history) / len(expected_history)
        variance = sum((value - avg) ** 2 for value in expected_history) / len(expected_history)
        expected_sigma_gap = (expected_history[-1] - avg) / (variance**0.5)
        self.assertAlmostEqual(derived.sigma_gap["ev_ebitda"], expected_sigma_gap)
        # If raw close were used the second sample (200.0 -> 21/2 = 10.5) would
        # dominate the percentile and push latest off the upper boundary.
        self.assertAlmostEqual(derived.self_range_percentile["ev_ebitda"], 1.0)
