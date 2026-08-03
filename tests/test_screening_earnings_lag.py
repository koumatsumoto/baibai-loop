from __future__ import annotations

import copy
import unittest
from collections.abc import Mapping, Sequence
from datetime import date

from baibai_engine.screening.earnings_lag import (
    EarningsLag,
    build_earnings_lag,
    estimate_next_announcement,
    index_calendar_announcements,
    tickers_without_calendar_rows,
)
from baibai_engine.screening.providers.jpx import JPXEarningsCalendarEntry
from baibai_engine.screening.providers.jquants import JQuantsFinancialSummary
from baibai_engine.screening.rule_config import DEFAULT_RULES_PATH, load_screening_rules
from baibai_engine.screening.selection import (
    build_selection_payload,
    candidate_record_from_mapping,
)

_ASOF = date(2026, 7, 31)


def _cycle(*disclosures: tuple[str, str]) -> list[JQuantsFinancialSummary]:
    """Quarterly history as (disclosed_at, period_end) pairs, oldest first."""
    return [
        JQuantsFinancialSummary(
            ticker="0000",
            disclosed_at=date.fromisoformat(disclosed_at),
            period_end=date.fromisoformat(period_end),
        )
        for disclosed_at, period_end in disclosures
    ]


def _lag(
    *,
    announcement_date: date | None,
    fin_latest_disclosed: date | None,
    summaries: Sequence[JQuantsFinancialSummary] = (),
) -> EarningsLag:
    return build_earnings_lag(
        asof=_ASOF,
        fin_latest_disclosed=fin_latest_disclosed,
        announcement_date=announcement_date,
        summaries=summaries,
    )


class StaleFinancialsTests(unittest.TestCase):
    def test_a_past_schedule_without_its_statement_is_flagged(self) -> None:
        # 4582 shape: the calendar says 7/30 but the row's newest disclosure is the
        # previous quarter, so the numbers predate a date the market already passed.
        lag = _lag(announcement_date=date(2026, 7, 30), fin_latest_disclosed=date(2026, 5, 7))

        self.assertIs(lag.stale_fin_flag, True)
        self.assertEqual(lag.next_earnings_status, "announced")

    def test_the_flag_clears_once_that_statement_lands(self) -> None:
        lag = _lag(announcement_date=date(2026, 7, 31), fin_latest_disclosed=date(2026, 7, 31))

        self.assertIs(lag.stale_fin_flag, False)
        self.assertEqual(lag.next_earnings_status, "announced")

    def test_a_disclosure_a_few_days_ahead_of_the_scheduled_date_is_not_stale(self) -> None:
        # 3536 shape: disclosed 7/13 against a 7/15 calendar date. Reading the two-day
        # gap as staleness would flag a row that already carries the quarter.
        lag = _lag(announcement_date=date(2026, 7, 15), fin_latest_disclosed=date(2026, 7, 13))

        self.assertIs(lag.stale_fin_flag, False)

    def test_a_future_schedule_is_not_evaluated_rather_than_clean(self) -> None:
        # Nothing has been checked here, and "not checked" must not read the same as
        # "checked and current".
        lag = _lag(announcement_date=date(2026, 8, 6), fin_latest_disclosed=date(2026, 5, 14))

        self.assertIsNone(lag.stale_fin_flag)
        self.assertEqual(lag.next_earnings_status, "scheduled")

    def test_a_row_without_financials_is_not_evaluated(self) -> None:
        # 9286 shape: no summary in the window at all. There is no figure to call
        # stale, and the empty disclosure date already says so.
        lag = _lag(announcement_date=date(2026, 7, 15), fin_latest_disclosed=None)

        self.assertIsNone(lag.stale_fin_flag)
        self.assertIsNone(lag.fin_latest_disclosed_date)

    def test_a_ticker_without_a_calendar_row_is_not_evaluated(self) -> None:
        lag = _lag(announcement_date=None, fin_latest_disclosed=date(2026, 7, 31))

        self.assertIsNone(lag.stale_fin_flag)


