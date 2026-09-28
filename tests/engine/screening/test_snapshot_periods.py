from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from tests.engine.screening.metric_fixtures import (
    _daily_bars,
    _security,
    _summary,
)
from tests.engine.screening.metric_snapshot_fixtures import SnapshotFixtures

from baibai_engine.market.jquants_models import JQuantsFinancialSummary
from baibai_engine.screening.metrics import (
    build_metrics,
    build_normalized_profit_signals,
)
from baibai_engine.screening.metrics.periods import _prior_year_summary, _ttm_value
from baibai_engine.screening.rule_config import load_screening_rules
from baibai_engine.screening.schema import OperatingProfitSource, TTMQuality
from baibai_engine.screening.security_analysis_builder import build_security_analysis_metrics


class ScreeningMetricsTests(SnapshotFixtures):
    def test_profit_yoy_uses_same_field_through_analysis(self) -> None:
        cases = (
            (
                "operating",
                (120, 100, 60),
                (100, 90, 40),
                OperatingProfitSource.OPERATING_PROFIT,
                0.2,
                False,
            ),
            (
                "ordinary_with_prior_operating",
                (None, 100, 60),
                (80, 120, 70),
                OperatingProfitSource.ORDINARY_PROFIT,
                -1 / 6,
                False,
            ),
            (
                "profit_with_prior_operating",
                (None, None, 60),
                (100, 90, 40),
                OperatingProfitSource.PROFIT,
                0.5,
                False,
            ),
            (
                "profit_with_prior_ordinary",
                (None, None, 60),
                (None, 100, 40),
                OperatingProfitSource.PROFIT,
                0.5,
                False,
            ),
            (
                "ordinary_loss_narrowing",
                (None, -20, -25),
                (-10, -30, -35),
                OperatingProfitSource.ORDINARY_PROFIT,
                -1 / 3,
                True,
            ),
            (
                "prior_same_field_zero",
                (None, 100, 60),
                (80, 0, 70),
                OperatingProfitSource.ORDINARY_PROFIT,
                None,
                False,
            ),
            (
                "prior_same_field_missing",
                (None, 100, 60),
                (80, None, 70),
                OperatingProfitSource.ORDINARY_PROFIT,
                None,
                None,
            ),
        )
        asof = date(2026, 8, 31)
        for name, current_values, prior_values, source, yoy, narrowing in cases:
            with self.subTest(name=name):
                prior = _summary(
                    "130A",
                    date(2025, 8, 1),
                    eps_ttm=None,
                    fiscal_year_end=date(2026, 3, 31),
                    period_start=date(2025, 4, 1),
                    period_end=date(2025, 6, 30),
                    operating_profit=prior_values[0],
                    ordinary_profit=prior_values[1],
                    profit=prior_values[2],
                )
                current = _summary(
                    "130A",
                    date(2026, 8, 1),
                    eps_ttm=None,
                    fiscal_year_end=date(2027, 3, 31),
                    period_start=date(2026, 4, 1),
                    period_end=date(2026, 6, 30),
                    operating_profit=current_values[0],
                    ordinary_profit=current_values[1],
                    profit=current_values[2],
                )
                result = build_metrics(
                    asof_date=asof,
                    securities_by_ticker={"130A": _security()},
                    bars_by_ticker={"130A": _daily_bars("130A", asof, 60)},
                    summaries_by_ticker={"130A": [prior, current]},
                    edinet_by_ticker={},
                )
                financial = result.financials["130A"]
                payload = build_security_analysis_metrics(
                    financial,
                    freshness_warning_count=0,
                    derived=result.derived["130A"],
                )
                self.assertIs(financial.operating_profit_source, source)
                self.assertEqual(
                    financial.operating_profit,
                    next(value for value in current_values if value is not None),
                )
                for actual in (financial.operating_profit_yoy, payload["operating_profit_yoy"]):
                    if yoy is None:
                        self.assertIsNone(actual)
                    else:
                        self.assertAlmostEqual(actual, yoy)
                self.assertIs(financial.operating_profit_loss_narrowing, narrowing)
                self.assertIs(payload["operating_profit_loss_narrowing"], narrowing)

    def test_ttm_matches_leap_year_fiscal_and_half_year_periods(self) -> None:
        rules = load_screening_rules()
        cases = (
            (
                date(2023, 2, 28),
                date(2024, 2, 29),
                date(2022, 3, 1),
                date(2022, 3, 1),
                date(2023, 3, 1),
                date(2022, 5, 31),
                date(2023, 5, 31),
            ),
            (
                date(2024, 2, 29),
                date(2025, 2, 28),
                date(2023, 3, 1),
                date(2023, 3, 1),
                date(2024, 3, 1),
                date(2023, 5, 31),
                date(2024, 5, 31),
            ),
            (
                date(2023, 8, 31),
                date(2024, 8, 31),
                date(2022, 9, 1),
                date(2022, 9, 1),
                date(2023, 9, 1),
                date(2023, 2, 28),
                date(2024, 2, 29),
            ),
            (
                date(2024, 1, 31),
                date(2025, 1, 31),
                date(2023, 2, 28),
                date(2023, 2, 1),
                date(2024, 2, 29),
                date(2023, 7, 31),
                date(2024, 7, 31),
            ),
        )
        for (
            prior_end,
            latest_end,
            prior_start,
            prior_fy_start,
            latest_start,
            prior_partial_end,
            latest_partial_end,
        ) in cases:
            with self.subTest(fiscal_year_end=latest_end):
                rows = [
                    _summary(
                        "130A",
                        prior_partial_end + timedelta(days=30),
                        fiscal_year_end=prior_end,
                        period_start=prior_start,
                        period_end=prior_partial_end,
                        sales=20.0,
                        cfo=20.0,
                        operating_profit=20.0,
                        profit=20.0,
                    ),
                    _summary(
                        "130A",
                        prior_end + timedelta(days=40),
                        fiscal_period="FY",
                        fiscal_year_end=prior_end,
                        period_start=prior_fy_start,
                        period_end=prior_end,
                        sales=100.0,
                        cfo=100.0,
                        operating_profit=100.0,
                        profit=100.0,
                    ),
                    _summary(
                        "130A",
                        latest_partial_end + timedelta(days=30),
                        fiscal_year_end=latest_end,
                        period_start=latest_start,
                        period_end=latest_partial_end,
                        sales=30.0,
                        cfo=30.0,
                        operating_profit=30.0,
                        profit=30.0,
                    ),
                ]
                for field in ("sales", "cfo", "operating_profit", "profit"):
                    self.assertEqual(_ttm_value(rows, field, rules.ttm), (110.0, TTMQuality.EXACT))

    def test_prior_year_fallback_matches_february_month_end(self) -> None:
        rows = [
            _summary(
                "130A", date(2024, 4, 1), fiscal_period="FY", fiscal_year_end=date(2024, 2, 29)
            ),
            _summary(
                "130A", date(2025, 4, 1), fiscal_period="FY", fiscal_year_end=date(2025, 2, 28)
            ),
        ]
        self.assertIs(_prior_year_summary(rows, load_screening_rules().ttm), rows[0])

    def test_normalized_profit_preserves_february_fiscal_year_ends(self) -> None:
        summaries = [
            _summary(
                "130A",
                date(year, 4, 10),
                fiscal_period="FY",
                fiscal_year_end=date(year, 2, 29 if year == 2024 else 28),
                eps_ttm=float((year - 2020) * 10),
            )
            for year in range(2021, 2026)
        ]
        result = build_normalized_profit_signals(summaries, (), date(2025, 5, 1), close=1200)
        self.assertEqual(result.normalized_per_3fy, 30.0)
        self.assertEqual(result.normalized_per_5fy, 40.0)

    def test_normalized_profit_does_not_fallback_from_null_revision(self) -> None:
        asof = date(2026, 6, 30)
        summaries = [
            _summary(
                "130A",
                date(year + 1, 5, 10),
                fiscal_period="FY",
                fiscal_year_end=date(year + 1, 3, 31),
                eps_ttm=20.0,
            )
            for year in range(2023, 2026)
        ]
        summaries.append(
            _summary(
                "130A",
                date(2026, 5, 20),
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                eps_ttm=None,
            )
        )

        result = build_normalized_profit_signals(summaries, (), asof, close=200.0)

        self.assertIsNone(result.normalized_per_3fy)

    def test_the_forecast_flags_read_the_pair_of_company_forecasts(self) -> None:
        # 会社予想の (純利益, 経常) の組ごとに、2 つの flag がどうなるかを 1 行で置く。
        # special gain: 純利益>経常利益の注記。原因は断定せず、片方でも欠損なら立てない。
        # full-year loss: どちらか一方が負なら立てる。予想が 1 つも無い行は「黒字予想」で
        # はないので、欠損を黒字へ畳まない。
        cases: tuple[tuple[str, float | None, float | None, bool, bool], ...] = (
            ("net_income_above_ordinary", 5_464_000_000.0, 3_406_000_000.0, True, False),
            ("net_income_below_ordinary", 2_400_000_000.0, 3_406_000_000.0, False, False),
            # 等しい行は「特別益がある」ではない。> を >= へ緩めた変更をここが止める。
            ("net_income_equal_to_ordinary", 3_406_000_000.0, 3_406_000_000.0, False, False),
            ("ordinary_missing", 5_464_000_000.0, None, False, False),
            ("net_income_missing", None, 3_406_000_000.0, False, False),
            # 2491 の 2026-07-29 開示は経常 △700 / 純利益 △800。
            ("both_negative", -800_000_000.0, -700_000_000.0, False, True),
            ("net_income_negative_alone", -800_000_000.0, None, False, True),
            ("ordinary_negative_alone", None, -700_000_000.0, False, True),
            ("both_positive", 480_000_000.0, 1_480_000_000.0, False, False),
            ("no_forecast_disclosed", None, None, False, False),
        )

        for name, profit, ordinary, special_gain, full_year_loss in cases:
            with self.subTest(case=name):
                snapshot = self._forecast_gain_snapshot(
                    forecast_profit=profit, forecast_ordinary_profit=ordinary
                )

                self.assertEqual(snapshot.forecast_special_gain_flag, special_gain)
                self.assertEqual(snapshot.forecast_full_year_loss_flag, full_year_loss)

    def test_per_trailing_rolls_quarterly_cumulative_eps_into_ttm(self) -> None:
        """J-Quants の EPS は期中累計なので、四半期開示直後は rolling 合成で TTM に直す。

        直近が Q1 累計 (24.0) のとき、TTM = Q1 累計 + 前期通期 (114.0) - 前年 Q1 累計 (26.0)
        = 112.0。単一四半期 EPS (24.0) で割った偽の割高 PER を作らない。
        """
        asof = date(2026, 7, 1)
        security = _security()
        bars = _daily_bars("130A", asof, 30)
        summaries = [
            _summary(
                "130A",
                date(2025, 5, 15),
                eps_ttm=26.0,
                fiscal_period="1Q",
                fiscal_year_end=date(2025, 12, 31),
                period_start=date(2025, 1, 1),
                period_end=date(2025, 3, 31),
            ),
            _summary(
                "130A",
                date(2026, 2, 6),
                eps_ttm=114.0,
                fiscal_period="FY",
                fiscal_year_end=date(2025, 12, 31),
                period_start=date(2025, 1, 1),
                period_end=date(2025, 12, 31),
            ),
            _summary(
                "130A",
                date(2026, 5, 15),
                eps_ttm=24.0,
                fiscal_period="1Q",
                fiscal_year_end=date(2026, 12, 31),
                period_start=date(2026, 1, 1),
                period_end=date(2026, 3, 31),
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
        latest_close = 100.0 + 29
        expected_eps_ttm = 24.0 + 114.0 - 26.0
        assert financial.per_trailing is not None
        self.assertAlmostEqual(financial.per_trailing, latest_close / expected_eps_ttm, places=5)
        self.assertEqual(financial.ttm_quality_per_trailing.value, "exact")

    def test_nonactual_notices_preserve_ttm_but_not_withdrawn_forecast(self) -> None:
        asof = date(2026, 9, 7)
        rows = self._ttm_actual_periods()
        rules = load_screening_rules()
        for forecast in (None, 100.0):
            notice = JQuantsFinancialSummary(
                ticker="4812",
                disclosed_at=date(2026, 8, 28),
                fiscal_period="FY",
                fiscal_year_end=date(2026, 12, 31),
                period_start=date(2026, 1, 1),
                period_end=date(2026, 12, 31),
                dps_forecast_annual=22.5,
                forecast_eps=forecast,
            )
            with self.subTest(forecast=forecast):
                for field, expected in (
                    ("sales", 173_275_000_000.0),
                    ("profit", 17_569_000_000.0),
                    ("operating_profit", 17_569_000_000.0),
                    ("cfo", 35_138_000_000.0),
                ):
                    self.assertEqual(
                        _ttm_value([*rows, notice], field, rules.ttm), (expected, TTMQuality.EXACT)
                    )
                snapshot = build_metrics(
                    asof_date=asof,
                    securities_by_ticker={"4812": _security("4812")},
                    bars_by_ticker={"4812": _daily_bars("4812", asof, 40)},
                    summaries_by_ticker={"4812": [*rows, notice]},
                    edinet_by_ticker={},
                ).financials["4812"]
                self.assertAlmostEqual(
                    snapshot.per_trailing, 139.0 * 200_000_000.0 / 17_569_000_000.0
                )
                self.assertAlmostEqual(snapshot.p_s, 139.0 * 200_000_000.0 / 173_275_000_000.0)
                self.assertEqual(snapshot.latest_financial_disclosure_date, notice.disclosed_at)
                if forecast is None:
                    self.assertIsNone(snapshot.per_forward)
                else:
                    self.assertAlmostEqual(snapshot.per_forward, 139.0 / forecast)
                self.assertAlmostEqual(snapshot.dividend_yield, 22.5 / 139.0)

    def test_ttm_does_not_fill_missing_actual_operand_from_older_revision(self) -> None:
        rules = load_screening_rules()
        for missing_operand in range(3):
            for field in ("sales", "profit", "cfo", "operating_profit"):
                with self.subTest(operand=missing_operand, field=field):
                    rows = self._ttm_actual_periods()
                    correction = replace(
                        rows[missing_operand],
                        disclosed_at=rows[missing_operand].disclosed_at + timedelta(days=1),
                        **{field: None},
                    )
                    rows.insert(missing_operand + 1, correction)
                    self.assertEqual(
                        _ttm_value(rows, field, rules.ttm), (None, TTMQuality.UNAVAILABLE)
                    )

    def test_ttm_late_historical_corrections_keep_latest_accounting_period(self) -> None:
        rules = load_screening_rules()
        for operand in (0, 1, 2):
            for field in ("sales", "profit", "cfo", "operating_profit"):
                for missing in (False, True):
                    for reverse in (False, True):
                        with self.subTest(
                            operand=operand, field=field, missing=missing, reverse=reverse
                        ):
                            rows = self._ttm_actual_periods()
                            original = (
                                rows[operand]
                                if operand < 2
                                else replace(
                                    rows[1],
                                    fiscal_year_end=date(2024, 12, 31),
                                    period_start=date(2024, 1, 1),
                                    period_end=date(2024, 12, 31),
                                )
                            )
                            correction = replace(
                                original,
                                disclosed_at=date(2026, 8, 28),
                                **{
                                    field: None if missing else getattr(original, field) + 1_000_000
                                },
                            )
                            expected = (
                                getattr(rows[2], field)
                                + getattr(rows[1], field)
                                - getattr(rows[0], field)
                            )
                            if operand < 2:
                                expected = (
                                    None
                                    if missing
                                    else expected + (-1_000_000 if operand == 0 else 1_000_000)
                                )
                            inputs = [*rows, correction]
                            if reverse:
                                inputs.reverse()
                            self.assertEqual(
                                _ttm_value(inputs, field, rules.ttm),
                                (
                                    expected,
                                    TTMQuality.UNAVAILABLE
                                    if expected is None
                                    else TTMQuality.EXACT,
                                ),
                            )

    def test_ttm_missing_latest_field_does_not_fall_back_to_late_historical_correction(
        self,
    ) -> None:
        rules = load_screening_rules()
        for field in ("sales", "profit", "cfo", "operating_profit"):
            with self.subTest(field=field):
                rows = self._ttm_actual_periods()
                latest_revision = replace(rows[-1], disclosed_at=date(2026, 8, 1), **{field: None})
                old_fy_correction = replace(rows[1], disclosed_at=date(2026, 8, 28))
                self.assertEqual(
                    _ttm_value([*rows, latest_revision, old_fy_correction], field, rules.ttm),
                    (None, TTMQuality.UNAVAILABLE),
                )

    def test_ttm_new_actual_period_missing_field_stays_unavailable(self) -> None:
        rules = load_screening_rules()
        rows = self._ttm_actual_periods()
        rows.append(
            replace(
                rows[-1],
                disclosed_at=date(2026, 10, 29),
                fiscal_period="3Q",
                period_end=date(2026, 9, 30),
                profit=None,
            )
        )
        self.assertEqual(_ttm_value(rows, "profit", rules.ttm), (None, TTMQuality.UNAVAILABLE))
        self.assertEqual(_ttm_value([], "profit", rules.ttm), (None, TTMQuality.UNAVAILABLE))

    def test_price_over_eps_equals_the_trailing_multiple(self) -> None:
        """同じ語が 2 つの値を指さない: `market_price_yen / eps` は `per_trailing` に一致する。

        eps は時価総額と同じ資本分母 (発行済 - 自己株) で組み直した値なので、この等式は
        自己株式を積み上げた会社でも成立する。
        """
        asof = date(2026, 7, 1)
        security = _security()
        bars = _daily_bars("130A", asof, 30)
        summaries = [
            _summary(
                "130A",
                date(2026, 5, 15),
                eps_ttm=40.0,
                shares_outstanding=100_000_000.0,
                treasury_shares=30_000_000.0,
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
        assert financial.eps is not None
        assert financial.per_trailing is not None
        assert financial.market_price_yen is not None
        assert financial.market_cap is not None
        self.assertAlmostEqual(
            financial.market_price_yen / financial.eps, financial.per_trailing, places=9
        )
        # 報告 1 株当たり当期純利益は期中平均株式数が分母なので、期末の資本分母で組み直した
        # eps とは一致しなくてよい。倍率の分母は時価総額と揃っている側である。
        self.assertAlmostEqual(financial.eps, 40.0, places=9)

    def test_per_trailing_is_null_when_ttm_composition_unavailable(self) -> None:
        """前期通期・前年同期間が無く合成できないときは per_trailing を出さない。"""
        asof = date(2026, 7, 1)
        security = _security()
        bars = _daily_bars("130A", asof, 30)
        summaries = [
            _summary(
                "130A",
                date(2026, 5, 15),
                eps_ttm=24.0,
                fiscal_period="1Q",
                fiscal_year_end=date(2026, 12, 31),
                period_start=date(2026, 1, 1),
                period_end=date(2026, 3, 31),
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
        self.assertIsNone(financial.per_trailing)
        self.assertEqual(financial.ttm_quality_per_trailing.value, "unavailable")

    def test_build_metrics_compares_yoy_with_prior_fiscal_period(self) -> None:
        asof = date(2026, 4, 24)
        summaries = [
            _summary(
                "130A",
                date(2025, 4, 24),
                eps_ttm=10.0,
                sales=100.0,
                cfo=20.0,
                operating_profit=20.0,
                fiscal_period="1Q",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2025, 7, 24),
                eps_ttm=40.0,
                sales=400.0,
                operating_profit=80.0,
                fiscal_period="2Q",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2025, 10, 24),
                eps_ttm=50.0,
                sales=500.0,
                operating_profit=100.0,
                fiscal_period="3Q",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2026, 1, 24),
                eps_ttm=60.0,
                sales=600.0,
                operating_profit=120.0,
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2026, 4, 24),
                eps_ttm=15.0,
                sales=125.0,
                cfo=25.0,
                operating_profit=25.0,
                fiscal_period="1Q",
                fiscal_year_end=date(2027, 3, 31),
            ),
        ]
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": _daily_bars("130A", asof, 800)},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        )
        snapshot = result.financials["130A"]
        self.assertIsNotNone(snapshot.eps_yoy)
        self.assertIsNotNone(snapshot.sales_yoy)
        self.assertIsNotNone(snapshot.operating_profit_yoy)
        self.assertIsNotNone(snapshot.cfo_yoy)
        assert snapshot.eps_yoy is not None
        assert snapshot.sales_yoy is not None
        assert snapshot.operating_profit_yoy is not None
        assert snapshot.cfo_yoy is not None
        # Old QoQ logic would compare EPS with the previous disclosure (60.0)
        # and produce -0.75. Fiscal-period matching locks YoY at +0.50.
        self.assertAlmostEqual(snapshot.eps_yoy, 0.5)
        self.assertAlmostEqual(snapshot.sales_yoy, 0.25)
        self.assertAlmostEqual(snapshot.operating_profit_yoy, 0.25)
        self.assertAlmostEqual(snapshot.cfo_yoy, 0.25)

    def test_build_metrics_sets_yoy_to_none_when_prior_year_summary_is_missing(self) -> None:
        asof = date(2026, 4, 24)
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": _daily_bars("130A", asof, 800)},
            summaries_by_ticker={
                "130A": [
                    _summary(
                        "130A",
                        date(2025, 7, 24),
                        fiscal_period="2Q",
                        fiscal_year_end=date(2026, 3, 31),
                    ),
                    _summary(
                        "130A",
                        date(2025, 10, 24),
                        fiscal_period="3Q",
                        fiscal_year_end=date(2026, 3, 31),
                    ),
                    _summary(
                        "130A",
                        date(2026, 1, 24),
                        fiscal_period="FY",
                        fiscal_year_end=date(2026, 3, 31),
                    ),
                    _summary(
                        "130A",
                        date(2026, 4, 24),
                        fiscal_period="1Q",
                        fiscal_year_end=date(2027, 3, 31),
                    ),
                ]
            },
            edinet_by_ticker={},
        )
        snapshot = result.financials["130A"]
        self.assertIsNone(snapshot.eps_yoy)
        self.assertIsNone(snapshot.sales_yoy)
        self.assertIsNone(snapshot.operating_profit_yoy)

    def test_build_metrics_ignores_qoq_seasonality_for_yoy_deterioration(self) -> None:
        asof = date(2026, 4, 24)
        summaries = [
            _summary(
                "130A",
                date(2025, 4, 24),
                eps_ttm=10.0,
                sales=100.0,
                operating_profit=20.0,
                fiscal_period="1Q",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2025, 7, 24),
                eps_ttm=20.0,
                sales=200.0,
                operating_profit=40.0,
                fiscal_period="2Q",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2025, 10, 24),
                eps_ttm=30.0,
                sales=300.0,
                operating_profit=60.0,
                fiscal_period="3Q",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2026, 1, 24),
                eps_ttm=80.0,
                sales=800.0,
                operating_profit=160.0,
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2026, 4, 24),
                eps_ttm=10.0,
                sales=100.0,
                operating_profit=20.0,
                fiscal_period="1Q",
                fiscal_year_end=date(2027, 3, 31),
            ),
        ]
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": _daily_bars("130A", asof, 800)},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        )
        snapshot = result.financials["130A"]
        self.assertEqual(snapshot.eps_yoy, 0.0)
        self.assertEqual(snapshot.sales_yoy, 0.0)
        self.assertEqual(snapshot.operating_profit_yoy, 0.0)

    def test_build_metrics_uses_fiscal_period_match_when_extra_disclosure_shifts_index(
        self,
    ) -> None:
        asof = date(2026, 4, 24)
        summaries = [
            _summary(
                "130A",
                date(2025, 4, 24),
                eps_ttm=10.0,
                fiscal_period="1Q",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2025, 6, 1),
                eps_ttm=999.0,
                fiscal_period="OTHER",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2025, 7, 24),
                eps_ttm=20.0,
                fiscal_period="2Q",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2025, 10, 24),
                eps_ttm=30.0,
                fiscal_period="3Q",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2026, 1, 24),
                eps_ttm=40.0,
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2026, 4, 24),
                eps_ttm=15.0,
                fiscal_period="1Q",
                fiscal_year_end=date(2027, 3, 31),
            ),
        ]
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": _daily_bars("130A", asof, 800)},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        )
        self.assertEqual(result.financials["130A"].eps_yoy, 0.5)

    def test_build_metrics_requires_same_prior_fiscal_year_end_for_yoy(self) -> None:
        asof = date(2026, 4, 24)
        summaries = [
            _summary(
                "130A",
                date(2025, 4, 24),
                eps_ttm=10.0,
                fiscal_period="1Q",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2025, 7, 24),
                eps_ttm=20.0,
                fiscal_period="2Q",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2025, 10, 24),
                eps_ttm=30.0,
                fiscal_period="3Q",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2026, 1, 24),
                eps_ttm=40.0,
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2026, 4, 24),
                eps_ttm=15.0,
                fiscal_period="1Q",
                fiscal_year_end=date(2027, 5, 31),
            ),
        ]
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": _daily_bars("130A", asof, 800)},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        )
        self.assertIsNone(result.financials["130A"].eps_yoy)
