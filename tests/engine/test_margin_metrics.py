from __future__ import annotations

import tempfile
import unittest
from dataclasses import asdict, fields
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from baibai_engine.screening.calibration import store as calibration_store
from baibai_engine.screening.calibration.store import CalibrationCacheError, read_panel
from baibai_engine.screening.candidate_build import candidate_metrics_map
from baibai_engine.screening.margin_metrics import (
    MarginBalance,
    MarginSupplyDemand,
    margin_supply_demand,
)
from baibai_engine.screening.schema import DerivedMetrics, FinancialSnapshot

WEEK = date(2026, 7, 24)


def _margin(**overrides: object) -> MarginBalance:
    values: dict[str, object] = {
        "balance_date": WEEK,
        "long_vol": 1000.0,
        "short_vol": 250.0,
        "long_std_vol": 800.0,
        "issue_type": "2",
    }
    values.update(overrides)
    return MarginBalance(**values)  # type: ignore[arg-type]


def _axes(**overrides: object) -> MarginSupplyDemand:
    values: dict[str, object] = {
        "latest": _margin(),
        "prior_26w": _margin(long_vol=1500.0),
        "avg_daily_volume_shares": 500.0,
        "shares_outstanding": 100_000.0,
    }
    values.update(overrides)
    return margin_supply_demand(**values)  # type: ignore[arg-type]


class MarginSupplyDemandTest(unittest.TestCase):
    def test_axes_are_computed_from_the_published_balance(self) -> None:
        axes = _axes()

        self.assertEqual(axes.margin_week_end, WEEK)
        self.assertEqual(axes.margin_long_to_adv, 2.0)
        self.assertEqual(axes.margin_short_to_adv, 0.5)
        self.assertEqual(axes.margin_long_share, 0.8)
        self.assertEqual(axes.margin_long_delta_26w, -0.005)
        self.assertEqual(axes.margin_std_long_share, 0.8)

    def test_the_most_crowded_state_stays_in_the_cross_section(self) -> None:
        # A 貸借銘柄 with longs and no shorts is the extreme the axis exists to
        # rank. The long/short ratio is undefined there; the long share is 1.0.
        axes = _axes(latest=_margin(short_vol=0.0))

        self.assertEqual(axes.margin_long_share, 1.0)
        self.assertEqual(axes.margin_short_to_adv, 0.0)

    def test_a_lending_ineligible_name_has_no_long_share(self) -> None:
        # A 信用銘柄 carries no stock lending, so its zero short balance is the
        # instrument, not positioning; ranking it on a two-sided scale is wrong.
        axes = _axes(latest=_margin(issue_type="1", short_vol=0.0))

        self.assertIsNone(axes.margin_long_share)
        self.assertIsNone(axes.margin_short_to_adv)
        self.assertIsNotNone(axes.margin_long_to_adv)

    def test_a_split_inside_a_window_declines_that_axis(self) -> None:
        # Balances are reported on the balance date's share basis and are not
        # restated, so a split inside the window compares two different bases.
        adv = _axes(split_within_adv_window=True)
        delta = _axes(split_within_delta_window=True)

        self.assertIsNone(adv.margin_long_to_adv)
        self.assertIsNone(adv.margin_short_to_adv)
        self.assertIsNotNone(adv.margin_long_delta_26w)
        self.assertIsNone(delta.margin_long_delta_26w)
        self.assertIsNotNone(delta.margin_long_to_adv)

    def test_a_zero_long_balance_is_no_overhang_but_not_a_zero_share(self) -> None:
        axes = _axes(latest=_margin(long_vol=0.0, long_std_vol=0.0))

        self.assertEqual(axes.margin_long_to_adv, 0.0)
        self.assertIsNone(axes.margin_std_long_share)

    def test_an_unobserved_balance_leaves_every_axis_unset(self) -> None:
        axes = _axes(latest=None)

        self.assertEqual(axes, MarginSupplyDemand())

    def test_a_missing_prior_week_only_stops_the_delta(self) -> None:
        axes = _axes(prior_26w=None)

        self.assertIsNone(axes.margin_long_delta_26w)
        self.assertIsNotNone(axes.margin_long_to_adv)


