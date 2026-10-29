from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pytest
from tests.helpers.network import _no_real_network as _no_real_network
from tests.helpers.provider_fixtures import (
    _estat_payload,
    _registry_yaml,
    _series,
)

from baibai_engine.cli.economic_indicators import (
    main,
)
from baibai_engine.data_collection.catalog import require_source
from baibai_engine.data_collection.provider_registry import provider_spec, registered_specs
from baibai_engine.data_collection.providers.estat.api import parse_estat_json
from baibai_engine.data_collection.providers.estat.dashboard import (
    source_url_selectors as estat_dashboard_selectors,
)
from baibai_engine.machine_analysis.observations.economic_indicators.definitions import (
    load_definitions,
)
from baibai_engine.machine_analysis.observations.economic_indicators.formulas import FORMULAS
from baibai_engine.machine_analysis.observations.economic_indicators.read_contracts import (
    point_in_time_providers,
)


class IndicatorsRegistryTests(unittest.TestCase):
    def test_point_in_time_read_contracts_match_registered_provider_specs(self) -> None:
        self.assertEqual(
            point_in_time_providers(),
            frozenset(spec.name for spec in registered_specs() if spec.point_in_time_vintage),
        )

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
            by_id["jp.nominal_wage_index"].reading_key,
            "0302030202010090010",
        )
        self.assertEqual(
            by_id["jp.unemployment"].reading_key,
            "0301010000020020010",
        )

        self.assertEqual(
            by_id["jp.real_effective_exchange_rate"].reading_key,
            "RBJPBIS",
        )
        self.assertEqual(
            by_id["jp.cpi.services"].reading_key,
            "0004052037?cdCat01=0220&cdArea=00000&cdTab=1",
        )
        # The narrowing codes are part of the Collection request identity.
        # L2 binds to that selected native series; it does not narrow a full table.
        self.assertEqual(
            by_id["jp.machinery_orders"].reading_key,
            "0003355222?cdCat01=160&cdCat02=100&cdTab=100",
        )
        self.assertEqual(
            by_id["jp.watcher_current_di"].reading_key,
            "0003348423?cdCat01=100&cdCat02=100&cdTab=140",
        )
        self.assertEqual(
            by_id["jp.consumer_confidence"].reading_key,
            "0003446462?cdCat01=1060&cdTab=200",
        )
        self.assertEqual(
            by_id["jp.bank_lending_yoy"].reading_key,
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
                source = require_source(series.source_id)
                self.assertEqual(series.reading_key, source.provider_series_id)
                parse_estat_json(source, empty, start=date(1970, 1, 1), end=date(2100, 1, 1))

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
                source = require_source(definition.source_id)
                selectors = estat_dashboard_selectors(source)
                self.assertEqual(definition.frequency, "monthly")
                self.assertEqual(selectors["IndicatorCode"], definition.reading_key)
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
        canonical = Path(
            "engine/src/baibai_engine/machine_analysis/observations/economic_indicators/registry/us.yaml"
        ).read_text(encoding="utf-8")
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
                "    reading_key: DGS10\n"
                "    source_id: fed-h15\n"
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
                "    reading_key: TEST\n"
                "    source_id: fed-h15\n"
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
                    "    reading_key: FIRST\n"
                    "    source_id: fed-h15\n"
                    f"    aliases: [{conflicting_alias}]\n"
                    "  - series_id: second.series\n"
                    "    name: Second series\n"
                    "    category: rates\n"
                    "    geography: test\n"
                    "    frequency: daily\n"
                    "    unit: percent\n"
                    "    reading_key: SECOND\n"
                    "    source_id: fed-h15\n",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    ValueError,
                    "indicator alias collides with another series_id or name",
                ):
                    load_definitions(definitions)

    def test_only_external_series_have_acquisition_providers(self) -> None:
        from baibai_engine.data_collection.provider_registry import provider_spec
        from baibai_engine.data_collection.transport.context import IndicatorsProviderError

        for series in load_definitions().series:
            with self.subTest(series_id=series.series_id):
                if series.provider == "derived":
                    with self.assertRaisesRegex(
                        IndicatorsProviderError, "unsupported indicator provider: derived"
                    ):
                        provider_spec(series.provider)
                else:
                    self.assertEqual(provider_spec(series.provider).name, series.provider)

    def test_registered_ranges_leave_the_matsui_publication_unbounded(self) -> None:
        for series in load_definitions().series:
            with self.subTest(series_id=series.series_id):
                if series.provider == "matsui_margin":
                    # Keep the published sign/value without assuming a denominator
                    # or an economic cutoff from the introductory formula.
                    self.assertIsNone(series.plausible_min)
                    self.assertIsNone(series.plausible_max)
                    continue
                self.assertIsNotNone(series.plausible_min)
                self.assertIsNotNone(series.plausible_max)

    def test_every_boj_series_binds_its_column_to_an_expected_header(self) -> None:
        boj_series = [series for series in load_definitions().series if series.provider == "boj"]

        self.assertGreater(len(boj_series), 0)
        for series in boj_series:
            with self.subTest(series_id=series.series_id):
                column, expected_header, metadata_column, expected_metadata = (
                    series.reading_key.split("|")
                )
                self.assertGreaterEqual(int(column), 2)
                self.assertTrue(expected_header.strip())
                self.assertGreaterEqual(int(metadata_column), 1)
                self.assertTrue(expected_metadata.strip())

    def test_derived_formulas_match_registry_membership_units_and_inputs(self) -> None:
        registry = load_definitions().by_id()

        self.assertEqual(
            set(FORMULAS), {key for key, value in registry.items() if value.provider == "derived"}
        )
        for series_id, formula in FORMULAS.items():
            with self.subTest(series_id=series_id):
                series = registry[series_id]
                self.assertEqual(formula.unit, series.unit)
                self.assertTrue(set(formula.inputs) <= registry.keys())


