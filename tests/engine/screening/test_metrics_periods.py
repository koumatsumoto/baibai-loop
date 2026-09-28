from __future__ import annotations

import unittest
from datetime import date

from tests.engine.screening.metric_fixtures import (
    _daily_bars,
    _security,
    _summary,
)

from baibai_engine.screening.metrics import (
    build_metrics,
)
from baibai_engine.screening.schema import OperatingProfitSource


class FinancialSummaryPeriodResolutionTests(unittest.TestCase):
    def test_completed_short_fiscal_year_outranks_earlier_quarter(self) -> None:
        earlier_quarter = _summary(
            "3823",
            date(2026, 1, 14),
            fiscal_period="1Q",
            fiscal_year_end=date(2026, 8, 31),
            period_start=date(2025, 9, 1),
            period_end=date(2025, 11, 30),
            sales=806_000_000.0,
            shares_outstanding=131_420_693.0,
        )
        completed_short_year = _summary(
            "3823",
            date(2026, 7, 24),
            fiscal_period="FY",
            fiscal_year_end=date(2026, 4, 30),
            period_start=date(2025, 9, 1),
            period_end=date(2026, 4, 30),
            sales=2_332_000_000.0,
            shares_outstanding=145_416_193.0,
        )
        asof = date(2026, 8, 12)

        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"3823": _security("3823")},
            bars_by_ticker={"3823": _daily_bars("3823", asof, 40)},
            summaries_by_ticker={"3823": [earlier_quarter, completed_short_year]},
            edinet_by_ticker={},
        ).financials["3823"]

        self.assertEqual(
            snapshot.latest_financial_disclosure_date, completed_short_year.disclosed_at
        )
        self.assertEqual(snapshot.sales, completed_short_year.sales)
        self.assertEqual(snapshot.shares_outstanding, completed_short_year.shares_outstanding)

    def test_partial_correction_keeps_specific_profit_from_the_same_period(self) -> None:
        statement = _summary(
            "9433",
            date(2025, 11, 6),
            fiscal_period="2Q",
            fiscal_year_end=date(2026, 3, 31),
            period_start=date(2025, 4, 1),
            period_end=date(2025, 9, 30),
            operating_profit=100.0,
            profit=80.0,
        )
        partial_correction = _summary(
            "9433",
            date(2025, 11, 7),
            fiscal_period="2Q",
            fiscal_year_end=date(2026, 3, 31),
            period_start=date(2025, 4, 1),
            period_end=date(2025, 9, 30),
            operating_profit=None,
            profit=70.0,
        )
        asof = date(2025, 11, 28)

        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"9433": _security("9433")},
            bars_by_ticker={"9433": _daily_bars("9433", asof, 40)},
            summaries_by_ticker={"9433": [statement, partial_correction]},
            edinet_by_ticker={},
        ).financials["9433"]

        self.assertEqual(snapshot.operating_profit, 100.0)
        self.assertEqual(snapshot.operating_profit_source, OperatingProfitSource.OPERATING_PROFIT)

    def test_future_period_metadata_cannot_outrank_a_later_valid_quarter(self) -> None:
        malformed_q1 = _summary(
            "9565",
            date(2026, 3, 12),
            fiscal_period="1Q",
            fiscal_year_end=date(2026, 6, 30),
            period_start=date(2025, 11, 1),
            period_end=date(2026, 6, 30),
            sales=781_000_000.0,
            operating_profit=49_000_000.0,
            shares_outstanding=2_775_933.0,
        )
        valid_q2 = _summary(
            "9565",
            date(2026, 6, 12),
            fiscal_period="2Q",
            fiscal_year_end=date(2026, 10, 31),
            period_start=date(2025, 11, 1),
            period_end=date(2026, 4, 30),
            sales=1_544_000_000.0,
            operating_profit=63_000_000.0,
            shares_outstanding=2_775_933.0,
        )
        asof = date(2026, 6, 30)

        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"9565": _security("9565")},
            bars_by_ticker={"9565": _daily_bars("9565", asof, 40)},
            summaries_by_ticker={"9565": [malformed_q1, valid_q2]},
            edinet_by_ticker={},
        ).financials["9565"]

        self.assertEqual(snapshot.latest_financial_disclosure_date, valid_q2.disclosed_at)
        self.assertEqual(snapshot.sales, valid_q2.sales)

    def test_latest_current_period_blank_withdraws_earnings_forecast(self) -> None:
        prior = _summary(
            "9433",
            date(2025, 8, 1),
            fiscal_period="1Q",
            fiscal_year_end=date(2026, 3, 31),
            period_start=date(2025, 4, 1),
            period_end=date(2025, 6, 30),
            forecast_eps=100.0,
            forecast_profit=1_000.0,
            forecast_ordinary_profit=900.0,
        )
        withdrawal = _summary(
            "9433",
            date(2025, 11, 6),
            fiscal_period="2Q",
            fiscal_year_end=date(2026, 3, 31),
            period_start=date(2025, 4, 1),
            period_end=date(2025, 9, 30),
            forecast_eps=None,
            forecast_profit=None,
            forecast_ordinary_profit=None,
        )
        asof = date(2025, 11, 28)

        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"9433": _security("9433")},
            bars_by_ticker={"9433": _daily_bars("9433", asof, 40)},
            summaries_by_ticker={"9433": [prior, withdrawal]},
            edinet_by_ticker={},
        ).financials["9433"]

        self.assertIsNone(snapshot.per_forward)
        self.assertFalse(snapshot.forecast_special_gain_flag)
        self.assertFalse(snapshot.forecast_full_year_loss_flag)

    def test_future_period_actual_remains_newer_than_prior_valid_year(self) -> None:
        prior = _summary(
            "3281",
            date(2025, 10, 14),
            fiscal_period="FY",
            fiscal_year_end=date(2025, 8, 31),
            period_start=date(2025, 3, 1),
            period_end=date(2025, 8, 31),
            sales=30_505_000_000.0,
            shares_outstanding=4_797_731.0,
        )
        current = _summary(
            "3281",
            date(2026, 4, 13),
            fiscal_period="FY",
            fiscal_year_end=date(2026, 8, 31),
            period_start=date(2026, 3, 1),
            period_end=date(2026, 8, 31),
            sales=28_821_000_000.0,
            shares_outstanding=4_797_731.0,
        )
        asof = date(2026, 4, 30)

        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"3281": _security("3281")},
            bars_by_ticker={"3281": _daily_bars("3281", asof, 40)},
            summaries_by_ticker={"3281": [prior, current]},
            edinet_by_ticker={},
        ).financials["3281"]

        self.assertEqual(snapshot.latest_financial_disclosure_date, current.disclosed_at)
        self.assertEqual(snapshot.sales, current.sales)
