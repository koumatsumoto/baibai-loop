from __future__ import annotations

from datetime import date
from typing import cast

from tests.engine.market_context.indicators.indicator_fixtures import (
    _series,
)
from tests.engine.market_context.indicators.provider_fixtures import SnapshotFixtures
from tests.helpers.indicator_store import (
    observation,
)

from baibai_engine.market_context.indicators.definitions import load_definitions
from baibai_engine.market_context.indicators.observations import ObservationRecord
from baibai_engine.market_context.indicators.providers import (
    IndicatorsProviderError,
)
from baibai_engine.market_context.indicators.providers.base import (
    FetchContext,
    HttpSession,
)
from baibai_engine.market_context.indicators.providers.derived import DerivedProvider
from baibai_engine.market_context.indicators.providers.formulas import FORMULAS


class IndicatorsProviderParserTests(SnapshotFixtures):
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

    def test_derived_provider_aligns_inputs_and_skips_partial_dates(self) -> None:
        series = _series("derived", "rate_diff.us_jp_10y", unit="percent")
        store: dict[str, tuple[ObservationRecord, ...]] = {
            "us.10y": (
                observation("us.10y", date(2026, 7, 16), 4.53),
                observation("us.10y", date(2026, 7, 17), 4.55),
            ),
            # jp.10y missing 07-16 -> that date is skipped (no half-computed value)
            "jp.10y": (observation("jp.10y", date(2026, 7, 17), 2.715),),
        }
        context = FetchContext(store_reader=lambda sid, s, e: store[sid])

        observations = DerivedProvider().fetch(
            series,
            start=date(2026, 7, 1),
            end=date(2026, 7, 31),
            session=cast(HttpSession, object()),
            context=context,
        )

        self.assertEqual([obs.observed_at for obs in observations], [date(2026, 7, 17)])
        self.assertAlmostEqual(observations[0].value, 1.835, places=3)

    def test_derived_provider_requires_store_reader(self) -> None:
        series = _series("derived", "rate_diff.us_jp_10y", unit="percent")

        with self.assertRaisesRegex(IndicatorsProviderError, "store reader"):
            DerivedProvider().fetch(
                series,
                start=date(2026, 7, 1),
                end=date(2026, 7, 31),
                session=cast(HttpSession, object()),
                context=FetchContext(),
            )

    def test_derived_real_10y_proxy_formula(self) -> None:
        value = FORMULAS["jp.real_10y_proxy"].compute({"jp.10y": 1.62, "jp.cpi.core_yoy": 1.6})

        # 1.62 - 1.6 = 0.02: the nominal 10Y JGB barely clears core inflation
        assert value is not None
        self.assertAlmostEqual(value, 0.02, places=3)

    def test_derived_us_erp_uses_monthly_alignment(self) -> None:
        self.assertEqual(FORMULAS["us.erp"].alignment, "monthly")
        self.assertEqual(FORMULAS["us.erp"].monthly_observation_date, "latest_input")

    def test_derived_provider_monthly_alignment_uses_month_end_of_daily_input(self) -> None:
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
        context = FetchContext(store_reader=lambda sid, s, e: store[sid])

        observations = DerivedProvider().fetch(
            series,
            start=date(2026, 5, 1),
            end=date(2026, 6, 30),
            session=cast(HttpSession, object()),
            context=context,
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

        observations = DerivedProvider().fetch(
            series,
            start=date(2026, 5, 1),
            end=date(2026, 5, 31),
            session=cast(HttpSession, object()),
            context=FetchContext(store_reader=lambda sid, s, e: store[sid]),
        )

        self.assertEqual(
            [(item.observed_at, item.value) for item in observations],
            [(date(2026, 5, 29), 1.0)],
        )

    def test_derived_provider_monthly_alignment_skips_month_missing_an_input(self) -> None:
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
        context = FetchContext(store_reader=lambda sid, s, e: store[sid])

        observations = DerivedProvider().fetch(
            series,
            start=date(2026, 5, 1),
            end=date(2026, 6, 30),
            session=cast(HttpSession, object()),
            context=context,
        )

        self.assertEqual([obs.observed_at for obs in observations], [date(2026, 5, 1)])