def test_catalog_never_opens_a_store(tmp_path, monkeypatch, capsys):
    def unexpected(*args, **kwargs):
        raise AssertionError("catalog opened a store")

    monkeypatch.setattr(sqlite3, "connect", unexpected)
    before = set(tmp_path.rglob("*"))
    monkeypatch.chdir(tmp_path)
    for command in (["list", "--format", "json"], ["search", "CPI"]):
        assert main(command) == 0
    assert set(tmp_path.rglob("*")) == before
    with pytest.raises(SystemExit):
        main(["list", "--db", str(tmp_path / "missing.sqlite")])


@pytest.mark.parametrize(("priority", "expected"), [("0", 0), ("10", 10), ("null", 100)])
def test_registry_preserves_explicit_priority(tmp_path, priority, expected):
    path = tmp_path / "registry.yaml"
    path.write_text(
        _registry_yaml(plausible_min="-2", plausible_max="20") + f"    priority: {priority}\n",
        encoding="utf-8",
    )
    assert load_definitions(path).series[0].priority == expected


@pytest.mark.parametrize("priority", ["true", "false"])
def test_registry_priority_is_not_a_boolean(tmp_path, priority):
    path = tmp_path / "registry.yaml"
    path.write_text(
        _registry_yaml(plausible_min="-2", plausible_max="20") + f"    priority: {priority}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"priority.*integer"):
        load_definitions(path)


@pytest.mark.parametrize(
    ("series_id", "provider_id", "excluded_alias"),
    [
        ("usd_index.broad", "DTWEXBGS", "DXY"),
        ("us.gdp_growth", "A191RL1Q225SBEA", "GDPC1"),
        ("us.pce.core", "PCEPILFE", "FRB目標インフレ"),
        ("jp.real_wage_index", "0302030201010090010", "実質賃金前年比"),
        ("jp.policy_rate", "FM01:STRDCLUCON", "日銀政策金利"),
    ],
)
def test_registry_preserves_series_identity(series_id, provider_id, excluded_alias):
    definition = load_definitions().by_id()[series_id]
    assert definition.reading_key == provider_id
    assert excluded_alias not in definition.aliases


def test_registry_rejects_old_l2_provider_series_id(tmp_path):
    path = tmp_path / "registry.yaml"
    path.write_text(
        _registry_yaml(plausible_min="-2", plausible_max="20").replace(
            "reading_key: TEST", "provider_series_id: TEST"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="reading_key, not provider_series_id"):
        load_definitions(path)


def test_acquisition_catalog_resolves_each_declared_provider():
    specs = registered_specs()
    assert len({spec.name for spec in specs}) == len(specs)
    for spec in specs:
        assert provider_spec(spec.name) == spec
