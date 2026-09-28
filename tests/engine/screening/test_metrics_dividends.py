from __future__ import annotations

import unittest
from datetime import date, timedelta

from tests.engine.screening.metric_fixtures import (
    _daily_bars,
    _security,
    _split_bar,
    _summary,
)

from baibai_engine.market.bars import JQuantsDailyBar
from baibai_engine.screening.calibration.forward import (
    _asof_basis_dividend as _forward_asof_basis_dividend,
)
from baibai_engine.screening.calibration.forward import (
    _FYDividendObservation,
)
from baibai_engine.screening.metrics import (
    build_metrics,
)
from baibai_engine.screening.metrics.capital import (
    _normalize_summaries_to_asof_basis,
    _normalize_summaries_with_status,
)
from baibai_engine.screening.metrics.dividends import (
    _asof_basis_dividend,
    _resolve_dividend_carry,
)
from baibai_engine.screening.schema import FinancialSnapshot


class DividendCarryResolverTests(unittest.TestCase):
    """carry 用配当利回りの基準解決 (予想の跳ね・株式基準) を検証する。"""

    def test_zero_forecast_outranks_positive_actual(self) -> None:
        summaries = [
            _summary("130A", date(2025, 5, 10), dps_actual_annual=50.0),
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
        self.assertEqual(normalized.summaries[0].dps_actual_annual, 25.0)
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
            _summary("3659", date(2026, 2, 12), dps_actual_annual=45.0),
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
            _summary("1111", date(2026, 2, 12), dps_actual_annual=45.0),
        ]

        carry = _resolve_dividend_carry(
            summaries, [], latest_price=1000.0, asof_date=date(2026, 8, 28)
        )

        self.assertEqual(carry.basis, "forecast_annual")
        assert carry.dividend_yield is not None
        self.assertAlmostEqual(carry.dividend_yield, 90.0 / 1000.0, places=9)

    def test_a_withdrawn_forecast_does_not_outrank_a_newer_actual(self) -> None:
        """8798 型の再現: 無配化した会社に、取り下げ前の予想の利回りを付けない。

        2024-09 に予想 17.5 を出したあと 4 期連続赤字で無配になり、2025-11 の通期実績は
        0.0、以降の提出に予想は無い。予想を無制限に遡ると 130 円の株に 13.46%/年の carry
        が付き、reversion 上限 (5%/年) を単独で超えて見返りを過大評価する。
        """
        summaries = [
            _summary("8798", date(2024, 9, 18), dps_forecast_annual=17.5),
            _summary("8798", date(2025, 11, 27), dps_actual_annual=0.0),
            _summary("8798", date(2026, 2, 13)),
        ]
        carry = _resolve_dividend_carry(
            summaries, [], latest_price=130.0, asof_date=date(2026, 8, 11)
        )
        self.assertEqual(carry.dividend_yield, 0.0)
        self.assertIsNone(carry.dps_forecast_annual)
        self.assertEqual(carry.basis, "actual_reported")

    def test_a_forecast_newer_than_the_actual_still_answers(self) -> None:
        # 鮮度境界は予想を弱めない。実績より後に出た予想はそのまま carry になる。
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

    def test_refuses_the_actual_yield_when_a_split_falls_in_the_accrual_window(self) -> None:
        # 分割が期末に重なる形。期末配当の基準日は分割の効力発生日より前なので実績 DPS は
        # 分割前基準だが、権利落ち日しか持たない store からはそれを言えない。基準が確定
        # できない年度は利回りを出さず、判別できなかった factor だけを事実として残す。
        summaries = [
            _summary("5445", date(2026, 5, 7), dps_actual_annual=300.0, dps_forecast_annual=None),
            _summary("5445", date(2025, 5, 7), dps_actual_annual=375.0, dps_forecast_annual=None),
        ]
        carry = _resolve_dividend_carry(
            summaries,
            [_split_bar("5445", date(2026, 3, 30), 1.0 / 3.0)],
            latest_price=1911.0,
            asof_date=date(2026, 7, 10),
        )
        self.assertEqual(carry.basis, "unresolved_split_basis")
        self.assertIsNone(carry.dividend_yield)
        self.assertIsNone(carry.dps_actual_annual)
        assert carry.split_factor is not None
        self.assertAlmostEqual(carry.split_factor, 1.0 / 3.0, places=6)

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

    def test_refuses_the_actual_yield_when_a_consolidation_falls_in_the_accrual_window(
        self,
    ) -> None:
        # 逆向き。併合後に開示された実績 DPS を併合 factor で膨らませると、株価に対して
        # 桁違いの利回りになる。こちらも基準を確定できないので出さない。
        summaries = [
            _summary("1491", date(2026, 5, 15), dps_actual_annual=34.0, dps_forecast_annual=None),
            _summary("1491", date(2025, 5, 15), dps_actual_annual=1.5, dps_forecast_annual=None),
        ]
        carry = _resolve_dividend_carry(
            summaries,
            [_split_bar("1491", date(2025, 9, 29), 20.0)],
            latest_price=785.0,
            asof_date=date(2026, 8, 10),
        )
        self.assertEqual(carry.basis, "unresolved_split_basis")
        self.assertIsNone(carry.dividend_yield)
        assert carry.split_factor is not None
        self.assertAlmostEqual(carry.split_factor, 20.0, places=6)

    def test_resolves_a_consolidation_that_precedes_every_payment(self) -> None:
        # 1491 の形。20:1 併合が期中に起き、当期の配当は期末 1 回だけ。基準日 2026-03-31 は
        # 併合より後なので報告値 34 は既に併合後の株式基準にある。支払ごとに換算すると
        # 掛かる調整が無く、株価と同じ基準の 34 がそのまま出る。
        summaries = [
            _summary(
                "1491",
                date(2026, 5, 15),
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                period_start=date(2025, 4, 1),
                dps_actual_annual=34.0,
                dividend_year_end=34.0,
            ),
            _summary("1491", date(2025, 5, 15), dps_actual_annual=1.5),
        ]
        carry = _resolve_dividend_carry(
            summaries,
            [_split_bar("1491", date(2025, 9, 29), 20.0)],
            latest_price=785.0,
            asof_date=date(2026, 8, 10),
        )
        self.assertEqual(carry.basis, "actual_record_date_resolved")
        assert carry.dividend_yield is not None
        self.assertAlmostEqual(carry.dividend_yield, 34.0 / 785.0, places=6)
        # negative assertion: 併合 factor を掛けた 680 円で 86.6% を出さない。
        self.assertLess(carry.dividend_yield, 0.10)

    def test_resolves_a_row_the_asof_normalisation_already_rewrote(self) -> None:
        """The shape production always hands over: a row already moved to the as-of basis.

        `dps_actual_annual` is rewritten by the as-of normalisation while the payment
        details stay as disclosed, so comparing the two raw reads back the conversion
        factor rather than a missing detail. A year with any adjustment after its
        disclosure would be refused, taking its dividend growth signal with it.
        """
        summaries = [
            _summary(
                "3399",
                date(2025, 5, 15),
                fiscal_period="FY",
                fiscal_year_end=date(2025, 3, 31),
                period_start=date(2024, 4, 1),
                dps_actual_annual=100.0,
                dividend_interim=40.0,
                dividend_year_end=60.0,
            ),
            _summary(
                "3399",
                date(2024, 5, 15),
                fiscal_year_end=date(2024, 3, 31),
                dps_actual_annual=80.0,
            ),
        ]
        bars = [
            # Inside the accrual window, clear of both record dates.
            _split_bar("3399", date(2024, 12, 15), 0.5),
            # After the disclosure: this is the one the normalisation folds into
            # `dps_actual_annual` and the detail sum never sees.
            _split_bar("3399", date(2025, 10, 1), 0.5),
        ]
        asof = date(2026, 8, 10)
        normalized = _normalize_summaries_to_asof_basis(summaries, bars, asof)
        self.assertAlmostEqual(normalized[0].dps_actual_annual or 0.0, 50.0, places=6)
        self.assertEqual(normalized[0].dividend_interim, 40.0)

        carry = _resolve_dividend_carry(normalized, bars, latest_price=1000.0, asof_date=asof)

        self.assertEqual(carry.basis, "actual_record_date_resolved")
        assert carry.dividend_yield is not None
        # 40 paid on the pre-2024-12-15 basis (x0.25) plus 60 on the pre-2025-10-01
        # basis (x0.5) is 40 yen at the as-of basis.
        self.assertAlmostEqual(carry.dividend_yield, 40.0 / 1000.0, places=6)

    def test_resolves_an_interim_only_payer_whose_split_came_after_the_record_date(self) -> None:
        # 4626 の形。当期の配当は中間 1 回だけで、基準日 2025-09-30 より後に 1:2 分割。
        # その支払は分割前の株数で払われたので、株価と比べるには 0.5 を掛ける。
        summaries = [
            _summary(
                "4626",
                date(2026, 4, 30),
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                period_start=date(2025, 4, 1),
                dps_actual_annual=165.0,
                dividend_interim=165.0,
            ),
            _summary("4626", date(2025, 4, 30), dps_actual_annual=190.0),
        ]
        carry = _resolve_dividend_carry(
            summaries,
            [_split_bar("4626", date(2025, 11, 27), 0.5)],
            latest_price=4827.0,
            asof_date=date(2026, 8, 10),
        )
        self.assertEqual(carry.basis, "actual_record_date_resolved")
        assert carry.dividend_yield is not None
        self.assertAlmostEqual(carry.dividend_yield, 82.5 / 4827.0, places=6)

    def test_refuses_when_the_split_lands_beside_a_record_date(self) -> None:
        # 1909 の形。権利落ちが 2026-03-30 で期末配当の基準日 2026-03-31 の前日に並ぶ。
        # 効力発生日を持たない store からは、その配当が分割の前の株数で払われたのか後なのか
        # を決められない。中間ぶんだけ換算して足すと 63.75 になり、正の 22.5 と 3 倍近く違う。
        summaries = [
            _summary(
                "1909",
                date(2026, 5, 13),
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                period_start=date(2025, 4, 1),
                dps_actual_annual=90.0,
                dividend_interim=35.0,
                dividend_year_end=55.0,
            ),
            _summary("1909", date(2025, 5, 13), dps_actual_annual=70.0),
        ]
        carry = _resolve_dividend_carry(
            summaries,
            [_split_bar("1909", date(2026, 3, 30), 0.25)],
            latest_price=3705.0,
            asof_date=date(2026, 8, 10),
        )
        self.assertEqual(carry.basis, "unresolved_split_basis")
        self.assertIsNone(carry.dividend_yield)

    def test_refuses_when_the_paid_amount_contradicts_the_payment_detail(self) -> None:
        # 総額は円なので株式基準を持たない。支払ごとの換算と食い違うなら、どちらかが別の
        # 株式基準を見ている。どちらが正しいかを機械が決められないので答えない。
        summaries = [
            _summary(
                "4626",
                date(2026, 4, 30),
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                period_start=date(2025, 4, 1),
                dps_actual_annual=165.0,
                dividend_interim=165.0,
                # 82.5 円/株になるはずのところ、総額は 165 円/株ぶんを示している。
                dividend_total_annual=165.0 * 1_000_000.0,
                shares_outstanding=1_000_000.0,
                treasury_shares=0.0,
                average_shares=1_000_000.0,
            ),
            _summary("4626", date(2025, 4, 30), dps_actual_annual=190.0),
        ]
        carry = _resolve_dividend_carry(
            summaries,
            [_split_bar("4626", date(2025, 11, 27), 0.5)],
            latest_price=4827.0,
            asof_date=date(2026, 8, 10),
        )
        self.assertEqual(carry.basis, "unresolved_split_basis")
        self.assertIsNone(carry.dividend_yield)

    def test_a_broken_share_count_does_not_veto_the_payment_detail(self) -> None:
        # 8097 の形。feed が自己株式数の欄に株数そのものを入れており、自己株控除後株式数が
        # 期中平均から 2 桁ずれる。この株数で総額を割った値は照合に使えないので、支払ごとの
        # 換算をその値で否定しない。
        summaries = [
            _summary(
                "4626",
                date(2026, 4, 30),
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                period_start=date(2025, 4, 1),
                dps_actual_annual=165.0,
                dividend_interim=165.0,
                dividend_total_annual=82.5 * 1_000_000.0,
                shares_outstanding=1_000_000.0,
                treasury_shares=990_000.0,
                average_shares=1_000_000.0,
            ),
            _summary("4626", date(2025, 4, 30), dps_actual_annual=190.0),
        ]
        carry = _resolve_dividend_carry(
            summaries,
            [_split_bar("4626", date(2025, 11, 27), 0.5)],
            latest_price=4827.0,
            asof_date=date(2026, 8, 10),
        )
        self.assertEqual(carry.basis, "actual_record_date_resolved")
        assert carry.dividend_yield is not None
        self.assertAlmostEqual(carry.dividend_yield, 82.5 / 4827.0, places=6)

    def test_a_same_year_correction_does_not_collapse_the_detection_window(self) -> None:
        # 判別窓の起点は当期会計期間の開始日。前期開示日を起点にすると、同一年度の訂正開示が
        # 直前に来た行で窓が 2 日へ縮み、窓内の分割が窓の外へ出て報告値がそのまま通る。
        summaries = [
            _summary(
                "3382",
                date(2024, 4, 12),
                fiscal_period="FY",
                fiscal_year_end=date(2024, 2, 29),
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
        # 0 円は何倍しても 0 円なので、無配の年度に確定すべき株式基準は無い。ここを拒否に
        # 倒すと、分割を出す無配銘柄が利回りだけでなく E[r] ごと判断面から消える。
        summaries = [
            _summary(
                "7369",
                date(2026, 5, 15),
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
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

    def test_refuses_when_the_payment_detail_does_not_add_up_to_the_reported_year(self) -> None:
        # feed は明細を一部だけ返すことがある。調整前の合計が報告年間値と合わない行は真値の
        # 一部しか持たないので、換算しても真値の一部にしかならない。総額が無い行では総額
        # veto も効かないため、ここで止めないと過小な値が「解決済み」として出る。
        summaries = [
            _summary(
                "4626",
                date(2026, 5, 15),
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                period_start=date(2025, 4, 1),
                dps_actual_annual=60.0,
                dividend_year_end=30.0,
            ),
            _summary("4626", date(2025, 5, 15), dps_actual_annual=50.0),
        ]
        carry = _resolve_dividend_carry(
            summaries,
            [_split_bar("4626", date(2025, 10, 1), 0.5)],
            latest_price=1000.0,
            asof_date=date(2026, 8, 10),
        )
        self.assertEqual(carry.basis, "unresolved_split_basis")
        self.assertIsNone(carry.dps_actual_annual)

    def test_the_share_anchor_is_normalized_with_the_share_count_it_checks(self) -> None:
        # 期中平均株式数は株数なので、asof 基準への正規化で `発行済` と同じ換算を受ける。
        # 受けないと比が factor 倍ずれ、健全性 gate が veto の最も要る
        # 母集団 (開示より後に分割がある行) でだけ静かに外れる。
        row = _summary(
            "4626",
            date(2026, 4, 30),
            fiscal_period="FY",
            fiscal_year_end=date(2026, 3, 31),
            shares_outstanding=1_000_000.0,
            treasury_shares=0.0,
            average_shares=1_000_000.0,
        )
        (normalized,) = _normalize_summaries_to_asof_basis(
            [row],
            [_split_bar("4626", date(2026, 6, 1), 1.0 / 3.0)],
            date(2026, 8, 10),
        )
        assert normalized.shares_outstanding is not None
        assert normalized.average_shares is not None
        self.assertAlmostEqual(normalized.shares_outstanding, 3_000_000.0, places=3)
        self.assertAlmostEqual(normalized.average_shares, 3_000_000.0, places=3)
        # negative assertion: 生のまま残ると比が 3.0 になり 2.0 gate を外れる。
        self.assertAlmostEqual(
            normalized.shares_outstanding / normalized.average_shares, 1.0, places=6
        )

    def test_forecast_still_answers_when_the_actual_basis_is_unresolved(self) -> None:
        # 予想 DPS は分割を跨ぐ行で正規化が None へ落とすので、残っていれば基準が揃って
        # いる。実績側が確定できなくても carry は予想で組める。
        summaries = [
            _summary("5445", date(2026, 5, 7), dps_actual_annual=300.0, dps_forecast_annual=40.0),
            _summary("5445", date(2025, 5, 7), dps_actual_annual=375.0, dps_forecast_annual=None),
        ]
        carry = _resolve_dividend_carry(
            summaries,
            [_split_bar("5445", date(2026, 3, 30), 1.0 / 3.0)],
            latest_price=1911.0,
            asof_date=date(2026, 7, 10),
        )
        self.assertEqual(carry.basis, "forecast_annual")
        self.assertIsNone(carry.dps_actual_annual)
        assert carry.dividend_yield is not None
        self.assertAlmostEqual(carry.dividend_yield, 40.0 / 1911.0, places=6)

    def test_uses_actual_reported_when_no_split_and_no_forecast(self) -> None:
        summaries = [
            _summary("7203", date(2026, 5, 7), dps_actual_annual=50.0, dps_forecast_annual=None),
            _summary("7203", date(2025, 5, 7), dps_actual_annual=45.0, dps_forecast_annual=None),
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


class DividendBasisRouteEquivalenceTests(unittest.TestCase):
    """screening と較正が同じ年度に同じ配当を出すことを強制する。

    判定は 2 か所に書かれている。`metrics._asof_basis_dividend` は
    `_normalize_summaries_to_asof_basis` を通った行を受け、
    `calibration.forward._asof_basis_dividend` は SQL から読んだ as-reported の行を受ける。
    **どちらも正しいが正しさの理由が逆で、片方だけを直すと較正が測る量と本番が使う量が
    別になる。** 実際 2026-08 に metrics 側だけが基準を揃えておらず、この経路を通る配当
    年度の約 9% を捨てていた。
    """

    @staticmethod
    def _bar(traded_at: date, factor: float | None = None) -> JQuantsDailyBar:
        return JQuantsDailyBar(
            ticker="1111",
            traded_at=traded_at,
            close=1000.0,
            turnover_value=3e8,
            adjustment_factor=factor,
        )

    def _both_routes(
        self,
        *,
        interim: float,
        year_end: float,
        reported: float,
        split_on: date | None,
        split_factor: float,
        asof: date,
    ) -> tuple[float | None, float | None]:
        fiscal_year_end = date(2026, 3, 31)
        disclosed_at = date(2026, 5, 15)
        bars = [self._bar(date(2025, 4, 1) + timedelta(days=index * 7)) for index in range(70)]
        if split_on is not None:
            bars.append(self._bar(split_on, split_factor))
        bars.sort(key=lambda bar: bar.traded_at)

        summaries = [
            _summary(
                "1111",
                disclosed_at,
                fiscal_period="FY",
                fiscal_year_end=fiscal_year_end,
                period_start=date(2025, 4, 1),
                dps_actual_annual=reported,
                dividend_interim=interim,
                dividend_year_end=year_end,
            )
        ]
        normalized = _normalize_summaries_to_asof_basis(summaries, bars, asof)
        from_metrics = _asof_basis_dividend(normalized[0], bars, asof_date=asof)

        observation = _FYDividendObservation(
            fiscal_year_end=fiscal_year_end,
            disclosed_at=disclosed_at,
            dps_actual_annual=reported,
            period_start=date(2025, 4, 1),
            payments=(None, interim, None, year_end),
        )
        from_forward = _forward_asof_basis_dividend(observation, bars, basis_date=asof)
        return from_metrics, from_forward

    def test_a_split_inside_the_year_resolves_the_same_on_both_routes(self) -> None:
        metrics_value, forward_value = self._both_routes(
            interim=40.0,
            year_end=60.0,
            reported=100.0,
            split_on=date(2025, 12, 15),
            split_factor=0.5,
            asof=date(2026, 8, 10),
        )
        self.assertIsNotNone(metrics_value)
        self.assertIsNotNone(forward_value)
        assert metrics_value is not None
        assert forward_value is not None
        self.assertAlmostEqual(metrics_value, forward_value, places=6)

    def test_a_split_after_the_disclosure_resolves_the_same_on_both_routes(self) -> None:
        """metrics 側だけが正規化を受ける形。ここがずれていた実際の欠陥である。"""
        metrics_value, forward_value = self._both_routes(
            interim=40.0,
            year_end=60.0,
            reported=100.0,
            split_on=date(2026, 7, 1),
            split_factor=0.5,
            asof=date(2026, 8, 10),
        )
        assert metrics_value is not None
        assert forward_value is not None
        self.assertAlmostEqual(metrics_value, forward_value, places=6)

    def test_a_payment_beside_a_split_is_refused_on_both_routes(self) -> None:
        metrics_value, forward_value = self._both_routes(
            interim=40.0,
            year_end=60.0,
            reported=100.0,
            # 期末配当の基準日 (2026-03-31) の 1 日前。どちらの経路も答えない。
            split_on=date(2026, 3, 30),
            split_factor=0.5,
            asof=date(2026, 8, 10),
        )
        self.assertIsNone(metrics_value)
        self.assertIsNone(forward_value)

    def test_month_end_interim_guard_has_both_boundaries_on_both_routes(self) -> None:
        # 2026-03-31 の interim は 2025-09-30。旧 9/28 丸めでは
        # 10/4, 10/5 を見逃し、9/24 を誤って拒否していた。
        for split_on, expected in (
            (date(2025, 10, 4), None),
            (date(2025, 10, 5), None),
            (date(2025, 10, 6), 50.0),
            (date(2025, 9, 24), 60.0),
        ):
            with self.subTest(split_on=split_on):
                metrics_value, forward_value = self._both_routes(
                    interim=20.0,
                    year_end=40.0,
                    reported=60.0,
                    split_on=split_on,
                    split_factor=0.5,
                    asof=date(2026, 8, 10),
                )
                self.assertEqual(metrics_value, expected)
                self.assertEqual(forward_value, expected)

    def test_incomplete_details_are_refused_on_both_routes(self) -> None:
        metrics_value, forward_value = self._both_routes(
            interim=40.0,
            year_end=10.0,
            reported=100.0,
            split_on=date(2025, 12, 15),
            split_factor=0.5,
            asof=date(2026, 8, 10),
        )
        self.assertIsNone(metrics_value)
        self.assertIsNone(forward_value)


class DividendCrossCheckBoundaryTests(unittest.TestCase):
    """明細合計の検算は、行の正規化と同じ境界で換算しなければならない。

    片方だけが開示日当日の権利落ちを数えると、比が必ず換算係数の逆数になり、その年度を
    「明細が欠けている」として捨てる。捨てた年度は増配判定ごと消える。
    """

    @staticmethod
    def _bar(traded_at: date, factor: float | None = None) -> JQuantsDailyBar:
        return JQuantsDailyBar(
            ticker="1111",
            traded_at=traded_at,
            close=1000.0,
            turnover_value=3e8,
            adjustment_factor=factor,
        )

    def test_a_split_going_ex_on_the_disclosure_date_keeps_the_year(self) -> None:
        disclosed_at = date(2026, 5, 15)
        asof = date(2026, 5, 29)
        bars = [self._bar(date(2025, 4, 1) + timedelta(days=index * 7)) for index in range(60)]
        bars.append(self._bar(disclosed_at, 0.5))
        bars.sort(key=lambda bar: bar.traded_at)
        summaries = [
            _summary(
                "1111",
                disclosed_at,
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                period_start=date(2025, 4, 1),
                dps_actual_annual=100.0,
                dividend_interim=40.0,
                dividend_year_end=60.0,
            )
        ]
        normalized = _normalize_summaries_to_asof_basis(summaries, bars, asof)

        resolved = _asof_basis_dividend(normalized[0], bars, asof_date=asof)

        self.assertIsNotNone(resolved)
        self.assertAlmostEqual(resolved or 0.0, 50.0, places=6)


class TradableShareChangeTest(unittest.TestCase):
    """取得年と消却年で 2 つの株数軸が別々に動くことを固定する。

    日本の自社株買いは取得した株式を自己株式へ入れるだけで、発行済株式総数は消却するまで
    減らない。carry の株数項が発行済で測られている限り、還元が起きた年は 0 で、現金の動かない
    消却年に符号が付く。どちらで測るかは事前登録した比較で決めるので
    (reports/studies/2026-08-17-tradable-share-change/)、ここでは 2 軸が別物であることだけを
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
        """自己株を 10% 積んだ年。発行済は動かないので現行の carry 項は 0 になる。"""

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
        """積んだ自己株を消却した年。現金は動かないのに現行の carry 項だけが符号を持つ。"""

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


def test_the_two_dividend_routes_must_agree_within_five_percent() -> None:
    """The per-share total and the sum of the payments are two readings of one year.

    They are computed on different share bases — the total is divided by the period-end
    count while each payment belongs to its own record date — so a few percent apart is
    the buyback, and further apart means one route is on another basis entirely. The
    year is then dropped rather than averaged, because a wrong dividend reaches the
    reader as a yield.
    """

    def carried(detail_total: float) -> float | None:
        half = detail_total / 2
        return _asof_basis_dividend(
            _summary(
                "130A",
                date(2026, 5, 10),
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                period_start=date(2025, 4, 1),
                period_end=date(2026, 3, 31),
                dps_actual_annual=100.0,
                dividend_interim=half,
                dividend_year_end=half,
            ),
            [],
            asof_date=date(2026, 7, 10),
        )

    assert carried(104.9) is not None
    assert carried(95.1) is not None
    assert carried(105.1) is None
    assert carried(94.9) is None