class NextEarningsStatusTests(unittest.TestCase):
    def test_an_early_disclosure_against_a_future_schedule_reads_as_announced(self) -> None:
        # 8040 shape: disclosed 7/31, calendar still points at 8/6. Calling that
        # "scheduled" makes the reader carry an event risk that has already passed.
        lag = _lag(announcement_date=date(2026, 8, 6), fin_latest_disclosed=date(2026, 7, 31))

        self.assertEqual(lag.next_earnings_status, "announced")

    def test_a_genuinely_forthcoming_schedule_reads_as_scheduled(self) -> None:
        lag = _lag(announcement_date=date(2026, 8, 6), fin_latest_disclosed=date(2026, 5, 14))

        self.assertEqual(lag.next_earnings_status, "scheduled")

    def test_a_missing_calendar_row_with_history_reads_as_estimated(self) -> None:
        lag = _lag(
            announcement_date=None,
            fin_latest_disclosed=date(2026, 7, 31),
            summaries=_cycle(
                ("2025-08-06", "2025-06-30"),
                ("2025-11-06", "2025-09-30"),
                ("2026-02-10", "2025-12-31"),
                ("2026-05-12", "2026-03-31"),
                ("2026-07-31", "2026-06-30"),
            ),
        )

        self.assertEqual(lag.next_earnings_status, "estimated")
        self.assertEqual(lag.next_earnings_estimated_date, date(2026, 11, 6))

    def test_no_calendar_row_and_no_usable_history_reads_as_unknown(self) -> None:
        lag = _lag(announcement_date=None, fin_latest_disclosed=None)

        self.assertEqual(lag.next_earnings_status, "unknown")
        self.assertIsNone(lag.next_earnings_estimated_date)

    def test_the_status_never_contradicts_the_staleness_flag(self) -> None:
        # A flagged row is by definition one whose announcement already happened.
        for announced, disclosed in (
            (date(2026, 7, 30), date(2026, 5, 7)),
            (date(2026, 7, 15), date(2026, 4, 14)),
            (date(2026, 7, 14), date(2026, 4, 14)),
        ):
            with self.subTest(announced=announced):
                lag = _lag(announcement_date=announced, fin_latest_disclosed=disclosed)
                self.assertIs(lag.stale_fin_flag, True)
                self.assertEqual(lag.next_earnings_status, "announced")


class EstimateTests(unittest.TestCase):
    def test_a_reissue_of_the_same_period_is_not_a_cycle_step(self) -> None:
        # Forecast revisions and re-issues land in the same table as the quarterly
        # statements. Counting one as the next quarter projects the wrong event —
        # here it would pick a date eight days later and then discard it as past.
        history = _cycle(
            ("2025-05-12", "2025-03-31"),
            ("2025-05-20", "2025-03-31"),
            ("2025-08-06", "2025-06-30"),
            ("2026-05-12", "2026-03-31"),
        )

        self.assertEqual(estimate_next_announcement(history, asof=_ASOF), date(2026, 8, 6))

    def test_no_estimate_without_a_matching_prior_cycle(self) -> None:
        history = _cycle(("2026-05-12", "2026-03-31"), ("2026-07-31", "2026-06-30"))

        self.assertIsNone(estimate_next_announcement(history, asof=_ASOF))

    def test_disclosures_after_the_asof_are_not_read(self) -> None:
        history = _cycle(
            ("2025-05-12", "2025-03-31"),
            ("2025-08-05", "2025-06-30"),
            ("2026-05-12", "2026-03-31"),
            ("2026-08-05", "2026-06-30"),
        )

        self.assertEqual(estimate_next_announcement(history, asof=_ASOF), date(2026, 8, 5))

    def test_a_leap_day_disclosure_shifts_to_a_date_that_exists(self) -> None:
        # 2/29 has no counterpart in the following year; the projection has to land
        # on a real date rather than raise out of the run.
        history = _cycle(
            ("2023-11-10", "2023-09-30"),
            ("2024-02-29", "2023-12-31"),
            ("2024-05-13", "2024-03-31"),
            ("2024-11-08", "2024-09-30"),
        )

        self.assertEqual(
            estimate_next_announcement(history, asof=date(2024, 12, 31)), date(2025, 2, 28)
        )


