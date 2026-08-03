from __future__ import annotations

import copy
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from baibai_engine.screening.earnings_lag import (
    build_earnings_lag,
    estimate_next_announcement,
    index_calendar_announcements,
    tickers_without_calendar_rows,
)
from baibai_engine.screening.providers.jquants import JQuantsFinancialSummary
from baibai_engine.screening.rule_config import DEFAULT_RULES_PATH, load_screening_rules
from baibai_engine.screening.selection import (
    build_selection_payload,
    candidate_record_from_mapping,
)

_ASOF = date(2026, 7, 31)


@dataclass(frozen=True, slots=True)
class _Entry:
    ticker: str
    announcement_date: date


def _summary(ticker: str, disclosed_at: str) -> JQuantsFinancialSummary:
    return JQuantsFinancialSummary(ticker=ticker, disclosed_at=date.fromisoformat(disclosed_at))


def _quarterly_history(ticker: str, dates: Sequence[str]) -> list[JQuantsFinancialSummary]:
    return [_summary(ticker, value) for value in dates]


def _latest(summaries: Mapping[str, Sequence[JQuantsFinancialSummary]], ticker: str) -> date | None:
    """What the snapshot builder reads: the newest disclosure in its own window."""
    history = summaries.get(ticker, ())
    return history[-1].disclosed_at if history else None