class DerivedMetricsCarriesEveryAxisTest(unittest.TestCase):
    def test_every_supply_demand_field_reaches_derived_metrics(self) -> None:
        # `DerivedMetrics` silently drops unknown keys, so a rename on one side
        # would leave the axis permanently None with nothing failing. This pins
        # the two field sets together.
        supply_demand = {field.name for field in fields(MarginSupplyDemand)}
        derived = {field.name for field in fields(DerivedMetrics)}

        self.assertEqual(supply_demand - derived, set())

    def test_the_splat_carries_values_through_rather_than_dropping_them(self) -> None:
        derived = DerivedMetrics(**asdict(_axes()))

        self.assertEqual(derived.margin_long_to_adv, 2.0)
        self.assertEqual(derived.margin_short_to_adv, 0.5)
        self.assertEqual(derived.margin_long_share, 0.8)
        self.assertEqual(derived.margin_long_delta_26w, -0.005)
        self.assertEqual(derived.margin_std_long_share, 0.8)
        self.assertEqual(derived.margin_week_end, WEEK)

    def test_candidate_output_carries_short_to_adv_and_observation_week(self) -> None:
        metrics = candidate_metrics_map(
            FinancialSnapshot(
                latest_disclosed_at=None,
                per_forward=None,
                per_trailing=None,
                pbr=None,
                ev_ebitda=None,
                p_s=None,
                pcfr=None,
                eps=None,
                sales_ttm=None,
                ocf_ttm=None,
            ),
            freshness_warning_count=0,
            derived=DerivedMetrics(margin_week_end=WEEK, margin_short_to_adv=0.5),
        )

        self.assertEqual(metrics["margin_week_end"], WEEK.isoformat())
        self.assertEqual(metrics["margin_short_to_adv"], 0.5)


class CalibrationCacheVersionTest(unittest.TestCase):
    """A panel written under an older field set must be refused up front."""

    def test_a_panel_from_an_older_cache_version_is_refused_before_it_is_read(self) -> None:
        # Adding a column to PanelRow without bumping the cache version leaves the
        # stored rows readable-looking; the failure then surfaces mid-build as a
        # missing-column error on one cohort instead of as "rebuild the cache".
        from tests.helpers.calibration_store import publish_panel

        from baibai_engine.screening.calibration.lake import CALIBRATION_PANEL

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            publish_panel(root, "2020-01-31", [{"ticker": "7203"}])
            read_panel(root, date(2020, 1, 31))

            with (
                mock.patch.dict(
                    calibration_store.CACHE_SCHEMA_VERSIONS,
                    {CALIBRATION_PANEL.name: "0" * 16},
                ),
                self.assertRaises(CalibrationCacheError) as caught,
            ):
                read_panel(root, date(2020, 1, 31))

            self.assertIn("different transform", str(caught.exception))


