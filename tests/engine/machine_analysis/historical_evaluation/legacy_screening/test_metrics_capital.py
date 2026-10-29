"""旧較正リプレイの合成と共有計算の回帰。現行五モデルの候補gateではない。"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, timedelta

from tests.engine.machine_analysis.historical_evaluation.legacy_screening.metric_fixtures import (
    _edinet_metric_record,
    _security,
    _summary,
)

from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.metric_builder import (
    build_metrics,
)
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.rule_config import (
    load_screening_rules,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.capital import (
    _closer_share_basis,
    _common_equity_yen,
    _edinet_passes_entity_scale_check,
    _normalize_summaries_to_asof_basis,
    _normalize_summaries_with_status,
    _resolve_capital_basis,
    _ShareBasis,
    build_shares_outstanding_index,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.periods import (
    _actual_rows,
    _ttm_value,
)
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.financials.metric_quality import TTMQuality
from baibai_engine.machine_analysis.observations.prices.price_models import (
    JQuantsAdjustmentFactorEvent,
    JQuantsDailyBar,
)


class ShareBasisOnTheDisclosureDateTests(unittest.TestCase):
    """開示日当日に権利落ちがある行の株式基準。

    この fixture は期末の分割前株数を申告する。開示日当日の adjustment_factor を
    除外すると株数だけ分割前・価格だけ分割後になる。実際の開示が常に未調整である
    ことを保証するテストではなく、遡及調整済みの基準は別の fixture で検証する。
    """

    @staticmethod
    def _bar(traded_at: date, factor: float | None = None) -> JQuantsDailyBar:
        return JQuantsDailyBar(
            ticker="1111",
            traded_at=traded_at,
            close=1000.0,
            turnover_value=3e8,
            adjustment_factor=factor,
            ex_right_type=(None if factor is None or factor == 1.0 else "2" if factor > 1 else "1"),
        )

    def test_a_split_going_ex_on_the_disclosure_date_normalizes_that_row(self) -> None:
        disclosed_at = date(2026, 7, 30)
        bars = [self._bar(date(2026, 7, 29)), self._bar(disclosed_at, 0.25)]
        summaries = [
            _summary(
                "1111",
                disclosed_at,
                shares_outstanding=700_000_000.0,
                treasury_shares=0.0,
            )
        ]

        normalized = _normalize_summaries_to_asof_basis(summaries, bars, date(2026, 7, 31))

        self.assertAlmostEqual(normalized[0].shares_outstanding or 0.0, 2_800_000_000.0, places=0)
        self.assertAlmostEqual(normalized[0].bps or 0.0, 30.0, places=6)

    def test_a_split_going_ex_after_the_disclosure_date_still_normalizes(self) -> None:
        """当日を数える変更が、翌日以降の権利落ちの扱いを変えていないこと。"""

        disclosed_at = date(2026, 7, 30)
        bars = [self._bar(disclosed_at), self._bar(date(2026, 7, 31), 0.25)]
        summaries = [
            _summary(
                "1111",
                disclosed_at,
                shares_outstanding=700_000_000.0,
                treasury_shares=0.0,
            )
        ]

        normalized = _normalize_summaries_to_asof_basis(summaries, bars, date(2026, 7, 31))

        self.assertAlmostEqual(normalized[0].shares_outstanding or 0.0, 2_800_000_000.0, places=0)


class CarriedCommonEquityTests(unittest.TestCase):
    """円経路を採るかは突き合わせ行の中で決め、carry 後の乖離では決めない。

    行内の整合と、後続行での値の変化を区別する。carry 後の差だけで古い BPS へ
    戻らないことを確認し、差の原因が資本変動だと識別できたとは主張しない。
    """

    @staticmethod
    def _row(
        *, bps: float, shares: float, total_assets: float, ratio: float
    ) -> JQuantsFinancialSummary:
        """行内で 2 経路が一致する突き合わせ行。判定はこの行だけを見る。"""

        return replace(
            _summary(
                "1111",
                date(2026, 5, 14),
                shares_outstanding=shares,
                treasury_shares=0.0,
                total_assets=total_assets,
                equity_to_asset_ratio=ratio,
            ),
            bps=bps,
        )

    def test_the_fresher_yen_route_is_kept_when_the_row_agrees(self) -> None:
        summaries = [self._row(bps=100.0, shares=1_000_000.0, total_assets=2e8, ratio=0.5)]

        equity = _common_equity_yen(
            summaries,
            total_assets=2.4e8,
            equity_to_asset_ratio=0.5,
            bps=100.0,
            shares_ex_treasury=1_000_000.0,
        )

        self.assertAlmostEqual(equity or 0.0, 1.2e8, places=0)

    def test_a_collapsed_equity_ratio_keeps_the_fresh_value(self) -> None:
        """自己資本が実際に崩れた会社で古い `bps` へ退避すると、割安側へ大きくずれる。"""

        summaries = [self._row(bps=100.0, shares=1_000_000.0, total_assets=2e8, ratio=0.5)]

        equity = _common_equity_yen(
            summaries,
            total_assets=2e8,
            equity_to_asset_ratio=0.05,
            bps=100.0,
            shares_ex_treasury=1_000_000.0,
        )

        self.assertAlmostEqual(equity or 0.0, 1e7, places=0)

    def test_a_row_that_disagrees_uses_the_common_share_basis(self) -> None:
        """行内の不一致では普通株基準の BPS 経路を採る。原因はこのテストでは特定しない。"""

        summaries = [self._row(bps=100.0, shares=1_000_000.0, total_assets=4e8, ratio=0.5)]

        equity = _common_equity_yen(
            summaries,
            total_assets=4e8,
            equity_to_asset_ratio=0.5,
            bps=100.0,
            shares_ex_treasury=1_000_000.0,
        )

        self.assertAlmostEqual(equity or 0.0, 1e8, places=0)

    def test_near_zero_ratio_uses_the_reported_rounding_interval(self) -> None:
        """EqARの3桁丸めが相対誤差を膨らませても、整合する普通株basisを拒否しない。"""

        summaries = [
            self._row(
                bps=5.34,
                shares=22_145_052.0,
                total_assets=17_860_000_000.0,
                ratio=0.007,
            )
        ]

        equity = _common_equity_yen(
            summaries,
            total_assets=17_860_000_000.0,
            equity_to_asset_ratio=-0.069,
            bps=5.34,
            shares_ex_treasury=22_145_052.0,
        )

        self.assertAlmostEqual(equity or 0.0, -1_232_340_000.0, places=0)


class InterimSplitShareBasisTests(unittest.TestCase):
    """期末と開示日の間に権利落ちがある行の株式基準を、行ごとに決める。

    提出者によって期末基準のまま出す行と分割を遡及適用した行に割れる。取り違えると
    株数が分割比だけずれ、時価総額・倍率・株数変化がまとめて壊れる。
    """

    @staticmethod
    def _bar(traded_at: date, factor: float | None = None) -> JQuantsDailyBar:
        return JQuantsDailyBar(
            ticker="1111",
            traded_at=traded_at,
            close=1000.0,
            turnover_value=3e8,
            adjustment_factor=factor,
            ex_right_type=(None if factor is None or factor == 1.0 else "2" if factor > 1 else "1"),
        )

    def _normalized_shares(self, *, reported: float, reference: float) -> float | None:
        """権利落ちを挟んで 2 行を並べ、後の行の正規化後株数を返す。"""

        bars = [
            self._bar(date(2026, 1, 30)),
            self._bar(date(2026, 4, 20), 0.5),
            self._bar(date(2026, 5, 15)),
        ]
        summaries = [
            _summary(
                "1111",
                date(2026, 2, 10),
                period_end=date(2025, 12, 31),
                shares_outstanding=reference,
                treasury_shares=0.0,
            ),
            _summary(
                "1111",
                date(2026, 5, 15),
                period_end=date(2026, 3, 31),
                shares_outstanding=reported,
                treasury_shares=0.0,
            ),
        ]
        return _normalize_summaries_to_asof_basis(summaries, bars, date(2026, 5, 29))[
            1
        ].shares_outstanding

    def test_a_row_still_on_the_period_end_basis_is_converted(self) -> None:
        """分割前の株数のまま出た行。価格だけ分割後になるので換算しないと時価総額が半分。"""

        self.assertAlmostEqual(
            self._normalized_shares(reported=1_000_000.0, reference=1_000_000.0),
            2_000_000.0,
            places=0,
        )

    def test_a_row_that_already_applied_the_split_is_left_alone(self) -> None:
        """提出者が遡及適用済みの行。もう一度掛けると株数が 2 倍になる。"""

        self.assertAlmostEqual(
            self._normalized_shares(reported=2_000_000.0, reference=1_000_000.0),
            2_000_000.0,
            places=0,
        )

    def test_a_small_issue_alongside_the_split_does_not_block_the_call(self) -> None:
        """分割と同時の数 % の増資は極を動かさない。固定幅の帯だとここで答えられなくなる。"""

        self.assertAlmostEqual(
            self._normalized_shares(reported=2_048_000.0, reference=1_000_000.0),
            2_048_000.0,
            places=0,
        )

    def test_an_earlier_action_is_applied_to_the_reference_before_classification(self) -> None:
        """連続したactionでは、比較元を現在の期末基準へ揃えてから次の基準を判定する。"""

        summaries = [
            _summary(
                "3936",
                date(2021, 8, 13),
                period_end=date(2021, 6, 30),
                shares_outstanding=1_166_592.0,
                treasury_shares=102.0,
            ),
            _summary(
                "3936",
                date(2021, 11, 10),
                period_end=date(2021, 9, 30),
                shares_outstanding=17_541_915.0,
                treasury_shares=2_130.0,
            ),
        ]
        events = [
            JQuantsAdjustmentFactorEvent("3936", date(2021, 9, 15), 0.2, "1"),
            JQuantsAdjustmentFactorEvent("3936", date(2021, 11, 1), 1.0 / 3.0, "1"),
        ]

        normalized = _normalize_summaries_to_asof_basis(
            summaries,
            events,
            date(2021, 11, 10),
        )
        resolution = _resolve_capital_basis(normalized)

        self.assertAlmostEqual(normalized[1].shares_outstanding or 0.0, 17_541_915.0, places=0)
        self.assertAlmostEqual(resolution.shares_ex_treasury or 0.0, 17_539_785.0, places=0)

    def test_average_shares_resolves_only_the_split_basis_of_a_large_capital_change(self) -> None:
        """AvgShはgrossの代替にせず、ShOut残差が大きい行のbasisだけを確定する。"""

        summaries = [
            _summary(
                "3350",
                date(2024, 5, 15),
                period_end=date(2024, 3, 31),
                shares_outstanding=114_692_187.0,
                treasury_shares=21_945.0,
                average_shares=114_670_334.0,
            ),
            _summary(
                "3350",
                date(2024, 8, 14),
                period_end=date(2024, 6, 30),
                shares_outstanding=181_692_187.0,
                treasury_shares=22_885.0,
                average_shares=138_793_927.0,
            ),
        ]
        events = [JQuantsAdjustmentFactorEvent("3350", date(2024, 7, 30), 10.0, "2")]

        normalized = _normalize_summaries_to_asof_basis(
            summaries,
            events,
            date(2024, 8, 14),
        )
        resolution = _resolve_capital_basis(normalized)

        # 期末basisのShOutFYは10:1併合後へ換算するが、AvgSh自身をcapitalには使わない。
        self.assertAlmostEqual(normalized[1].shares_outstanding or 0.0, 18_169_218.7, places=1)
        self.assertAlmostEqual(resolution.shares_ex_treasury or 0.0, 18_166_930.2, places=1)

    def test_average_shares_can_confirm_an_already_adjusted_disclosure_basis(self) -> None:
        """AvgSh anchorが開示basisを示す行は、ShOutFYを二重換算しない。"""

        summaries = [
            _summary(
                "6628",
                date(2020, 2, 14),
                period_end=date(2019, 12, 31),
                shares_outstanding=189_869_995.0,
                treasury_shares=408_187.0,
                average_shares=150_421_187.0,
            ),
            _summary(
                "6628",
                date(2020, 7, 31),
                period_end=date(2020, 3, 31),
                shares_outstanding=54_866_334.0,
                treasury_shares=81_639.0,
                average_shares=33_700_601.0,
            ),
        ]
        events = [JQuantsAdjustmentFactorEvent("6628", date(2020, 7, 20), 5.0, "2")]

        normalized = _normalize_summaries_to_asof_basis(
            summaries,
            events,
            date(2020, 7, 31),
        )

        self.assertAlmostEqual(normalized[1].shares_outstanding or 0.0, 54_866_334.0, places=0)

    def test_a_large_capital_change_alongside_the_split_is_refused(self) -> None:
        """どちらの仮説でも残差が大きい行は答えない。時価総額が出ないので母集団に入らない。"""

        self.assertIsNone(self._normalized_shares(reported=1_500_000.0, reference=1_000_000.0))

    def test_a_refused_row_keeps_the_yen_quantities(self) -> None:
        """円の総額は株式基準に依存しない。落とすのは株数と per-share だけ。"""

        bars = [self._bar(date(2026, 4, 20), 0.5), self._bar(date(2026, 5, 15))]
        summaries = [
            _summary(
                "1111",
                date(2026, 2, 10),
                period_end=date(2025, 12, 31),
                shares_outstanding=1_000_000.0,
                treasury_shares=0.0,
            ),
            _summary(
                "1111",
                date(2026, 5, 15),
                period_end=date(2026, 3, 31),
                shares_outstanding=1_500_000.0,
                treasury_shares=0.0,
                total_assets=5e8,
                equity_to_asset_ratio=0.5,
            ),
        ]

        row = _normalize_summaries_to_asof_basis(summaries, bars, date(2026, 5, 29))[1]

        self.assertIsNone(row.shares_outstanding)
        self.assertIsNone(row.bps)
        self.assertEqual(row.total_assets, 5e8)
        self.assertEqual(row.equity_to_asset_ratio, 0.5)

    def test_zero_dividend_remains_the_latest_actual_anchor_when_basis_is_unknown(self) -> None:
        bars = [self._bar(date(2026, 4, 20), 0.5), self._bar(date(2026, 5, 15))]
        prior = _summary(
            "1111",
            date(2025, 5, 15),
            fiscal_period="FY",
            fiscal_year_end=date(2025, 3, 31),
            period_start=date(2024, 4, 1),
            period_end=date(2025, 3, 31),
            shares_outstanding=1_000_000.0,
        )
        latest = replace(
            _summary(
                "1111",
                date(2026, 5, 15),
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                period_start=date(2025, 4, 1),
                period_end=date(2026, 3, 31),
                shares_outstanding=1_500_000.0,
                dps_actual_annual=0.0,
                dps_forecast_annual=0.0,
            ),
            sales=None,
            cfo=None,
            cash_eq=None,
            total_assets=None,
            equity=None,
            operating_profit=None,
            ordinary_profit=None,
            profit=None,
            cumulative_eps=None,
            bps=None,
            equity_to_asset_ratio=None,
            average_shares=None,
        )
        normalized = _normalize_summaries_with_status([prior, latest], bars, date(2026, 5, 29))
        self.assertEqual(normalized.summaries[1].dps_actual_annual, 0.0)
        self.assertEqual(normalized.summaries[1].dps_forecast_annual, 0.0)
        self.assertIsNone(normalized.summaries[1].shares_outstanding)
        self.assertIsNone(normalized.summaries[1].treasury_shares)
        positive_forecast = replace(latest, dps_forecast_annual=10.0)
        positive_normalized = _normalize_summaries_with_status(
            [prior, positive_forecast], bars, date(2026, 5, 29)
        )
        self.assertEqual(positive_normalized.summaries[1].dps_actual_annual, 0.0)
        self.assertIsNone(positive_normalized.summaries[1].dps_forecast_annual)
        self.assertIs(_actual_rows(normalized.summaries)[-1], normalized.summaries[1])
        self.assertEqual(
            _ttm_value(normalized.summaries, "profit", load_screening_rules().ttm),
            (None, TTMQuality.UNAVAILABLE),
        )

    def test_refused_capital_row_does_not_revive_older_share_basis(self) -> None:
        """判定不能な新capital stateより前の株数はmarket capへcarryしない。"""

        asof = date(2026, 5, 29)
        bars = [self._bar(date(2026, 5, 15))]
        events = [JQuantsAdjustmentFactorEvent("1111", date(2026, 4, 20), 0.5, "1")]
        summaries = [
            _summary(
                "1111",
                date(2026, 2, 10),
                period_end=date(2025, 12, 31),
                shares_outstanding=1_000_000.0,
                treasury_shares=100_000.0,
            ),
            _summary(
                "1111",
                date(2026, 5, 15),
                period_end=date(2026, 3, 31),
                shares_outstanding=1_500_000.0,
                treasury_shares=150_000.0,
            ),
        ]

        index = build_shares_outstanding_index(
            {"1111": summaries},
            {"1111": bars},
            asof,
            adjustment_events_by_ticker={"1111": events},
        )
        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"1111": _security("1111")},
            bars_by_ticker={"1111": bars},
            summaries_by_ticker={"1111": summaries},
            edinet_by_ticker={},
            adjustment_events_by_ticker={"1111": events},
        ).financials["1111"]

        self.assertIsNone(index["1111"])
        self.assertIsNone(snapshot.market_cap)
        self.assertEqual(snapshot.capital_basis_failure_reason, "indeterminate_share_basis")

    def test_per_share_only_refusal_does_not_block_safe_capital_carry(self) -> None:
        """株数を観測しない曖昧行は、確定済みcapital stateと競合しない。"""

        asof = date(2026, 5, 29)
        bars = [self._bar(date(2026, 5, 15))]
        events = [JQuantsAdjustmentFactorEvent("1111", date(2026, 4, 20), 0.5, "1")]
        capital = _summary(
            "1111",
            date(2026, 2, 10),
            period_end=date(2025, 12, 31),
            shares_outstanding=1_000_000.0,
            treasury_shares=100_000.0,
        )
        per_share_only = replace(
            _summary(
                "1111",
                date(2026, 5, 15),
                period_end=date(2026, 3, 31),
                shares_outstanding=1_000_000.0,
            ),
            shares_outstanding=None,
            treasury_shares=None,
            average_shares=None,
        )

        index = build_shares_outstanding_index(
            {"1111": [capital, per_share_only]},
            {"1111": bars},
            asof,
            adjustment_events_by_ticker={"1111": events},
        )

        self.assertEqual(index["1111"], 1_800_000.0)

    def test_new_complete_capital_row_resets_indeterminate_barrier(self) -> None:
        """曖昧行後にissuedとtreasuryを再観測すれば、その状態から再開する。"""

        asof = date(2026, 6, 30)
        events = [JQuantsAdjustmentFactorEvent("1111", date(2026, 4, 20), 0.5, "1")]
        summaries = [
            _summary(
                "1111",
                date(2026, 2, 10),
                period_end=date(2025, 12, 31),
                shares_outstanding=1_000_000.0,
                treasury_shares=100_000.0,
            ),
            _summary(
                "1111",
                date(2026, 5, 15),
                period_end=date(2026, 3, 31),
                shares_outstanding=1_500_000.0,
                treasury_shares=150_000.0,
            ),
            _summary(
                "1111",
                date(2026, 6, 15),
                period_end=date(2026, 3, 31),
                shares_outstanding=2_100_000.0,
                treasury_shares=100_000.0,
            ),
        ]

        index = build_shares_outstanding_index(
            {"1111": summaries},
            {"1111": [self._bar(asof)]},
            asof,
            adjustment_events_by_ticker={"1111": events},
        )

        self.assertEqual(index["1111"], 2_000_000.0)


class CapitalBasisStateMachineTests(unittest.TestCase):
    def test_increase_then_decrease_invalidates_old_positive_treasury(self) -> None:
        asof = date(2026, 7, 1)
        rows = [
            _summary(
                "1111",
                asof - timedelta(days=300),
                shares_outstanding=1_000.0,
                treasury_shares=100.0,
            ),
            _summary(
                "1111", asof - timedelta(days=200), shares_outstanding=1_200.0, treasury_shares=None
            ),
            _summary(
                "1111", asof - timedelta(days=20), shares_outstanding=1_100.0, treasury_shares=None
            ),
        ]

        result = _resolve_capital_basis(rows)

        self.assertIsNone(result.shares_ex_treasury)
        self.assertEqual(
            result.failure_reason,
            "indeterminate_treasury_after_later_issued_observation",
        )

    def test_complete_zero_pair_carries_when_neither_share_count_is_observed(self) -> None:
        current = _summary(
            "1111", date(2026, 5, 14), shares_outstanding=1_000.0, treasury_shares=0.0
        )
        later = replace(
            _summary("1111", date(2026, 6, 14)),
            shares_outstanding=None,
            treasury_shares=None,
        )
        result = _resolve_capital_basis([current, later])
        self.assertEqual(result.shares_ex_treasury, 1_000.0)
        self.assertIsNone(result.failure_reason)

    def test_future_summary_cannot_change_asof_share_index(self) -> None:
        asof = date(2026, 7, 1)
        current = _summary(
            "1111", asof - timedelta(days=20), shares_outstanding=1_000.0, treasury_shares=100.0
        )
        future = _summary(
            "1111", asof + timedelta(days=20), shares_outstanding=2_000.0, treasury_shares=100.0
        )

        index = build_shares_outstanding_index({"1111": [current, future]}, {"1111": []}, asof)

        self.assertEqual(index["1111"], 900.0)

    def test_same_day_average_share_revision_keeps_capital_state(self) -> None:
        day = date(2026, 7, 1)
        rows = [
            _summary(
                "1111", day, shares_outstanding=1_000.0, treasury_shares=100.0, average_shares=900.0
            ),
            _summary(
                "1111", day, shares_outstanding=1_000.0, treasury_shares=100.0, average_shares=850.0
            ),
        ]

        result = _resolve_capital_basis(rows)

        self.assertEqual(result.shares_ex_treasury, 900.0)
        self.assertIsNone(result.failure_reason)


class MissingCloseAdjustmentEventTests(unittest.TestCase):
    def test_event_without_price_normalizes_capital_and_price_series(self) -> None:
        """100:1併合日のclose欠損でも、eventを株数・価格の両経路へ適用する。"""

        asof = date(2024, 3, 29)
        event_day = date(2024, 2, 28)
        bars = [
            JQuantsDailyBar(
                "1111",
                asof - timedelta(days=89 - index),
                1.0 if asof - timedelta(days=89 - index) < event_day else 100.0,
                300_000_000.0,
            )
            for index in range(90)
            if asof - timedelta(days=89 - index) != event_day
        ]
        events = [JQuantsAdjustmentFactorEvent("1111", event_day, 100.0, "2")]
        summary = _summary(
            "1111",
            date(2024, 1, 31),
            shares_outstanding=100_000_000.0,
            treasury_shares=0.0,
            period_end=date(2023, 12, 31),
        )

        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"1111": _security("1111")},
            bars_by_ticker={"1111": bars},
            summaries_by_ticker={"1111": [summary]},
            edinet_by_ticker={},
            adjustment_events_by_ticker={"1111": events},
        )
        financial = result.financials["1111"]
        derived = result.derived["1111"]

        self.assertEqual(financial.shares_ex_treasury, 1_000_000.0)
        self.assertEqual(financial.market_cap, 100_000_000.0)
        self.assertIsNotNone(derived.price_change_60d)
        self.assertIsNotNone(derived.gap_from_52w_low)
        self.assertAlmostEqual(derived.price_change_60d, 0.0, places=12)
        self.assertAlmostEqual(derived.gap_from_52w_low, 0.0, places=12)


def test_the_edinet_balance_sheet_must_be_within_a_double_of_the_statement() -> None:
    """Pin the total-assets ratio filter, not proof of entity or consolidation identity.

    Similar total assets can belong to different accounting scopes; rejection outside
    this heuristic band also does not establish the cause of the discrepancy.
    """

    def passes_scale_check(edinet_total_assets: float) -> bool:
        return _edinet_passes_entity_scale_check(
            _edinet_metric_record(total_assets=edinet_total_assets), 1_000.0
        )

    assert passes_scale_check(2_000.0)
    assert passes_scale_check(500.0)
    assert not passes_scale_check(2_001.0)
    assert not passes_scale_check(499.0)


def test_the_common_equity_routes_must_agree_within_five_percent() -> None:
    """Pin the route-selection tolerance using a row with comparable inputs.

    Agreement within five percent permits the yen route under the policy; it does not
    prove accounting identity. Disagreement alone does not identify capital structure.
    """

    def equity(ratio: float) -> float | None:
        # The row's own two routes are what the comparison reads. bps 120.0 x 1,000,000
        # shares is 1.2e8, which is a 0.60 equity ratio on 2e8 of assets, so the band
        # runs from 0.57 to 0.63.
        summaries = [
            _summary(
                "130A",
                date(2026, 5, 10),
                fiscal_period="FY",
                shares_outstanding=1_000_000.0,
                treasury_shares=0.0,
                total_assets=2e8,
                equity_to_asset_ratio=ratio,
            )
        ]
        return _common_equity_yen(
            summaries,
            total_assets=2e8,
            equity_to_asset_ratio=ratio,
            bps=120.0,
            shares_ex_treasury=1_000_000.0,
        )

    # 0.629 is 4.8% from the common-basis route and 0.631 is 5.2%, so the pair
    # straddles the band closely enough that widening or narrowing it changes an answer.
    # The band's own edge, 0.63, is a float equality and is deliberately not asserted.
    inside = equity(0.629)
    outside = equity(0.631)

    assert inside is not None
    assert outside is not None
    # Inside the band the fresher yen route is kept, so the answer follows the ratio.
    assert abs(inside - 0.629 * 2e8) < 1.0
    # Outside it the common-basis route wins and the answer stops following the ratio.
    assert abs(outside - 1.2e8) < 1.0


def test_a_share_count_that_moved_more_than_a_fifth_after_the_split_is_not_classified() -> None:
    """The residual is what is left once the ratio is pulled onto the nearer basis.

    A split and a large issuance in the same period make the same ratio readable as
    either basis, and choosing wrong moves the share count by the split factor. Measured
    residuals stop at 12.4% and resume at 29.3%, so the cut sits in the empty 17 points
    between them, and a row past it is answered as indeterminate rather than guessed.
    The residuals below are literal, not derived from the constant.
    """

    # A 1:4 split: the two hypotheses are a factor of four apart, so the geometric
    # midpoint is far outside the residual band and this cut is the one that decides.
    interim = 0.25

    assert _closer_share_basis(1.19, interim) is _ShareBasis.AS_OF_PERIOD_END
    assert _closer_share_basis(0.81, interim) is _ShareBasis.AS_OF_PERIOD_END
    assert _closer_share_basis(1.26, interim) is _ShareBasis.INDETERMINATE
    assert _closer_share_basis(0.79, interim) is _ShareBasis.INDETERMINATE