class EarningsLagTests(unittest.TestCase):
    def test_announcement_on_the_asof_without_an_ingested_summary_is_stale(self) -> None:
        # 7943 shape: the calendar points at the as-of itself and the store's newest
        # disclosure is still the previous quarter, so the row's financials predate
        # an announcement the market has already seen.
        summaries = {"7943": _quarterly_history("7943", ["2026-01-30", "2026-05-13"])}
        lag = build_earnings_lag(
            ticker="7943",
            asof=_ASOF,
            fin_latest_disclosed=_latest(summaries, "7943"),
            calendar_next=index_calendar_announcements(
                [_Entry("7943", date(2026, 7, 31))], asof=_ASOF
            ),
            summaries_by_ticker=summaries,
        )

        self.assertTrue(lag.stale_fin_flag)
        self.assertEqual(lag.fin_latest_disclosed_date, date(2026, 5, 13))

    def test_the_flag_clears_once_the_announced_quarter_is_ingested(self) -> None:
        summaries = {"7943": _quarterly_history("7943", ["2026-01-30", "2026-05-13", "2026-07-31"])}
        lag = build_earnings_lag(
            ticker="7943",
            asof=_ASOF,
            fin_latest_disclosed=_latest(summaries, "7943"),
            calendar_next=index_calendar_announcements(
                [_Entry("7943", date(2026, 7, 31))], asof=_ASOF
            ),
            summaries_by_ticker=summaries,
        )

        self.assertFalse(lag.stale_fin_flag)
        self.assertEqual(lag.fin_latest_disclosed_date, date(2026, 7, 31))

    def test_a_disclosure_a_few_days_ahead_of_the_scheduled_date_is_not_stale(self) -> None:
        # 3536 shape: the company disclosed on 7/13 against a 7/15 calendar date. The
        # row already carries that quarter, so reading the two-day gap as staleness
        # would flag a current row.
        summaries = {"3536": _quarterly_history("3536", ["2026-04-14", "2026-07-13"])}
        lag = build_earnings_lag(
            ticker="3536",
            asof=_ASOF,
            fin_latest_disclosed=_latest(summaries, "3536"),
            calendar_next=index_calendar_announcements(
                [_Entry("3536", date(2026, 7, 15))], asof=_ASOF
            ),
            summaries_by_ticker=summaries,
        )

        self.assertFalse(lag.stale_fin_flag)
        self.assertEqual(lag.fin_latest_disclosed_date, date(2026, 7, 13))

    def test_a_scheduled_future_announcement_is_never_stale(self) -> None:
        summaries = {"4849": _quarterly_history("4849", ["2026-02-12", "2026-05-14"])}
        lag = build_earnings_lag(
            ticker="4849",
            asof=_ASOF,
            fin_latest_disclosed=_latest(summaries, "4849"),
            calendar_next=index_calendar_announcements(
                [_Entry("4849", date(2026, 8, 6))], asof=_ASOF
            ),
            summaries_by_ticker=summaries,
        )

        self.assertFalse(lag.stale_fin_flag)
        self.assertIsNone(lag.next_earnings_estimated_date)

    def test_a_ticker_without_a_calendar_row_gets_an_estimated_date(self) -> None:
        # 4202 shape: the calendar has no row at all, so the next announcement is
        # projected from the same quarter one cycle earlier.
        summaries = {
            "4202": _quarterly_history(
                "4202",
                ["2025-08-06", "2025-11-06", "2026-02-10", "2026-05-12", "2026-07-31"],
            )
        }
        lag = build_earnings_lag(
            ticker="4202",
            asof=_ASOF,
            fin_latest_disclosed=_latest(summaries, "4202"),
            calendar_next=index_calendar_announcements([], asof=_ASOF),
            summaries_by_ticker=summaries,
        )

        self.assertFalse(lag.stale_fin_flag)
        self.assertEqual(lag.next_earnings_estimated_date, date(2026, 11, 6))

    def test_no_estimate_without_a_matching_prior_cycle(self) -> None:
        # A newly listed company has no same-quarter history: guessing here would
        # invent a date the store cannot support.
        summaries = {"9999": _quarterly_history("9999", ["2026-05-12", "2026-07-31"])}

        self.assertIsNone(estimate_next_announcement(summaries["9999"], asof=_ASOF))

    def test_the_reported_disclosure_is_the_one_the_row_read(self) -> None:
        # The annotation must not re-derive the date: a second derivation can drift
        # from the disclosure the snapshot actually built its financials from.
        summaries = {"1111": _quarterly_history("1111", ["2026-01-30", "2026-05-12"])}
        lag = build_earnings_lag(
            ticker="1111",
            asof=_ASOF,
            fin_latest_disclosed=date(2026, 5, 12),
            calendar_next=index_calendar_announcements([], asof=_ASOF),
            summaries_by_ticker=summaries,
        )

        self.assertEqual(lag.fin_latest_disclosed_date, date(2026, 5, 12))

    def test_a_past_calendar_row_wins_over_a_future_one(self) -> None:
        # Staleness is only answerable about an announcement that already happened.
        index = index_calendar_announcements(
            [_Entry("2222", date(2026, 11, 5)), _Entry("2222", date(2026, 7, 31))], asof=_ASOF
        )

        self.assertEqual(index, {"2222": date(2026, 7, 31)})

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

    def test_the_stale_row_is_tagged_and_carries_its_disclosure_date(self) -> None:
        payload = self._payload(
            [
                _candidate(
                    "1111",
                    stale_fin_flag=True,
                    fin_latest_disclosed_date="2026-05-13",
                )
            ]
        )
        recommendations = payload["recommendations"]
        assert isinstance(recommendations, list)
        row = recommendations[0]

        self.assertIn("stale_financials", row["risk_tags"])
        self.assertTrue(row["stale_fin_flag"])
        self.assertEqual(row["fin_latest_disclosed_date"], "2026-05-13")

    def test_an_unreadable_date_degrades_the_label_instead_of_the_selection(self) -> None:
        # An annotation label must not be able to fail the payload it annotates.
        payload = self._payload(
            [
                {**_candidate("1111"), "next_earnings_date": "not-a-date"},
                _candidate("2222", next_earnings_estimated_date="also-not-a-date"),
            ]
        )
        recommendations = payload["recommendations"]
        assert isinstance(recommendations, list)

        self.assertEqual(
            {item["ticker"]: item["next_earnings_status"] for item in recommendations},
            {"1111": "unknown", "2222": "unknown"},
        )
        self.assertEqual(recommendations[0]["next_earnings_date"], "not-a-date")

    def test_next_earnings_status_separates_scheduled_announced_and_estimated(self) -> None:
        payload = self._payload(
            [
                {**_candidate("1111"), "next_earnings_date": "2026-08-06"},
                {**_candidate("2222"), "next_earnings_date": _ASOF.isoformat()},
                _candidate("3333", next_earnings_estimated_date="2026-11-06"),
                _candidate("4444"),
            ]
        )
        recommendations = payload["recommendations"]
        assert isinstance(recommendations, list)
        status = {item["ticker"]: item["next_earnings_status"] for item in recommendations}

        self.assertEqual(
            status,
            {
                "1111": "scheduled",
                "2222": "announced",
                "3333": "estimated",
                "4444": "unknown",
            },
        )


if __name__ == "__main__":
    unittest.main()
