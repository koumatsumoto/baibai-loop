from __future__ import annotations

from datetime import date
from unittest import TestCase

from tests.helpers.indicator_store import (
    observation,
)
from tests.helpers.network import _no_real_network as _no_real_network
from tests.helpers.provider_fixtures import (
    _series,
)

from baibai_engine.machine_analysis.observations.economic_indicators.definitions import (
    load_definitions,
)
from baibai_engine.machine_analysis.observations.economic_indicators.derived import derive_series
from baibai_engine.machine_analysis.observations.economic_indicators.formulas import FORMULAS
from baibai_engine.machine_analysis.observations.economic_indicators.observations import (
    ObservationRecord,
)


class DerivedReadingTests(TestCase):
    def test_derived_net_liquidity_formula_converts_units(self) -> None:
        definitions = load_definitions().by_id()
        self.assertEqual(definitions["us.fed_assets"].unit, "usd-million")
        self.assertEqual(definitions["us.tga"].unit, "usd-million")
        self.assertEqual(definitions["us.reverse_repo"].unit, "usd-billion")
        value = FORMULAS["us.net_liquidity"].compute(
            {"us.fed_assets": 8_000_000.0, "us.reverse_repo": 1_000.0, "us.tga": 500_000.0}
        )

        # Assets and TGA arrive in millions; RRP already arrives in billions.
        # (8,000,000 - 500,000) / 1000 - 1,000 = 6,500 billion.
        self.assertEqual(value, 6500.0)

    def test_net_liquidity_does_not_divide_billion_dollar_rrp_again(self) -> None:
        for assets, tga, rrp, expected in (
            (8_795_567.0, 689_569.0, 2366.798, 5739.2),
            (6_747_560.0, 880_253.0, 2.338, 5864.969),
        ):
            with self.subTest(rrp=rrp):
                actual = FORMULAS["us.net_liquidity"].compute(
                    {"us.fed_assets": assets, "us.tga": tga, "us.reverse_repo": rrp}
                )
                assert actual is not None
                self.assertAlmostEqual(actual, expected, places=6)

    def test_treasury_spread_uses_source_yields_and_converts_to_bp(self) -> None:
        series = load_definitions().by_id()["us.10y_2y_spread"]
        self.assertEqual(series.provider, "derived")
        rows = derive_series(
            series,
            {
                "us.10y": (
                    observation("us.10y", date(2026, 5, 1), 4.39),
                    observation("us.10y", date(2026, 5, 4), 3.45),
                    observation("us.10y", date(2026, 5, 5), 3.50),
                ),
                "us.2y": (
                    observation("us.2y", date(2026, 5, 1), 3.88),
                    observation("us.2y", date(2026, 5, 4), 3.95),
                ),
            },
            start=date(2026, 5, 1),
            end=date(2026, 5, 5),
        )
        self.assertEqual(
            [(row.observed_at, round(row.value, 6), row.unit) for row in rows],
            [(date(2026, 5, 1), 51.0, "bp"), (date(2026, 5, 4), -50.0, "bp")],
        )

    def test_derived_rate_diff_formula(self) -> None:
        value = FORMULAS["rate_diff.us_jp_10y"].compute({"us.10y": 4.55, "jp.10y": 2.715})

        self.assertAlmostEqual(value, 1.835, places=3)

    def test_derived_policy_path_gap_formulas(self) -> None:
        """Compute the yield-policy spread, without identifying pure policy expectations."""

        tightening = FORMULAS["us.policy_path_gap"].compute(
            {"us.2y": 4.37, "us.fed_funds.upper": 3.75, "us.fed_funds.lower": 3.5}
        )
        easing = FORMULAS["us.policy_path_gap"].compute(
            {"us.2y": 3.88, "us.fed_funds.upper": 4.5, "us.fed_funds.lower": 4.25}
        )
        normalization = FORMULAS["jp.policy_path_gap"].compute(
            {"jp.2y": 1.45, "jp.policy_rate": 0.978}
        )

        assert tightening is not None
        assert easing is not None
        assert normalization is not None
        self.assertAlmostEqual(tightening, 0.745, places=3)
        self.assertAlmostEqual(easing, -0.495, places=3)
        self.assertAlmostEqual(normalization, 0.472, places=3)

    def test_derived_terms_of_trade_formula(self) -> None:
        value = FORMULAS["jp.terms_of_trade"].compute(
            {"jp.export_price_index": 162.6, "jp.import_price_index": 196.6}
        )

        # 162.6 / 196.6 = 0.8271: ratio of indices relative to their base, not absolute prices.
        assert value is not None
        self.assertAlmostEqual(value, 0.8271, places=4)

    def test_derived_jp_erp_formula(self) -> None:
        value = FORMULAS["jp.erp"].compute({"jp.nikkei_per": 17.82, "jp.10y": 1.7})

        # 100 / 17.82 - 1.7 = 3.9117: Nikkei earnings yield minus the 10Y JGB
        assert value is not None
        self.assertAlmostEqual(value, 3.9117, places=3)

    def test_derived_gold_copper_skips_zero_divisor(self) -> None:
        self.assertIsNone(FORMULAS["gold_copper_ratio"].compute({"gold": 3000.0, "copper": 0.0}))

    def test_derived_aligns_inputs_and_skips_partial_dates(self) -> None:
        series = _series("derived", "rate_diff.us_jp_10y", unit="percent")
        store: dict[str, tuple[ObservationRecord, ...]] = {
            "us.10y": (
                observation("us.10y", date(2026, 7, 16), 4.53),
                observation("us.10y", date(2026, 7, 17), 4.55),
            ),
            # jp.10y missing 07-16 -> that date is skipped (no half-computed value)
            "jp.10y": (observation("jp.10y", date(2026, 7, 17), 2.715),),
        }

        observations = derive_series(
            series,
            store,
            start=date(2026, 7, 1),
            end=date(2026, 7, 31),
        )

        self.assertEqual([obs.observed_at for obs in observations], [date(2026, 7, 17)])
        self.assertAlmostEqual(observations[0].value, 1.835, places=3)

    def test_missing_inputs_produce_no_derived_value(self) -> None:
        series = _series("derived", "rate_diff.us_jp_10y", unit="percent")
        observations = derive_series(series, {}, start=date(2026, 7, 1), end=date(2026, 7, 31))
        self.assertEqual(observations, ())

    def test_derived_real_10y_proxy_formula(self) -> None:
        value = FORMULAS["jp.real_10y_proxy"].compute({"jp.10y": 1.62, "jp.cpi.core_yoy": 1.6})

        # 1.62 - 1.6 = 0.02: the nominal 10Y JGB barely clears core inflation
        assert value is not None
        self.assertAlmostEqual(value, 0.02, places=3)

    def test_derived_us_erp_uses_monthly_alignment(self) -> None:
        self.assertEqual(FORMULAS["us.erp"].alignment, "monthly")
        self.assertEqual(FORMULAS["us.erp"].monthly_observation_date, "latest_input")

    def test_derived_monthly_alignment_uses_month_end_of_daily_input(self) -> None:
        series = _series("derived", "jp.real_10y_proxy", unit="percent", frequency="monthly")
        store: dict[str, tuple[ObservationRecord, ...]] = {
            "jp.10y": (
                observation("jp.10y", date(2026, 5, 1), 1.50),
                observation("jp.10y", date(2026, 5, 29), 1.58),
                observation("jp.10y", date(2026, 6, 1), 1.60),
                observation("jp.10y", date(2026, 6, 30), 1.62),  # June month-end reading
            ),
            "jp.cpi.core_yoy": (
                observation("jp.cpi.core_yoy", date(2026, 5, 1), 1.5),
                observation("jp.cpi.core_yoy", date(2026, 6, 1), 1.6),
            ),
        }

        observations = derive_series(
            series,
            store,
            start=date(2026, 5, 1),
            end=date(2026, 6, 30),
        )

        # The existing real-yield proxy keeps its canonical month-start grid.
        self.assertEqual(
            [(obs.observed_at, round(obs.value, 3)) for obs in observations],
            [(date(2026, 5, 1), 0.08), (date(2026, 6, 1), 0.02)],
        )

    def test_us_erp_monthly_alignment_uses_latest_input_date(self) -> None:
        series = _series("derived", "us.erp", unit="percent", frequency="monthly")
        store: dict[str, tuple[ObservationRecord, ...]] = {
            "us.sp500_earnings_yield": (
                observation("us.sp500_earnings_yield", date(2026, 5, 1), 5.0),
            ),
            "us.10y": (observation("us.10y", date(2026, 5, 29), 4.0),),
        }

        observations = derive_series(
            series,
            store,
            start=date(2026, 5, 1),
            end=date(2026, 5, 31),
        )

        self.assertEqual(
            [(item.observed_at, item.value) for item in observations],
            [(date(2026, 5, 29), 1.0)],
        )

    def test_derived_monthly_alignment_skips_month_missing_an_input(self) -> None:
        series = _series("derived", "jp.real_10y_proxy", unit="percent", frequency="monthly")
        store: dict[str, tuple[ObservationRecord, ...]] = {
            "jp.10y": (
                observation("jp.10y", date(2026, 5, 29), 1.58),
                observation("jp.10y", date(2026, 6, 30), 1.62),  # June has a yield ...
            ),
            # ... but the fixture has no June CPI observation, so June must not emit a
            # half-computed proxy.
            "jp.cpi.core_yoy": (observation("jp.cpi.core_yoy", date(2026, 5, 1), 1.5),),
        }

        observations = derive_series(
            series,
            store,
            start=date(2026, 5, 1),
            end=date(2026, 6, 30),
        )

        self.assertEqual([obs.observed_at for obs in observations], [date(2026, 5, 1)])
