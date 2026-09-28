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
from baibai_engine.market.jquants_models import JQuantsFinancialSummary
from baibai_engine.screening.metrics import (
    build_metrics,
)
from baibai_engine.screening.schema import FinancialSnapshot


class ScreeningMetricsTests(SnapshotFixtures):
    def test_equal_average_without_prior_gross_reconstruction_is_allowed(self) -> None:
        """ShOutFYとAvgShの同値だけでは正常な1Q行を除外しない。"""
        asof = date(2026, 7, 1)
        bars = _daily_bars("130A", asof, 40)
        summaries = [
            _summary(
                "130A",
                asof - timedelta(days=300),
                fiscal_period="FY",
                shares_outstanding=900.0,
                average_shares=899.0,
                treasury_shares=100.0,
            ),
            _summary(
                "130A",
                asof - timedelta(days=20),
                fiscal_period="1Q",
                shares_outstanding=900.0,
                average_shares=900.0,
                treasury_shares=100.0,
            ),
        ]

        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        ).financials["130A"]

        self.assertEqual(snapshot.shares_ex_treasury, 800.0)
        self.assertIsNone(snapshot.capital_basis_failure_reason)

    def test_bs_fields_carry_forward_from_recent_disclosure(self) -> None:
        """bps 等の BS 系 fact は latest の四半期行に無くても直近 FY 行から
        carry-forward され、staleness fact が付く。"""
        asof = date(2026, 1, 30)
        security = _security()
        bars = [
            JQuantsDailyBar(
                ticker="130A",
                traded_at=asof - timedelta(days=29 - index),
                close=100.0,
                turnover_value=300_000_000.0,
            )
            for index in range(30)
        ]
        fy = _summary(
            "130A",
            asof - timedelta(days=200),
            fiscal_period="FY",
            period_start=date(2024, 4, 1),
            period_end=date(2025, 3, 31),
            # 自己資本は円で 4.8e10 (= bps 120 x 自己株控除後 400M) になる値を置く。
            # PBR は円経路で組むので、この 2 つが carry-forward される側になる。
            total_assets=8e10,
            equity_to_asset_ratio=0.6,
        )
        quarterly = JQuantsFinancialSummary(
            ticker="130A",
            disclosed_at=asof - timedelta(days=10),
            forecast_eps=20.0,
            eps_ttm=5.0,
            bps=None,
            shares_outstanding=None,
            sales=250_000_000.0,
            cfo=None,
            cash_eq=None,
            total_assets=None,
            equity=None,
            operating_profit=50_000_000.0,
            ordinary_profit=None,
            profit=None,
            fiscal_period="3Q",
            fiscal_year_end=date(2026, 3, 31),
            period_start=date(2025, 4, 1),
            period_end=date(2025, 12, 31),
        )
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": security},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": [fy, quarterly]},
            edinet_by_ticker={},
        )
        financial = result.financials["130A"]
        # FY 行の総資産と自己資本比率が carry-forward され PBR が計算できる。値は
        # 1 株当たり純資産経由と同じで、恒等式が成り立つ限り経路は答えを変えない。
        assert financial.pbr is not None
        self.assertAlmostEqual(financial.pbr, 100.0 / 120.0, places=6)
        fields = financial.bs_carry_forward_fields or ""
        self.assertIn("total_assets", fields)
        self.assertIn("equity_to_asset_ratio", fields)
        # `bps` は基準の突き合わせに使うので carry 対象のまま残る。
        self.assertIn("bps", fields)
        assert financial.bs_carry_forward_lag_days is not None
        self.assertEqual(financial.bs_carry_forward_lag_days, 190)

    def test_disclosures_after_the_asof_date_do_not_reach_the_snapshot(self) -> None:
        """較正リプレイは過去の断面を作り直す。発表前の決算が 1 行混ざれば全部が偽になる。

        bar は `_latest_bar_on_or_before` が切るので、開示行だけ呼び出し側任せにすると
        「未発表の好決算で割安に見える」行ができる。
        """
        asof = date(2026, 3, 31)
        security = _security()
        bars = _daily_bars("130A", asof, 60)
        disclosed = _summary(
            "130A",
            date(2026, 3, 1),
            eps_ttm=10.0,
            shares_outstanding=100_000_000.0,
            treasury_shares=0.0,
            fiscal_period="FY",
            fiscal_year_end=date(2026, 3, 31),
            period_start=date(2025, 4, 1),
            period_end=date(2026, 3, 31),
        )
        undisclosed = replace(disclosed, disclosed_at=asof + timedelta(days=10), eps_ttm=99.0)

        def snapshot(summaries: list[JQuantsFinancialSummary]) -> FinancialSnapshot:
            return build_metrics(
                asof_date=asof,
                securities_by_ticker={"130A": security},
                bars_by_ticker={"130A": bars},
                summaries_by_ticker={"130A": summaries},
                edinet_by_ticker={},
            ).financials["130A"]

        without = snapshot([disclosed])
        with_future = snapshot([disclosed, undisclosed])
        self.assertEqual(with_future.latest_financial_disclosure_date, date(2026, 3, 1))
        self.assertEqual(with_future.eps, without.eps)
        self.assertEqual(with_future.per_trailing, without.per_trailing)

    def test_build_metrics_adds_short_dislocation_fields(self) -> None:
        asof = date(2026, 4, 24)
        start = asof - timedelta(days=29)
        bars = [
            JQuantsDailyBar(
                ticker="130A",
                traded_at=start + timedelta(days=index),
                close=90.0 if index >= 25 else 100.0,
                adjustment_close=90.0 if index >= 25 else 100.0,
                turnover_value=300_000_000.0 if index >= 25 else 100_000_000.0,
            )
            for index in range(30)
        ]
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": [_summary("130A", asof - timedelta(days=30))]},
            edinet_by_ticker={},
        )

        derived = result.derived["130A"]
        self.assertEqual(derived.price_change_1d, 0.0)
        self.assertAlmostEqual(derived.price_change_5d or 0.0, -0.1)
        self.assertAlmostEqual(derived.price_change_20d or 0.0, -0.1)
        self.assertIsNone(derived.price_change_60d)
        self.assertEqual(derived.gap_from_52w_low, 0.0)
        self.assertAlmostEqual(derived.turnover_spike_5d or 0.0, 3.0)

    def test_build_metrics_historical_ev_ebitda_uses_market_cap_plus_net_debt(self) -> None:
        asof = date(2026, 4, 24)
        bars = [
            JQuantsDailyBar("130A", asof - timedelta(days=2), 80.0, 300_000_000.0),
            JQuantsDailyBar("130A", asof - timedelta(days=1), 100.0, 300_000_000.0),
            JQuantsDailyBar("130A", asof, 120.0, 300_000_000.0),
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

        snapshot = result.financials["130A"]
        derived = result.derived["130A"]
        # Historical EV/EBITDA should be
        # (price * shares + debt - cash) / ebitda, not a collapsed price ratio.
        self.assertIsNotNone(snapshot.ev_ebitda)
        self.assertIsNotNone(derived.self_range_percentile["ev_ebitda"])
        self.assertIsNotNone(derived.sigma_gap["ev_ebitda"])
        self.assertAlmostEqual(snapshot.ev_ebitda, ((120.0 * 10.0) + 300.0 - 100.0) / 200.0)
        self.assertAlmostEqual(derived.self_range_percentile["ev_ebitda"], 1.0)
        history_values = [
            ((price * 10.0) + 300.0 - 100.0) / 200.0 for price in (80.0, 100.0, 120.0)
        ]
        avg = sum(history_values) / len(history_values)
        variance = sum((value - avg) ** 2 for value in history_values) / len(history_values)
        expected_sigma_gap = (history_values[-1] - avg) / (variance**0.5)
        self.assertAlmostEqual(derived.sigma_gap["ev_ebitda"], expected_sigma_gap)

    def test_build_metrics_adds_investment_securities_to_net_cash_ratio(self) -> None:
        asof = date(2026, 4, 24)
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": [JQuantsDailyBar("130A", asof, 120.0, 300_000_000.0)]},
            summaries_by_ticker={
                "130A": [
                    _summary(
                        "130A",
                        asof,
                        fiscal_period="FY",
                        fiscal_year_end=date(2026, 3, 31),
                        shares_outstanding=10.0,
                    )
                ]
            },
            edinet_by_ticker={"130A": _edinet_metric_record(investment_securities=800.0)},
        )

        snapshot = result.financials["130A"]
        self.assertEqual(snapshot.investment_securities, 800.0)
        self.assertAlmostEqual(snapshot.net_cash_to_market_cap or 0.0, -200.0 / 1200.0)
        self.assertAlmostEqual(snapshot.asset_backed_ratio or 0.0, 600.0 / 1200.0)

    def test_asset_backed_ratio_is_null_without_positive_market_cap(self) -> None:
        asof = date(2026, 4, 24)
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": [JQuantsDailyBar("130A", asof, 120.0, 300_000_000.0)]},
            summaries_by_ticker={
                "130A": [
                    _summary(
                        "130A",
                        asof,
                        fiscal_period="FY",
                        fiscal_year_end=date(2026, 3, 31),
                        shares_outstanding=0.0,
                    )
                ]
            },
            edinet_by_ticker={"130A": _edinet_metric_record(investment_securities=800.0)},
        )

        self.assertIsNone(result.financials["130A"].asset_backed_ratio)

    def test_build_metrics_drops_ev_ebitda_when_ev_or_ebitda_is_non_positive(self) -> None:
        asof = date(2026, 4, 24)
        bars = [
            JQuantsDailyBar("130A", asof - timedelta(days=2), 80.0, 300_000_000.0),
            JQuantsDailyBar("130A", asof - timedelta(days=1), 100.0, 300_000_000.0),
            JQuantsDailyBar("130A", asof, 120.0, 300_000_000.0),
        ]
        summaries = {
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
        }

        negative_ebitda = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker=summaries,
            edinet_by_ticker={"130A": _edinet_metric_record(ebitda_ttm=-10.0)},
        )
        self.assertIsNone(negative_ebitda.financials["130A"].ev_ebitda)
        self.assertIsNone(negative_ebitda.derived["130A"].self_range_percentile["ev_ebitda"])
        self.assertIsNone(negative_ebitda.derived["130A"].sigma_gap["ev_ebitda"])

        negative_ev = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker=summaries,
            edinet_by_ticker={
                "130A": _edinet_metric_record(cash=2_000.0, debt=0.0, ebitda_ttm=200.0)
            },
        )
        self.assertIsNone(negative_ev.financials["130A"].ev_ebitda)
        self.assertIsNone(negative_ev.derived["130A"].self_range_percentile["ev_ebitda"])
        self.assertIsNone(negative_ev.derived["130A"].sigma_gap["ev_ebitda"])
