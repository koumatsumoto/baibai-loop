"""旧較正リプレイの合成と共有計算の回帰。現行五モデルの候補gateではない。"""

from __future__ import annotations

import unittest
from datetime import date

import pytest
from tests.engine.screening.metric_fixtures import _daily_bars, _security, _split_bar, _summary

from baibai_engine.machine_analysis.models.company_financials.metrics.capital import (
    _normalize_summaries_to_asof_basis,
    _normalize_summaries_with_status,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.dividends import (
    _resolve_dividend_carry,
    build_shareholder_return_change_signals,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import FinancialSnapshot
from baibai_engine.machine_analysis.historical_evaluation.forward import (
    _asof_basis_dividend as _forward_asof_basis_dividend,
)
from baibai_engine.machine_analysis.historical_evaluation.forward import (
    _FYDividendObservation,
)
from baibai_engine.machine_analysis.screening.metric_builder import build_metrics
from baibai_engine.machine_analysis.observations.prices.price_models import JQuantsDailyBar


class DividendCarryResolverTests(unittest.TestCase):
    """Current-period guidance and explicitly bounded actual dividend bases."""

    def test_zero_forecast_outranks_positive_actual(self) -> None:
        summaries = [
            _summary(
                "130A",
                date(2025, 5, 10),
                fiscal_period="FY",
                fiscal_year_end=date(2025, 3, 31),
                period_start=date(2024, 4, 1),
                period_end=date(2025, 3, 31),
                dps_actual_annual=50.0,
            ),
            _summary("130A", date(2025, 8, 10), dps_forecast_annual=0.0),
        ]
        carry = _resolve_dividend_carry(summaries, [], 1000.0, date(2025, 9, 1))
        self.assertEqual(carry.dividend_yield, 0.0)
        self.assertEqual(carry.basis, "forecast_annual")
        self.assertEqual(carry.dps_actual_annual, 50.0)
        self.assertEqual(carry.dps_forecast_annual, 0.0)

    def test_zero_forecast_survives_post_disclosure_split(self) -> None:
        summary = _summary(
            "130A", date(2025, 5, 10), dps_actual_annual=50.0, dps_forecast_annual=0.0
        )
        split = [_split_bar("130A", date(2025, 7, 1), 0.5)]
        normalized = _normalize_summaries_with_status([summary], split, date(2025, 8, 1))
        self.assertEqual(normalized.summaries[0].dps_forecast_annual, 0.0)
        self.assertEqual(normalized.summaries[0].dps_actual_annual, 50.0)
        carry = _resolve_dividend_carry(normalized.summaries, split, 1000.0, date(2025, 8, 1))
        self.assertEqual(carry.dividend_yield, 0.0)
        self.assertEqual(carry.basis, "forecast_annual")

    def test_zero_actual_allows_new_positive_forecast(self) -> None:
        summaries = [
            _summary("130A", date(2025, 5, 10), dps_actual_annual=0.0),
            _summary("130A", date(2025, 8, 10), dps_forecast_annual=10.0),
        ]
        carry = _resolve_dividend_carry(summaries, [], 1000.0, date(2025, 9, 1))
        self.assertEqual(carry.dividend_yield, 0.01)
        self.assertEqual(carry.basis, "forecast_annual")

    def test_prefers_forecast_over_actual(self) -> None:
        # 上限内の予想 DPS は実績より優先し、分割後基準の予想で利回りを出す。
        summaries = [
            _summary("5445", date(2026, 5, 7), dps_actual_annual=300.0, dps_forecast_annual=100.0),
            _summary("5445", date(2025, 5, 7), dps_actual_annual=375.0),
        ]
        carry = _resolve_dividend_carry(
            summaries,
            [_split_bar("5445", date(2026, 3, 30), 1.0 / 3.0)],
            latest_price=1911.0,
            asof_date=date(2026, 7, 10),
        )
        self.assertEqual(carry.basis, "forecast_annual")
        assert carry.dividend_yield is not None
        self.assertAlmostEqual(carry.dividend_yield, 100.0 / 1911.0, places=6)

    def test_a_forecast_above_twice_actual_uses_actual_carry(self) -> None:
        """3659 型: 一回性の分配を 5 年反復する収益として順位へ入れない。"""
        summaries = [
            _summary("3659", date(2026, 8, 13), dps_forecast_annual=475.0),
            _summary(
                "3659",
                date(2026, 2, 12),
                fiscal_period="FY",
                fiscal_year_end=date(2025, 12, 31),
                period_start=date(2025, 1, 1),
                period_end=date(2025, 12, 31),
                dps_actual_annual=45.0,
            ),
        ]

        carry = _resolve_dividend_carry(
            summaries, [], latest_price=3148.0, asof_date=date(2026, 8, 28)
        )

        self.assertEqual(carry.basis, "actual_reported")
        self.assertEqual(carry.dps_forecast_annual, 475.0)
        self.assertEqual(carry.dps_actual_annual, 45.0)
        assert carry.dividend_yield is not None
        self.assertAlmostEqual(carry.dividend_yield, 45.0 / 3148.0, places=9)

    def test_a_forecast_exactly_twice_actual_stays_forecast_carry(self) -> None:
        summaries = [
            _summary("1111", date(2026, 8, 13), dps_forecast_annual=90.0),
            _summary(
                "1111",
                date(2026, 2, 12),
                fiscal_period="FY",
                fiscal_year_end=date(2025, 12, 31),
                period_start=date(2025, 1, 1),
                period_end=date(2025, 12, 31),
                dps_actual_annual=45.0,
            ),
        ]

        carry = _resolve_dividend_carry(
            summaries, [], latest_price=1000.0, asof_date=date(2026, 8, 28)
        )

        self.assertEqual(carry.basis, "forecast_annual")
        assert carry.dividend_yield is not None
        self.assertAlmostEqual(carry.dividend_yield, 90.0 / 1000.0, places=9)

    def test_a_forecast_newer_than_the_actual_still_answers(self) -> None:
        # 対象期が解決済みの予想は、無配実績があっても配当再開として計算できる。
        summaries = [
            _summary("8798", date(2025, 11, 27), dps_actual_annual=0.0),
            _summary("8798", date(2026, 2, 13), dps_forecast_annual=6.0),
        ]
        carry = _resolve_dividend_carry(
            summaries, [], latest_price=130.0, asof_date=date(2026, 8, 11)
        )
        self.assertEqual(carry.basis, "forecast_annual")
        assert carry.dividend_yield is not None
        self.assertAlmostEqual(carry.dividend_yield, 6.0 / 130.0, places=9)

    def test_a_forecast_answers_before_any_actual_is_disclosed(self) -> None:
        # 上書きすべき実績が 1 つも無い銘柄 (実績開示前の新規上場) は予想をそのまま使う。
        summaries = [_summary("130A", date(2026, 5, 15), dps_forecast_annual=12.0)]
        carry = _resolve_dividend_carry(
            summaries, [], latest_price=600.0, asof_date=date(2026, 8, 11)
        )
        self.assertEqual(carry.basis, "forecast_annual")
        assert carry.dividend_yield is not None
        self.assertAlmostEqual(carry.dividend_yield, 12.0 / 600.0, places=9)

    def test_old_period_correction_keeps_current_guidance(self) -> None:
        """9257: 2024年期の遅い訂正は、2026年期予想を失効させない。"""
        summaries = [
            _summary(
                "9257",
                date(2026, 2, 13),
                fiscal_period="FY",
                fiscal_year_end=date(2025, 12, 31),
                period_start=date(2025, 1, 1),
                period_end=date(2025, 12, 31),
                dps_actual_annual=14.09,
                dps_forecast_annual=19.11,
            ),
            _summary(
                "9257",
                date(2026, 5, 15),
                fiscal_period="1Q",
                fiscal_year_end=date(2026, 12, 31),
                period_end=date(2026, 3, 31),
                dps_forecast_annual=19.19,
            ),
            _summary(
                "9257",
                date(2026, 6, 24),
                fiscal_period="FY",
                fiscal_year_end=date(2024, 12, 31),
                period_start=date(2024, 1, 1),
                period_end=date(2024, 12, 31),
                dps_actual_annual=0.0,
            ),
        ]
        for rows in (summaries, list(reversed(summaries))):
            with self.subTest(disclosure_order=[row.disclosed_at for row in rows]):
                carry = _resolve_dividend_carry(rows, [], 1000.0, date(2026, 6, 24))
                self.assertEqual(carry.basis, "forecast_annual")
                self.assertEqual(carry.dps_actual_annual, 14.09)
                self.assertEqual(carry.dps_forecast_annual, 19.19)
                self.assertAlmostEqual(carry.dividend_yield or 0.0, 0.01919)

    def test_null_dps_does_not_establish_a_withdrawal(self) -> None:
        summaries = [
            _summary(
                "130A",
                date(2026, 5, 10),
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                period_start=date(2025, 4, 1),
                period_end=date(2026, 3, 31),
                dps_actual_annual=30.0,
            ),
            _summary("130A", date(2026, 5, 15), dps_forecast_annual=40.0),
        ]
        for period, year_end in (("FY", date(2026, 3, 31)), ("1Q", date(2027, 3, 31))):
            with self.subTest(period=period):
                # 配当予想が記載されていない文書は、既存の対象期予想を撤回しない。
                null_row = _summary(
                    "130A",
                    date(2026, 5, 20),
                    fiscal_period=period,
                    fiscal_year_end=year_end,
                )
                carry = _resolve_dividend_carry(
                    [*summaries, null_row], [], 1000.0, date(2026, 5, 20)
                )
                self.assertEqual(carry.basis, "forecast_annual")
                self.assertEqual(carry.dps_actual_annual, 30.0)
                self.assertEqual(carry.dps_forecast_annual, 40.0)

    def test_normalized_null_forecast_does_not_restore_raw_positive_value(self) -> None:
        rows = [
            _summary(
                "130A",
                date(2026, 5, 10),
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                period_start=date(2025, 4, 1),
                period_end=date(2026, 3, 31),
                dps_actual_annual=40.0,
                dps_forecast_annual=50.0,
            )
        ]
        split = [_split_bar("130A", date(2026, 7, 1), 0.5)]
        asof = date(2026, 8, 1)
        normalized = _normalize_summaries_to_asof_basis(rows, split, asof)
        self.assertIsNone(normalized[0].dps_forecast_annual)
        carry = _resolve_dividend_carry(normalized, split, 1000.0, asof)
        self.assertEqual(carry.basis, "unresolved_split_basis")
        self.assertIsNone(carry.dps_forecast_annual)
        self.assertIsNone(carry.dividend_yield)

    def test_an_unresolved_actual_basis_does_not_suppress_a_valid_forecast(self) -> None:
        """実績を同じ株式基準へ揃えられなければ、倍率guardを適用しない。"""
        summaries = [
            _summary(
                "5445",
                date(2026, 5, 7),
                dps_actual_annual=300.0,
                dps_forecast_annual=100.0,
            )
        ]

        carry = _resolve_dividend_carry(
            summaries,
            [_split_bar("5445", date(2026, 3, 30), 1.0 / 3.0)],
            latest_price=1911.0,
            asof_date=date(2026, 7, 10),
        )

        self.assertEqual(carry.basis, "forecast_annual")
        self.assertIsNone(carry.dps_actual_annual)
        self.assertEqual(carry.dps_forecast_annual, 100.0)
        assert carry.dividend_yield is not None
        self.assertAlmostEqual(carry.dividend_yield, 100.0 / 1911.0, places=9)

    def test_a_same_year_correction_does_not_collapse_the_detection_window(self) -> None:
        # 判別窓の起点は当期会計期間の開始日。前期開示日を起点にすると、同一年度の訂正開示が
        # 直前に来た行で窓が 2 日へ縮み、窓内の分割が窓の外へ出て報告値がそのまま通る。
        summaries = [
            _summary(
                "3382",
                date(2024, 4, 12),
                fiscal_period="FY",
                fiscal_year_end=date(2024, 2, 29),
                period_end=date(2024, 2, 29),
                period_start=date(2023, 3, 1),
                dps_actual_annual=113.0,
                dividend_interim=56.5,
                dividend_year_end=56.5,
            ),
            _summary(
                "3382",
                date(2024, 4, 10),
                fiscal_period="FY",
                fiscal_year_end=date(2024, 2, 29),
                period_end=date(2024, 2, 29),
                period_start=date(2023, 3, 1),
                dps_actual_annual=113.0,
            ),
        ]
        carry = _resolve_dividend_carry(
            summaries,
            [_split_bar("3382", date(2024, 2, 28), 1.0 / 3.0)],
            latest_price=2000.0,
            asof_date=date(2024, 6, 30),
        )
        self.assertNotEqual(carry.basis, "actual_reported")
        self.assertIsNone(carry.dividend_yield)

    def test_a_year_with_no_dividend_is_answered_even_across_a_split(self) -> None:
        # 明示された0円は分割後も0円。未観測DPSや基準不明の正のDPSとは区別する。
        summaries = [
            _summary(
                "7369",
                date(2026, 5, 15),
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                period_end=date(2026, 3, 31),
                period_start=date(2025, 4, 1),
                dps_actual_annual=0.0,
            ),
            _summary("7369", date(2025, 5, 15), dps_actual_annual=0.0),
        ]
        carry = _resolve_dividend_carry(
            summaries,
            [_split_bar("7369", date(2025, 10, 1), 0.5)],
            latest_price=1000.0,
            asof_date=date(2026, 8, 10),
        )
        self.assertEqual(carry.dividend_yield, 0.0)
        self.assertEqual(carry.basis, "actual_reported")
        self.assertEqual(carry.split_factor, 0.5)

    def test_uses_actual_reported_when_no_split_and_no_forecast(self) -> None:
        summaries = [
            _summary(
                "7203",
                date(2026, 5, 7),
                dps_actual_annual=50.0,
                dps_forecast_annual=None,
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                period_start=date(2025, 4, 1),
                period_end=date(2026, 3, 31),
            ),
            _summary(
                "7203",
                date(2025, 5, 7),
                dps_actual_annual=45.0,
                dps_forecast_annual=None,
                fiscal_period="FY",
                fiscal_year_end=date(2025, 3, 31),
                period_start=date(2024, 4, 1),
                period_end=date(2025, 3, 31),
            ),
        ]
        carry = _resolve_dividend_carry(
            summaries, [], latest_price=1000.0, asof_date=date(2026, 7, 10)
        )
        self.assertEqual(carry.basis, "actual_reported")
        self.assertIsNone(carry.split_factor)
        assert carry.dividend_yield is not None
        self.assertAlmostEqual(carry.dividend_yield, 50.0 / 1000.0, places=6)

    def test_none_when_no_dividend_data(self) -> None:
        summaries = [_summary("9999", date(2026, 5, 7))]
        carry = _resolve_dividend_carry(
            summaries, [], latest_price=1000.0, asof_date=date(2026, 7, 10)
        )
        self.assertEqual(carry.basis, "unavailable")
        self.assertIsNone(carry.dividend_yield)


class TradableShareChangeTest(unittest.TestCase):
    """取得年と消却年で 2 つの株数軸が別々に動くことを固定する。

    日本の自社株買いは取得した株式を自己株式へ入れるだけで、発行済株式総数は消却するまで
    減らない。carry の株数項が発行済で測られている限り、還元が起きた年は 0 で、現金の動かない
    消却年に符号が付く。当時の比較結果は既存 carry の維持として記録済み
    (docs/reference/company-valuation.md#carry-policy)。ここでは 2 軸が別物であることを
    固定する。
    """

    ASOF = date(2026, 6, 30)

    def _snapshot(
        self,
        *,
        prior_issued: float,
        prior_treasury: float | None,
        issued: float,
        treasury: float | None,
    ) -> FinancialSnapshot:
        summaries = [
            _summary(
                "130A",
                date(2025, 5, 12),
                fiscal_period="FY",
                fiscal_year_end=date(2025, 3, 31),
                period_start=date(2024, 4, 1),
                period_end=date(2025, 3, 31),
                shares_outstanding=prior_issued,
                treasury_shares=prior_treasury,
            ),
            _summary(
                "130A",
                date(2026, 5, 12),
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                period_start=date(2025, 4, 1),
                period_end=date(2026, 3, 31),
                shares_outstanding=issued,
                treasury_shares=treasury,
            ),
        ]
        result = build_metrics(
            asof_date=self.ASOF,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": _daily_bars("130A", self.ASOF, 400)},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        )
        return result.financials["130A"]

    def test_a_buyback_year_moves_only_the_treasury_excluded_count(self) -> None:
        """自己株を 10% 積んだ年。発行済は動かないので旧推計の carry 項は 0 になる。"""

        financial = self._snapshot(
            prior_issued=400_000_000.0,
            prior_treasury=0.0,
            issued=400_000_000.0,
            treasury=40_000_000.0,
        )

        assert financial.net_share_change_yoy is not None
        self.assertAlmostEqual(financial.net_share_change_yoy, 0.0, places=9)
        assert financial.tradable_share_change_yoy is not None
        self.assertAlmostEqual(financial.tradable_share_change_yoy, -0.10, places=9)

    def test_a_cancellation_year_moves_only_the_issued_count(self) -> None:
        """積んだ自己株を消却した年。現金は動かないのに旧推計の carry 項だけが符号を持つ。"""

        financial = self._snapshot(
            prior_issued=400_000_000.0,
            prior_treasury=40_000_000.0,
            issued=360_000_000.0,
            treasury=0.0,
        )

        assert financial.net_share_change_yoy is not None
        self.assertAlmostEqual(financial.net_share_change_yoy, -0.10, places=9)
        assert financial.tradable_share_change_yoy is not None
        self.assertAlmostEqual(financial.tradable_share_change_yoy, 0.0, places=9)

    def test_an_unobservable_treasury_count_leaves_the_new_axis_unanswered(self) -> None:
        """自己株式数の欠損を 0 で埋めると自己株ゼロを捏造する。答えない側に倒す。"""

        financial = self._snapshot(
            prior_issued=400_000_000.0,
            prior_treasury=None,
            issued=400_000_000.0,
            treasury=40_000_000.0,
        )

        self.assertIsNone(financial.tradable_share_change_yoy)
        # 発行済側は前年行から答えられるので、旧軸は残る。
        assert financial.net_share_change_yoy is not None
        self.assertAlmostEqual(financial.net_share_change_yoy, 0.0, places=9)


@pytest.mark.parametrize(
    ("interim", "year_end", "annual"), [(40.0, 60.0, 100.0), (20.0, 60.0, 80.0)]
)
@pytest.mark.parametrize("action_day", [date(2025, 12, 15), date(2026, 5, 15)])
def test_reported_payment_detail_cannot_establish_issuer_split_basis(
    interim, year_end, annual, action_day
):
    # The same source shape can contain record-date or issuer-adjusted DPS.
    # Matching detail sums and cash totals do not identify which basis was used.
    disclosed = date(2026, 5, 15)
    asof = date(2026, 8, 10)
    row = _summary(
        "1111",
        disclosed,
        fiscal_period="FY",
        fiscal_year_end=date(2026, 3, 31),
        period_start=date(2025, 4, 1),
        period_end=date(2026, 3, 31),
        dps_actual_annual=annual,
        dividend_interim=interim,
        dividend_year_end=year_end,
        dividend_total_annual=130_000_000.0,
        shares_outstanding=2_000_000.0,
        treasury_shares=500_000.0,
        average_shares=1_750_000.0,
    )
    bars = [_split_bar("1111", action_day, 0.5)]
    normalized = _normalize_summaries_to_asof_basis([row], bars, asof)
    carry = _resolve_dividend_carry(normalized, bars, 1000.0, asof)
    assert carry.basis == "unresolved_split_basis"
    assert carry.dps_actual_annual is None
    assert carry.dividend_yield is None
    assert (
        _forward_asof_basis_dividend(
            _FYDividendObservation(date(2026, 3, 31), disclosed, annual, date(2025, 4, 1)),
            bars,
            basis_date=asof,
        )
        is None
    )
    assert row.dividend_interim == interim
    assert row.dividend_total_annual == 130_000_000.0


@pytest.mark.parametrize(("annual", "expected"), [(100.0, None), (0.0, 0.0)])
def test_offsetting_unit_changes_do_not_establish_positive_dividend_basis(annual, expected):
    asof = date(2026, 8, 10)
    row = _summary(
        "1111",
        date(2026, 5, 15),
        fiscal_period="FY",
        fiscal_year_end=date(2026, 3, 31),
        period_end=date(2026, 3, 31),
        period_start=date(2025, 4, 1),
        dps_actual_annual=annual,
        dividend_interim=annual * 0.4,
        dividend_year_end=annual * 0.6,
    )
    bars = [_split_bar("1111", date(2025, 6, 10), 0.5), _split_bar("1111", date(2025, 12, 15), 2.0)]
    carry = _resolve_dividend_carry([row], bars, 1000.0, asof)
    assert carry.split_factor == 1.0
    assert carry.dps_actual_annual == expected
    assert carry.dividend_yield == (None if expected is None else 0.0)
    assert (
        _forward_asof_basis_dividend(
            _FYDividendObservation(date(2026, 3, 31), row.disclosed_at, annual, row.period_start),
            bars,
            basis_date=asof,
        )
        == expected
    )


@pytest.mark.parametrize(("post_disclosure_split", "expected"), [(False, 100.0), (True, None)])
def test_post_disclosure_ambiguity_agrees_between_routes(post_disclosure_split, expected):
    asof = date(2026, 8, 10)
    row = _summary(
        "1111",
        date(2026, 5, 15),
        fiscal_period="FY",
        fiscal_year_end=date(2026, 3, 31),
        period_end=date(2026, 3, 31),
        period_start=date(2025, 4, 1),
        dps_actual_annual=100.0,
    )
    bars = [_split_bar("1111", date(2026, 7, 1), 0.5)] if post_disclosure_split else []
    normalized = _normalize_summaries_to_asof_basis([row], bars, asof)
    carry = _resolve_dividend_carry(normalized, bars, 1000.0, asof)
    assert normalized[0].dps_actual_annual == 100.0
    assert carry.basis == ("actual_reported" if expected is not None else "unresolved_split_basis")
    assert carry.dps_actual_annual == expected
    assert carry.dividend_yield == (expected / 1000.0 if expected is not None else None)
    assert (
        _forward_asof_basis_dividend(
            _FYDividendObservation(date(2026, 3, 31), row.disclosed_at, 100.0, row.period_start),
            bars,
            basis_date=asof,
        )
        == expected
    )


def test_unresolved_latest_dividend_year_does_not_restore_an_older_growth_signal():
    summaries = [
        _summary(
            "1111",
            date(year, 5, 15),
            fiscal_period="FY",
            fiscal_year_end=date(year, 3, 31),
            period_start=date(year - 1, 4, 1),
            dps_actual_annual=amount,
        )
        for year, amount in [(2024, 80.0), (2025, 90.0), (2026, 100.0)]
    ]
    bars = [_split_bar("1111", date(2025, 12, 15), 0.5)]
    result = build_shareholder_return_change_signals(summaries, bars, date(2026, 8, 10))
    assert result.dps_yoy_latest is None
    assert result.dps_streak_up is None


def test_rights_price_adjustment_does_not_change_actual_cash_per_share():
    row = _summary(
        "1111",
        date(2026, 5, 15),
        fiscal_period="FY",
        fiscal_year_end=date(2026, 3, 31),
        period_end=date(2026, 3, 31),
        period_start=date(2025, 4, 1),
        dps_actual_annual=100.0,
    )
    event = JQuantsDailyBar(
        ticker="1111",
        traded_at=date(2025, 12, 15),
        close=1000.0,
        turnover_value=3e8,
        adjustment_factor=0.8,
        ex_right_type="3",
    )
    carry = _resolve_dividend_carry([row], [event], 1000.0, date(2026, 8, 10))
    assert carry.dps_actual_annual == 100.0
    assert carry.dividend_yield == 0.1


@pytest.mark.parametrize(("factor", "kind"), [(2.0, "2"), (0.5, None), (None, "1")])
def test_reverse_or_unknown_unit_change_keeps_actual_dividend_unresolved(factor, kind):
    row = _summary(
        "1111",
        date(2026, 5, 15),
        fiscal_period="FY",
        fiscal_year_end=date(2026, 3, 31),
        period_start=date(2025, 4, 1),
        period_end=date(2026, 3, 31),
        dps_actual_annual=100.0,
    )
    event = JQuantsDailyBar(
        ticker="1111",
        traded_at=date(2025, 12, 15),
        close=1000.0,
        turnover_value=3e8,
        adjustment_factor=factor,
        ex_right_type=kind,
    )
    carry = _resolve_dividend_carry([row], [event], 1000.0, date(2026, 8, 10))
    assert carry.basis == "unresolved_split_basis"
    assert carry.dividend_yield is None
    assert (
        _forward_asof_basis_dividend(
            _FYDividendObservation(date(2026, 3, 31), row.disclosed_at, 100.0),
            [event],
            basis_date=date(2026, 8, 10),
        )
        is None
    )


def test_unrelated_payment_and_share_disagreements_do_not_veto_annual_dps(tmp_path):
    import sqlite3
    from contextlib import closing

    from baibai_engine.machine_analysis.historical_evaluation.forward import _load_fy_dividends
    from baibai_engine.machine_analysis.observations.financials.reader import (
        read_financial_documents,
    )
    from baibai_engine.machine_analysis.observations.jquants_writer import store_jquants_fin_summaries

    day = date(2026, 5, 15)
    path = tmp_path / "market.sqlite"
    records = [
        {
            "Code": "11110",
            "DiscDate": day.isoformat(),
            "DiscNo": str(index),
            "DocType": "FYFinancialStatements_Consolidated_JP",
            "CurPerType": "FY",
            "CurPerSt": "2025-04-01",
            "CurPerEn": "2026-03-31",
            "CurFYSt": "2025-04-01",
            "CurFYEn": "2026-03-31",
            "DivAnn": 100.0,
            "Div2Q": 20.0 * index,
            "DivFY": 80.0 / index,
            "DivTotalAnn": 100_000_000.0 * index,
            "ShOutFY": 2_000_000.0 * index,
            "TrShFY": 100_000.0 * index,
        }
        for index in (1, 2)
    ]
    store_jquants_fin_summaries(path, records, requested_start=day, requested_end=day)
    with closing(sqlite3.connect(path)) as connection:
        rows = read_financial_documents(connection, start=day, end=day, ticker="1111")
        forward = _load_fy_dividends(connection, "1111", cutoff=day)
    assert len(rows) == 2
    assert {row.dividend_total_annual for row in rows} == {100_000_000.0, 200_000_000.0}
    assert _resolve_dividend_carry(rows, [], 1000.0, day).dps_actual_annual == 100.0
    assert len(forward) == 1
    assert forward[0].dps_actual_annual == 100.0


@pytest.mark.parametrize("annual", [0.0, 100.0])
def test_future_unit_change_does_not_contaminate_requested_dividend_basis(annual):
    disclosed = date(2026, 5, 15)
    asof = date(2026, 6, 30)
    source = _summary(
        "1111",
        disclosed,
        fiscal_period="FY",
        fiscal_year_end=date(2026, 3, 31),
        period_end=date(2026, 3, 31),
        period_start=date(2025, 4, 1),
        dps_actual_annual=annual,
    )
    events = [_split_bar("1111", date(2026, 7, 1), 0.5)]
    rows = _normalize_summaries_to_asof_basis([source], events, asof)
    carry = _resolve_dividend_carry(rows, events, 1000.0, asof)
    assert carry.dps_actual_annual == annual
    assert carry.split_factor is None
    observation = _FYDividendObservation(date(2026, 3, 31), disclosed, annual, source.period_start)
    assert _forward_asof_basis_dividend(observation, events, basis_date=asof) == annual
    after = date(2026, 7, 2)
    later = _resolve_dividend_carry(rows, events, 1000.0, after)
    assert later.dps_actual_annual == (0.0 if annual == 0 else None)
    assert later.split_factor == 0.5
    assert _forward_asof_basis_dividend(observation, events, basis_date=after) == (
        0.0 if annual == 0 else None
    )


@pytest.mark.parametrize("forecast", [0.0, 10.0])
def test_missing_price_preserves_forecast_dps_but_never_invents_a_yield(forecast):
    summaries = [_summary("130A", date(2025, 8, 10), dps_forecast_annual=forecast)]
    priced = _resolve_dividend_carry(summaries, [], 1000.0, date(2025, 9, 1))
    missing = _resolve_dividend_carry(summaries, [], None, date(2025, 9, 1))
    assert priced.dividend_yield == forecast / 1000
    assert missing.dps_forecast_annual == forecast
    assert missing.dps_actual_annual == priced.dps_actual_annual
    assert missing.split_factor == priced.split_factor
    assert missing.dividend_yield is None
    assert missing.basis == "unavailable"