class CalendarIndexTests(unittest.TestCase):
    def test_every_calendar_row_is_indexed_by_its_ticker(self) -> None:
        index = index_calendar_announcements(
            [
                JPXEarningsCalendarEntry(ticker="1111", announcement_date=date(2026, 8, 6)),
                JPXEarningsCalendarEntry(ticker="2222", announcement_date=date(2026, 7, 31)),
            ]
        )

        self.assertEqual(index, {"1111": date(2026, 8, 6), "2222": date(2026, 7, 31)})

    def test_universe_tickers_without_calendar_rows_are_counted(self) -> None:
        count = tickers_without_calendar_rows(("1111", "2222", "3333"), {"2222": date(2026, 8, 6)})

        self.assertEqual(count, 2)


def _candidate(ticker: str, **metrics: object) -> Mapping[str, object]:
    return {
        "ticker": ticker,
        "name": f"name-{ticker}",
        "sector_33": "機械",
        "market_cap_oku": 300,
        "avg_turnover_oku": 2.0,
        "listing_span_days": 1200,
        "jpx_flags": [],
        "evidence_hits": [{"name": "cashflow-yield-discount"}],
        "metrics": {"ocf_yield": 0.11, "er_annual": 0.05, **metrics},
    }


class EarningsLagAnnotationIsolationTests(unittest.TestCase):
    """The lag annotations must not move ranking, E[r] or the selection gate."""

    def setUp(self) -> None:
        self.rules = load_screening_rules(DEFAULT_RULES_PATH)

    def _payload(self, candidates: Sequence[Mapping[str, object]]) -> dict[str, object]:
        return build_selection_payload(
            asof_date=_ASOF,
            candidates=tuple(candidate_record_from_mapping(item) for item in candidates),
            macro_context=None,
            rules=self.rules,
            top=10,
            profile="balanced",
            candidates_ref="test.yaml",
            macro_context_ref=None,
            longlist_top=10,
        )

    def test_annotations_do_not_change_the_selection_outcome(self) -> None:
        plain = [
            _candidate("1111", er_annual=0.09),
            _candidate("2222", er_annual=0.07),
            _candidate("3333", er_annual=0.05),
        ]
        annotated = copy.deepcopy(plain)
        annotated[1]["metrics"].update(  # type: ignore[union-attr]  # dict literal above
            {
                "stale_fin_flag": True,
                "next_earnings_status": "announced",
                "fin_latest_disclosed_date": "2026-05-13",
                "next_earnings_estimated_date": "2026-11-06",
            }
        )

        before = self._payload(plain)
        after = self._payload(annotated)

        def outcome(payload: dict[str, object]) -> list[tuple[object, object, object]]:
            recommendations = payload["recommendations"]
            assert isinstance(recommendations, list)
            return [(item["rank"], item["ticker"], item["er_annual"]) for item in recommendations]

        self.assertEqual(outcome(before), outcome(after))
        self.assertEqual(before["selection"], after["selection"])

    def test_the_judgment_surface_transcribes_what_earnings_lag_decided(self) -> None:
        lag = _lag(announcement_date=date(2026, 7, 30), fin_latest_disclosed=date(2026, 5, 7))
        payload = self._payload(
            [
                _candidate(
                    "1111",
                    stale_fin_flag=lag.stale_fin_flag,
                    next_earnings_status=lag.next_earnings_status,
                    fin_latest_disclosed_date="2026-05-07",
                )
            ]
        )
        recommendations = payload["recommendations"]
        assert isinstance(recommendations, list)
        row = recommendations[0]

        self.assertEqual(row["next_earnings_status"], "announced")
        self.assertIs(row["stale_fin_flag"], True)
        self.assertEqual(row["fin_latest_disclosed_date"], "2026-05-07")
        self.assertIn("stale_financials", row["risk_tags"])

    def test_a_flagged_row_reaches_the_longlist_event_warnings(self) -> None:
        # The longlist is the wider triage view the human reads; an annotation that
        # stops at the recommendation row never reaches the review surface.
        payload = self._payload([_candidate("1111", stale_fin_flag=True)])
        longlist = payload["longlist"]
        assert isinstance(longlist, list)

        self.assertIn("stale_financials", longlist[0]["event_warnings"])

    def test_an_unevaluated_row_is_not_reported_as_clean(self) -> None:
        payload = self._payload([_candidate("1111")])
        recommendations = payload["recommendations"]
        assert isinstance(recommendations, list)

        self.assertIsNone(recommendations[0]["stale_fin_flag"])
        self.assertIsNone(recommendations[0]["next_earnings_status"])


if __name__ == "__main__":
    unittest.main()
