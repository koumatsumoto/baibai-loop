from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.engine.macro.indicator_fixtures import (
    _estat_payload,
    _registry_yaml,
    _series,
)

from baibai_engine.macro.indicators.cli import (
    main,
)
from baibai_engine.macro.indicators.definitions import (
    load_definitions,
)
from baibai_engine.macro.indicators.providers import (
    parse_estat_json,
    point_in_time_providers,
    registered_specs,
)
from baibai_engine.macro.indicators.providers.estat_dashboard import (
    source_url_selectors as estat_dashboard_selectors,
)
from baibai_engine.macro.indicators.providers.formulas import FORMULAS


class IndicatorsRegistryTests(unittest.TestCase):
    def test_point_in_time_read_contracts_match_registered_provider_specs(self) -> None:
        self.assertEqual(
            point_in_time_providers(),
            frozenset(spec.name for spec in registered_specs() if spec.point_in_time_vintage),
        )

    def test_canonical_registry_membership_has_a_known_generation(self) -> None:
        self.assertEqual(load_definitions().generation, 5)
        with (
            patch(
                "baibai_engine.macro.indicators.definitions._REGISTRY_MEMBERSHIP_GENERATIONS",
                {},
            ),
            self.assertRaisesRegex(ValueError, "without a new generation digest"),
        ):
            load_definitions()

    def test_new_tier1_series_registered_with_expected_provider_and_category(self) -> None:
        by_id = load_definitions().by_id()
        expected = {
            "jp.nikkei225": ("fred_csv", "equity-index"),
            "jp.policy_rate": ("boj_timeseries", "policy"),
            "jp.10y": ("mof_jgb", "rates"),
            "jp.unemployment": ("estat_dashboard", "labor"),
            "jp.nominal_wage_index": ("estat_dashboard", "labor"),
            "jp.real_effective_exchange_rate": ("fred_csv", "fx"),
            "jp.cpi.services": ("estat", "inflation"),
            "credit.us_hy_oas": ("fred_csv", "credit"),
            "credit.us_ccc_oas": ("fred_csv", "credit"),
            "btc_usd": ("fred_csv", "crypto"),
            "jp.machinery_orders": ("estat", "activity"),
            "jp.watcher_current_di": ("estat", "activity"),
            "jp.consumer_confidence": ("estat", "activity"),
            "jp.bank_lending_yoy": ("boj_timeseries", "monetary"),
            "jp.tankan_large_nonmfg_di": ("boj_timeseries", "activity"),
            "jp.real_wage_index": ("estat_dashboard", "labor"),
            "us.empire_manufacturing": ("fred_csv", "activity"),
            "us.philly_fed_manufacturing": ("fred_csv", "activity"),
            "jp.n225_iv_30d": ("jquants_options", "volatility"),
            "jp.n225_iv_skew": ("jquants_options", "volatility"),
            "jp.n225_iv_term": ("jquants_options", "volatility"),
        }

        for series_id, (provider, category) in expected.items():
            self.assertIn(series_id, by_id)
            self.assertEqual(by_id[series_id].provider, provider)
            self.assertEqual(by_id[series_id].category, category)

        self.assertEqual(
            by_id["jp.nominal_wage_index"].provider_series_id,
            "0302030202010090010",
        )
        self.assertEqual(
            by_id["jp.unemployment"].provider_series_id,
            "0301010000020020010",
        )

        self.assertEqual(
            by_id["jp.real_effective_exchange_rate"].provider_series_id,
            "RBJPBIS",
        )
        self.assertEqual(
            by_id["jp.cpi.services"].provider_series_id,
            "0004052037?cdCat01=0220&cdArea=00000&cdTab=1",
        )
        # The narrowing codes are the series identity for an e-Stat table that
        # carries dozens of series, so an edit to them changes what is stored
        # without changing anything else the tests look at.
        self.assertEqual(
            by_id["jp.machinery_orders"].provider_series_id,
            "0003355222?cdCat01=160&cdCat02=100&cdTab=100",
        )
        self.assertEqual(
            by_id["jp.watcher_current_di"].provider_series_id,
            "0003348423?cdCat01=100&cdCat02=100&cdTab=140",
        )
        self.assertEqual(
            by_id["jp.consumer_confidence"].provider_series_id,
            "0003446462?cdCat01=1060&cdTab=200",
        )
        self.assertEqual(
            by_id["jp.bank_lending_yoy"].provider_series_id,
            "MD13:FAAPOBAL1@",
        )

    def test_estat_series_narrow_on_dimensions_the_answer_can_be_checked_against(self) -> None:
        """A narrowing key the parser cannot verify belongs to no registry entry.

        The provider refuses such a key mid-fetch, which would surface as one
        failing series in the daily batch long after the registry edit.
        """

        empty = _estat_payload([])
        for series in load_definitions().series:
            if series.provider != "estat":
                continue
            with self.subTest(series=series.series_id):
                parse_estat_json(series, empty, start=date(1970, 1, 1), end=date(2100, 1, 1))

    def test_estat_dashboard_series_pin_one_upstream_series_in_their_source_url(self) -> None:
        """The registry, not a fetch, is where a mis-pinned selector must be caught.

        A selector the provider would reject only shows up in the daily batch as one
        failing series, so the registry entries are checked here instead.
        """

        dashboard = [
            definition
            for definition in load_definitions().series
            if definition.provider == "estat_dashboard"
        ]
        self.assertNotEqual(dashboard, [])

        for definition in dashboard:
            with self.subTest(series_id=definition.series_id):
                selectors = estat_dashboard_selectors(definition)
                self.assertEqual(definition.frequency, "monthly")
                self.assertEqual(selectors["IndicatorCode"], definition.provider_series_id)
                self.assertEqual(selectors["Cycle"], "1")
                self.assertIn(selectors["IsSeasonalAdjustment"], {"1", "2"})

    def test_every_series_id_maps_to_a_single_provider(self) -> None:
        series_ids = [series.series_id for series in load_definitions().series]

        self.assertEqual(len(series_ids), len(set(series_ids)))

    def test_us_equity_indices_registered(self) -> None:
        by_id = load_definitions().by_id()
        for series_id in ("us.sp500", "us.nasdaq", "us.dow"):
            self.assertIn(series_id, by_id)
            self.assertEqual(by_id[series_id].category, "equity-index")
            self.assertEqual(by_id[series_id].provider, "fred_csv")

    def test_tradingview_symbols_cover_major_market_series(self) -> None:
        by_id = load_definitions().by_id()
        configured = {
            series_id: definition.tradingview_symbol
            for series_id, definition in by_id.items()
            if definition.tradingview_symbol is not None
        }

        self.assertGreaterEqual(len(configured), 10)
        self.assertEqual(configured["us.10y"], "TVC:US10Y")
        self.assertEqual(configured["usd_jpy"], "FX:USDJPY")
        self.assertEqual(configured["vix"], "CBOE:VIX")
        self.assertEqual(configured["jp.nikkei225"], "TVC:NI225")
        self.assertEqual(configured["us.sp500"], "SP:SPX")
        self.assertIsNone(by_id["jp.pmi_manufacturing"].tradingview_symbol)

    def test_tradingview_symbol_rejects_invalid_format(self) -> None:
        canonical = Path("engine/src/baibai_engine/macro/indicators/registry/us.yaml").read_text(
            encoding="utf-8"
        )
        for invalid in ("invalid symbol", ":", "TVC:", ":US10Y", "A:B:C"):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as tmp:
                definitions = Path(tmp) / "us.yaml"
                definitions.write_text(
                    canonical.replace(
                        "tradingview_symbol: TVC:US10Y",
                        f'tradingview_symbol: "{invalid}"',
                        1,
                    ),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(ValueError, "EXCHANGE:SYMBOL"):
                    load_definitions(definitions)

    def test_registry_rejects_duplicate_series_id_across_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp)
            block = (
                "series:\n"
                "  - series_id: us.10y\n"
                "    name: dup\n"
                "    category: rates\n"
                "    geography: us\n"
                "    frequency: daily\n"
                "    unit: percent\n"
                "    provider: fred_csv\n"
                "    provider_series_id: DGS10\n"
                "    source_id: x\n"
                "    source_url: https://example.com/x.csv\n"
            )
            (registry / "a.yaml").write_text(block, encoding="utf-8")
            (registry / "b.yaml").write_text(block, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "duplicate indicator series_id.*us.10y"):
                load_definitions(registry)

    def test_registry_rejects_duplicate_yaml_mapping_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            definitions = Path(tmp) / "registry.yaml"
            definitions.write_text(
                "series:\n"
                "  - series_id: test.series\n"
                "    name: Test series\n"
                "    category: rates\n"
                "    geography: test\n"
                "    frequency: daily\n"
                "    unit: percent\n"
                "    provider: fred_csv\n"
                "    provider_series_id: TEST\n"
                "    source_id: test-source\n"
                "    source_url: https://example.com/test.csv\n"
                "    aliases: [first]\n"
                "    aliases: [second]\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "duplicate YAML mapping key: 'aliases'"):
                load_definitions(definitions)

    def test_registry_parses_optional_plausible_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            definitions = Path(tmp) / "registry.yaml"
            definitions.write_text(
                _registry_yaml(plausible_min="-2", plausible_max="20"),
                encoding="utf-8",
            )

            series = load_definitions(definitions).series[0]

            self.assertEqual(series.plausible_min, -2.0)
            self.assertEqual(series.plausible_max, 20.0)

    def test_registry_rejects_invalid_plausible_range(self) -> None:
        cases = (
            ("true", "20", "finite number"),
            ("low", "20", "finite number"),
            (".inf", "20", "finite number"),
            ("20", "-2", "less than or equal"),
        )
        for plausible_min, plausible_max, expected in cases:
            with self.subTest(plausible_min=plausible_min), tempfile.TemporaryDirectory() as tmp:
                definitions = Path(tmp) / "registry.yaml"
                definitions.write_text(
                    _registry_yaml(
                        plausible_min=plausible_min,
                        plausible_max=plausible_max,
                    ),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(ValueError, expected):
                    load_definitions(definitions)

    def test_series_definition_rejects_invalid_programmatic_plausible_range(self) -> None:
        for plausible_min, plausible_max, expected in (
            (float("nan"), 20.0, "plausible_min must be a finite number"),
            (-2.0, float("inf"), "plausible_max must be a finite number"),
            (True, 20.0, "plausible_min must be a finite number"),
            (20.0, -2.0, "plausible_min must be less than or equal"),
        ):
            with (
                self.subTest(plausible_min=plausible_min, plausible_max=plausible_max),
                self.assertRaisesRegex(ValueError, expected),
            ):
                _series(
                    "fred_csv",
                    "TEST",
                    plausible_min=plausible_min,
                    plausible_max=plausible_max,
                )

    def test_registry_rejects_alias_colliding_with_other_canonical_identity(self) -> None:
        for conflicting_alias in ("second.series", "Second series"):
            with (
                self.subTest(conflicting_alias=conflicting_alias),
                tempfile.TemporaryDirectory() as tmp,
            ):
                definitions = Path(tmp) / "registry.yaml"
                definitions.write_text(
                    "series:\n"
                    "  - series_id: first.series\n"
                    "    name: First series\n"
                    "    category: rates\n"
                    "    geography: test\n"
                    "    frequency: daily\n"
                    "    unit: percent\n"
                    "    provider: fred_csv\n"
                    "    provider_series_id: FIRST\n"
                    "    source_id: first-source\n"
                    "    source_url: https://example.com/first.csv\n"
                    f"    aliases: [{conflicting_alias}]\n"
                    "  - series_id: second.series\n"
                    "    name: Second series\n"
                    "    category: rates\n"
                    "    geography: test\n"
                    "    frequency: daily\n"
                    "    unit: percent\n"
                    "    provider: fred_csv\n"
                    "    provider_series_id: SECOND\n"
                    "    source_id: second-source\n"
                    "    source_url: https://example.com/second.csv\n",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    ValueError,
                    "indicator alias collides with another series_id or name",
                ):
                    load_definitions(definitions)

    def test_every_registered_series_provider_is_registered(self) -> None:
        from baibai_engine.macro.indicators.providers import provider_spec

        for series in load_definitions().series:
            with self.subTest(series_id=series.series_id):
                # resolve_provider (via provider_spec) raises for an unknown provider.
                self.assertEqual(provider_spec(series.provider).name, series.provider)

    def test_every_registered_series_declares_a_complete_plausible_range(self) -> None:
        for series in load_definitions().series:
            with self.subTest(series_id=series.series_id):
                self.assertIsNotNone(series.plausible_min)
                self.assertIsNotNone(series.plausible_max)

    def test_every_boj_series_binds_its_column_to_an_expected_header(self) -> None:
        boj_series = [series for series in load_definitions().series if series.provider == "boj"]

        self.assertGreater(len(boj_series), 0)
        for series in boj_series:
            with self.subTest(series_id=series.series_id):
                column, expected_header, metadata_column, expected_metadata = (
                    series.provider_series_id.split("|")
                )
                self.assertGreaterEqual(int(column), 2)
                self.assertTrue(expected_header.strip())
                self.assertGreaterEqual(int(metadata_column), 1)
                self.assertTrue(expected_metadata.strip())

    def test_derived_formula_and_registry_plausible_ranges_do_not_drift(self) -> None:
        registry = load_definitions().by_id()

        self.assertEqual(
            set(FORMULAS), {key for key, value in registry.items() if value.provider == "derived"}
        )
        for series_id, formula in FORMULAS.items():
            with self.subTest(series_id=series_id):
                series = registry[series_id]
                self.assertEqual(
                    (formula.plausible_min, formula.plausible_max),
                    (series.plausible_min, series.plausible_max),
                )


def test_catalog_never_opens_a_store(tmp_path, monkeypatch, capsys):
    def unexpected(*args, **kwargs):
        raise AssertionError("catalog opened a store")

    monkeypatch.setattr("baibai_engine.macro.indicators.db.open_connection", unexpected)
    monkeypatch.setattr("baibai_engine.macro.indicators.cli.load_project_env", unexpected)
    before = set(tmp_path.rglob("*"))
    monkeypatch.chdir(tmp_path)
    for command in (["list", "--format", "json"], ["search", "CPI"]):
        assert main(command) == 0
    assert set(tmp_path.rglob("*")) == before
    with pytest.raises(SystemExit):
        main(["list", "--db", str(tmp_path / "missing.sqlite")])