class PublishedWeekReadabilityTest(unittest.TestCase):
    """The list of published balance dates must only hold dates the join can read."""

    @staticmethod
    def _seed(db: Path, week_ends: list[date], trading_days: list[date]) -> None:
        from baibai_engine.screening.sqlite_cache import (
            open_connection,
            store_jquants_weekly_margin,
        )

        for week_end in week_ends:
            store_jquants_weekly_margin(db, [{"Code": "72030", "LongVol": 1.0}], week_end=week_end)
        conn = open_connection(db)
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO jquants_daily_bars"
                "(ticker, traded_at, close, adjustment_close) VALUES (?, ?, ?, ?)",
                [("7203", day.isoformat(), 1.0, 1.0) for day in trading_days],
            )
            conn.commit()
        finally:
            conn.close()

    def test_a_balance_date_after_the_asof_is_not_published_yet(self) -> None:
        # A store filled past the decision date holds balance dates the market has not
        # reached. Listing one would put a future week at the head of the cohort, which
        # is the point-in-time break this reader exists to prevent.
        from baibai_engine.screening.sqlite_reader import published_margin_week_ends

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            weeks = [date(2026, 7, 17), date(2026, 7, 24), date(2026, 7, 31)]
            trading = [
                date(2026, 7, 17),
                *(date(2026, 7, day) for day in (20, 21, 22, 23, 24, 27, 28, 29, 30, 31)),
                *(date(2026, 8, day) for day in (3, 4, 5, 6, 7)),
            ]
            self._seed(db, weeks, trading)

            # 2026-07-31's own publication day has not been reached either, so this
            # holds whether the read filters on the balance date or on the lag.
            self.assertEqual(
                published_margin_week_ends(db, date(2026, 7, 30)),
                [date(2026, 7, 17), date(2026, 7, 24)],
            )

    def test_a_partial_week_falls_back_instead_of_blanking_the_cohort(self) -> None:
        # One malformed code in one weekly payload marks that week `partial`, which
        # the reader refuses. If the published list still named it, the newest entry
        # would read back empty and every axis would be None for the whole cohort
        # even though every earlier week is intact.
        from baibai_engine.screening.sqlite_cache import open_connection
        from baibai_engine.screening.sqlite_reader import (
            published_margin_week_ends,
            read_margin_supply_demand_inputs,
        )

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            weeks = [date(2026, 7, 17), date(2026, 7, 24)]
            trading = [
                date(2026, 7, 17),
                *(date(2026, 7, day) for day in (20, 21, 22, 23, 24, 27, 28, 29, 30)),
            ]
            self._seed(db, weeks, trading)
            asof = date(2026, 7, 30)
            self.assertEqual(published_margin_week_ends(db, asof), weeks)

            conn = open_connection(db)
            try:
                conn.execute(
                    "UPDATE source_coverage SET status = 'partial' "
                    "WHERE source = 'jquants_weekly_margin' AND coverage_start = '2026-07-24'"
                )
                conn.commit()
            finally:
                conn.close()

            self.assertEqual(published_margin_week_ends(db, asof), [date(2026, 7, 17)])
            latest, _ = read_margin_supply_demand_inputs(db, asof)
            self.assertEqual(sorted(latest), ["7203"])

    def test_a_balance_date_too_old_to_describe_the_asof_is_refused(self) -> None:
        from baibai_engine.screening.sqlite_reader import read_margin_supply_demand_inputs

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            week = date(2026, 1, 30)
            trading = [week, date(2026, 2, 2), date(2026, 2, 3), date(2026, 2, 4)]
            self._seed(db, [week], [*trading, date(2026, 7, 30)])

            fresh, _ = read_margin_supply_demand_inputs(db, date(2026, 2, 4))
            stale, _ = read_margin_supply_demand_inputs(db, date(2026, 7, 30))

            self.assertEqual(sorted(fresh), ["7203"])
            self.assertEqual(stale, {})


