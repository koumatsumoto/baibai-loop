from __future__ import annotations

import tempfile
import unittest
from dataclasses import asdict, fields
from datetime import date
from pathlib import Path

import yaml

from baibai_engine.screening.calibration.store import (
    CACHE_SCHEMA_VERSION,
    CalibrationCacheError,
    cache_meta_path,
    read_panel,
)
from baibai_engine.screening.margin_metrics import MarginSupplyDemand, margin_supply_demand
from baibai_engine.screening.providers.jquants import JQuantsWeeklyMargin
from baibai_engine.screening.schema import DerivedMetrics

WEEK = date(2026, 7, 24)


def _margin(**overrides: object) -> JQuantsWeeklyMargin:
    values: dict[str, object] = {
        "ticker": "7203",
        "week_end": WEEK,
        "long_vol": 1000.0,
        "short_vol": 250.0,
        "long_std_vol": 800.0,
        "long_neg_vol": 200.0,
        "short_std_vol": 200.0,
        "short_neg_vol": 50.0,
        "issue_type": "2",
    }
    values.update(overrides)
    return JQuantsWeeklyMargin(**values)  # type: ignore[arg-type]


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
        axes = _axes(latest=_margin(short_vol=0.0, short_std_vol=0.0, short_neg_vol=0.0))

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
        axes = _axes(latest=_margin(long_vol=0.0, long_std_vol=0.0, long_neg_vol=0.0))

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


class CalibrationCacheVersionTest(unittest.TestCase):
    """A panel written under an older field set must be refused up front."""

    def test_a_panel_from_an_older_cache_version_is_refused_before_it_is_read(self) -> None:
        # Adding a column to PanelRow without bumping the cache version leaves the
        # old CSVs readable-looking; the failure then surfaces mid-build as a
        # missing-column error on one cohort instead of as "rebuild the cache".
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_meta_path(root).parent.mkdir(parents=True, exist_ok=True)
            cache_meta_path(root).write_text(
                yaml.safe_dump({"cache_schema_version": CACHE_SCHEMA_VERSION - 1}),
                encoding="utf-8",
            )
            (root / "panel-2020-01-31.csv").write_text("asof,ticker\n2020-01-31,7203\n", "utf-8")

            with self.assertRaises(CalibrationCacheError) as caught:
                read_panel(root, date(2020, 1, 31))

            self.assertIn("cache version is incompatible", str(caught.exception))


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


if __name__ == "__main__":
    unittest.main()