class MarginPublicationSeamTest(unittest.TestCase):
    """The supply/demand column has to cross the 2026-09-18 freeze without going dark.

    The weekly balance stops at 2026-09-18 and the all-issues daily balance starts at
    2026-09-25, so a column that only reads the weekly table answers for 35 more days
    and then unsets the margin axes for every ticker. These tests seed both series and
    ask what the column says on either side of that date.
    """

    LAST_WEEKLY = date(2026, 9, 18)
    FIRST_DAILY = date(2026, 9, 25)

    @staticmethod
    def _weekdays(start: date, end: date) -> list[date]:
        days: list[date] = []
        day = start
        while day <= end:
            if day.weekday() < 5:
                days.append(day)
            day += timedelta(days=1)
        return days

    @classmethod
    def _seed(cls, db: Path, *, weekly_from: date, daily_through: date) -> None:
        """Weekly balances through the freeze, daily balances after it, bars for both."""
        from baibai_engine.screening.sqlite_cache import (
            open_connection,
            store_jquants_all_issues_daily_margin,
            store_jquants_weekly_margin,
        )

        trading = cls._weekdays(weekly_from, daily_through)
        conn = open_connection(db)
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO jquants_daily_bars"
                "(ticker, traded_at, close, adjustment_close) VALUES (?, ?, ?, ?)",
                [("7203", day.isoformat(), 1.0, 1.0) for day in trading],
            )
            conn.commit()
        finally:
            conn.close()

        last_of_week: dict[tuple[int, int], date] = {}
        for day in trading:
            if day <= cls.LAST_WEEKLY:
                last_of_week[day.isocalendar()[:2]] = day
        for week_end in sorted(last_of_week.values()):
            store_jquants_weekly_margin(
                db,
                [{"Code": "72030", "LongVol": 1000.0, "IssType": "2"}],
                week_end=week_end,
            )
        for day in trading:
            if day < cls.FIRST_DAILY:
                continue
            store_jquants_all_issues_daily_margin(
                db,
                [
                    {
                        "Code": "72030",
                        "Date": day.isoformat(),
                        "LongVol": 2000.0,
                        "ShrtVol": 250.0,
                        "LongStdVol": 800.0,
                        "LongNegVol": 1200.0,
                        "ShrtStdVol": 200.0,
                        "ShrtNegVol": 50.0,
                        "IssType": "2",
                    }
                ],
                balance_date=day,
            )

    def test_the_flag_off_column_holds_no_daily_balance_date(self) -> None:
        """Inert means inert: with the flag false the daily rows are seeded and unread."""
        from baibai_engine.screening.sqlite_reader import (
            published_margin_balance_dates,
            published_margin_week_ends,
            read_margin_supply_demand_inputs,
        )

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            self._seed(db, weekly_from=date(2026, 3, 2), daily_through=date(2026, 10, 5))
            asof = date(2026, 10, 8)

            weekly = published_margin_week_ends(db, asof)
            column = published_margin_balance_dates(db, asof, publication_confirmed=False)

            self.assertEqual(column, [(day, "weekly") for day in weekly])
            self.assertEqual(weekly[-1], self.LAST_WEEKLY)

            latest, prior = read_margin_supply_demand_inputs(db, asof, publication_confirmed=False)

            self.assertEqual({row.balance_date for row in latest.values()}, {self.LAST_WEEKLY})
            # The 26-week reach is the same list index the weekly-only reader used.
            self.assertEqual(
                {row.balance_date for row in prior.values()},
                {weekly[-1 - 26]},
            )

    def test_the_axes_go_dark_after_the_freeze_and_the_daily_series_keeps_them_lit(self) -> None:
        """The failure this exists to prevent, and the same store answering with the flag on."""
        from baibai_engine.screening.sqlite_reader import read_margin_supply_demand_inputs

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            self._seed(db, weekly_from=date(2026, 3, 2), daily_through=date(2026, 10, 30))
            # 2026-09-18 + MARGIN_MAX_STALE_DAYS lands on 2026-10-23, so this as-of is
            # the first Monday on which the weekly-only column has nothing to say.
            asof = date(2026, 10, 26)

            self.assertEqual(
                read_margin_supply_demand_inputs(db, asof, publication_confirmed=False),
                ({}, {}),
            )

            latest, _ = read_margin_supply_demand_inputs(db, asof, publication_confirmed=True)

            self.assertEqual(sorted(latest), ["7203"])
            observed = {row.balance_date for row in latest.values()}
            self.assertEqual(observed, {date(2026, 10, 22)})

    def test_a_daily_balance_is_unusable_until_its_publication_day_has_closed(self) -> None:
        """Next business day, and not at that day's own close."""
        from baibai_engine.screening.sqlite_reader import published_margin_balance_dates

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            self._seed(db, weekly_from=date(2026, 9, 1), daily_through=date(2026, 9, 30))

            def daily_dates(asof: date) -> list[date]:
                return [
                    day
                    for day, cadence in published_margin_balance_dates(
                        db, asof, publication_confirmed=True
                    )
                    if cadence == "daily"
                ]

            # 2026-09-25 is published on 2026-09-28, the next trading day.
            self.assertEqual(daily_dates(date(2026, 9, 28)), [])
            self.assertEqual(daily_dates(date(2026, 9, 29)), [date(2026, 9, 25)])
            self.assertEqual(daily_dates(date(2026, 9, 30)), [date(2026, 9, 25), date(2026, 9, 28)])

    def test_the_delta_keeps_reaching_back_half_a_year_once_the_cadence_changes(self) -> None:
        """26 rows back into a daily column would be five weeks, not half a year."""
        from baibai_engine.screening.sqlite_reader import read_margin_supply_demand_inputs

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            self._seed(db, weekly_from=date(2026, 1, 5), daily_through=date(2026, 10, 30))
            asof = date(2026, 11, 2)

            latest, prior = read_margin_supply_demand_inputs(db, asof, publication_confirmed=True)

            observed = next(iter(latest.values())).balance_date
            reached = next(iter(prior.values())).balance_date

            # 2026-10-30 is stored but published on 2026-11-02, which is `asof` itself.
            self.assertEqual(observed, date(2026, 10, 29))
            # Independent of the reader: 26 weeks before the observed balance date.
            self.assertLessEqual(abs((observed - reached).days - 26 * 7), 7)
            # And it therefore still lands in the weekly era rather than in the daily
            # rows, which is what "26 back" would have given.
            self.assertLess(reached, self.LAST_WEEKLY)


if __name__ == "__main__":
    unittest.main()
