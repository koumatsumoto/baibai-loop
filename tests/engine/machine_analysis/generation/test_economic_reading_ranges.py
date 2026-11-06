"""Scope reduction uses the calculation owners, not an independent window policy."""

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
import yaml
from tests.helpers.r2_store import MemoryR2Store

import baibai_engine.machine_analysis.generation.economic_inputs as fixed
import baibai_engine.machine_analysis.observations.economic_indicators.flows as flows
import baibai_engine.machine_analysis.observations.economic_indicators.reader as reader
from baibai_engine.data_collection.receipts.native_sources.confirmations import (
    confirm_source_response,
)
from baibai_engine.data_collection.receipts.native_sources.records import store_snapshot
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.runners.market_reading import (
    reading_observation_ranges,
    reading_snapshot,
)
from baibai_engine.machine_analysis.generation.runners.reading_rules import (
    DEFAULT_RULES_PATH,
    load_reading_rules,
)
from baibai_engine.machine_analysis.observations.economic_indicators.definitions import (
    load_definitions,
)
from baibai_engine.machine_analysis.observations.economic_indicators.formulas import (
    FORMULAS,
    DerivedFormula,
)
from baibai_engine.primitives.json import canonical_json

DAY = date(2026, 1, 5)
AT = datetime(2026, 1, 5, 6, tzinfo=UTC)


def selection(store, saved):
    return json.loads(
        store.values[fixed._PREFIX + saved.descriptor["selection_sha256"] + ".json"].body
    )


def test_shared_source_union_and_nested_monthly_requests_match_reader(monkeypatch):
    definitions = load_definitions().by_id()
    nested = replace(definitions["us.erp"], series_id="nested", reading_key="nested")
    monkeypatch.setitem(
        FORMULAS,
        "nested",
        DerivedFormula(
            inputs=("us.erp", "us.10y_2y_spread"),
            unit=nested.unit,
            compute=lambda values: 0.0,
        ),
    )
    series = (*definitions.values(), nested)
    requested = {
        "nested": (date(2024, 1, 17), date(2024, 2, 20)),
        "us.2y": (date(2023, 1, 1), date(2023, 1, 31)),
    }
    planned = reader.source_record_ranges(series=series, requested=requested)
    actual = {}

    def native(connection, definition, *, start, end, vintage_cutoff=None):
        previous = actual.get(definition.source_id, (start, end))
        actual[definition.source_id] = (min(previous[0], start), max(previous[1], end))
        return ()

    monkeypatch.setattr(reader, "read_native_series", native)
    with closing(sqlite3.connect(":memory:")) as connection:
        read = reader.build_store_observation_reader(connection, series=series)
        for series_id, bounds in requested.items():
            assert read(series_id, *bounds) == ()
    assert planned == actual
    assert planned[definitions["us.sp500_earnings_yield"].source_id][0] == date(2024, 1, 1)
    assert planned[definitions["us.10y"].source_id] == (date(2023, 1, 1), date(2024, 2, 20))


def test_flow_plan_matches_publication_read_not_aggregation_window(monkeypatch):
    series = load_definitions().series
    flow = next(item for item in series if item.provider == "jquants_flows")
    bounds = (date(2024, 1, 15), date(2024, 1, 31))
    cutoff = date(2024, 2, 10)
    actual = []

    def snapshots(connection, start, end, *, source_id):
        actual.append((source_id, start, end))
        return ()

    monkeypatch.setattr(flows, "read_snapshots", snapshots)
    with closing(sqlite3.connect(":memory:")) as connection:
        read = reader.build_store_observation_reader(
            connection, series=series, vintage_cutoff=cutoff
        )
        assert read(flow.series_id, *bounds) == ()
    planned = reader.source_record_ranges(
        series=series, requested={flow.series_id: bounds}, vintage_cutoff=cutoff
    )
    assert planned == {flow.source_id: (date.min, cutoff)}
    assert actual == [(flow.source_id, *planned[flow.source_id])]


def test_native_scope_skips_old_and_future_bodies_and_reduces_projection(tmp_path, monkeypatch):
    source = tmp_path / "native.sqlite"
    source_id = "cftc-cot-jpy"
    with closing(initialize_database(source)) as c, c:
        store_snapshot(
            c,
            source_id=source_id,
            observed_at=date(2026, 1, 1),
            fetched_at=AT,
            records=[{"noncomm_positions_long_all": "3", "noncomm_positions_short_all": "5"}],
        )
        # Invalid and large outside-window bytes must never be decoded or copied.
        for day in ("1990-01-01", "2030-01-01"):
            c.execute(
                "INSERT INTO economic_source_records VALUES (?,?,?,?,?)",
                (source_id, day, AT.isoformat(), 1, b"not-gzip" * 150_000),
            )
    expected = reading_snapshot(source, asof=DAY)
    before = source.read_bytes()
    store = MemoryR2Store()
    raw_rows = fixed.read_snapshot_rows
    retained = []

    def selected_rows(*args, **kwargs):
        for row in raw_rows(*args, **kwargs):
            retained.append(row[:3])
            yield row

    original_row = fixed._row
    exact = []

    def row(connection, kind, key):
        exact.append((kind, key))
        return original_row(connection, kind, key)

    monkeypatch.setattr(fixed, "read_snapshot_rows", selected_rows)
    monkeypatch.setattr(fixed, "_row", row)
    saved = fixed.retain_economic_input(store, source, as_of=DAY, evaluation_time=AT)
    assert saved.descriptor["format"] == "economic-input-v2"
    assert [item[1] for item in retained] == ["2026-01-01"]
    assert not exact  # Already-selected raw rows are not fetched a second time.
    root = selection(store, saved)
    assert root["format"] == "economic-selection-v2"
    assert root["record_ranges"][source_id][1] == DAY.isoformat()
    assert set(root["months"]) == {source_id + "/2026-01"}
    sizes = []

    def read_projection(path, **kwargs):
        sizes.append(path.stat().st_size)
        return reading_snapshot(path, **kwargs)

    monkeypatch.setattr(fixed, "reading_snapshot", read_projection)
    assert fixed.rebuild_economic_input(store, source, saved).context["reading"] == expected
    assert exact == [("records", retained[0])]
    assert sizes[0] < len(before) // 10
    assert source.read_bytes() == before


def test_custom_rule_window_and_yoy_lookback_are_saved_from_same_owner(tmp_path):
    source = tmp_path / "native.sqlite"
    with closing(initialize_database(source)):
        pass
    raw = yaml.safe_load(DEFAULT_RULES_PATH.read_text())
    raw["overrides"]["us.nonfarm_payrolls"] = {"percentile_window_years": 2, "statistic": "yoy"}
    rules_path = tmp_path / "custom-rules.yaml"
    rules_path.write_text(yaml.safe_dump(raw))
    definitions = load_definitions().series
    rules = load_reading_rules(rules_path)
    requested = reading_observation_ranges(series=definitions, rules=rules, asof=DAY)
    plan = reader.source_record_ranges(series=definitions, requested=requested)
    assert requested["us.nonfarm_payrolls"][0] < date(2024, 1, 5)
    store = MemoryR2Store()
    saved = fixed.retain_economic_input(
        store, source, as_of=DAY, evaluation_time=AT, rules_path=rules_path
    )
    actual = selection(store, saved)["record_ranges"]
    assert actual == {
        key: [start.isoformat(), end.isoformat()] for key, (start, end) in plan.items()
    }
    assert fixed.rebuild_economic_input(store, source, saved, rules_path=rules_path).context[
        "reading"
    ] == reading_snapshot(
        source,
        asof=DAY,
        rules_path=rules_path,
    )


def test_fred_subset_keeps_global_provenance_without_claiming_complete_projection(
    tmp_path, monkeypatch
):
    source = tmp_path / "native.sqlite"
    with closing(initialize_database(source)) as c, c:
        confirm_source_response(
            c,
            source_id="fred-payems",
            fetched_at=AT,
            records_by_day={
                date(1990, 1, 1): [{"PAYEMS": "100000"}],
                date(2026, 1, 1): [{"PAYEMS": "120000"}],
            },
        )
    store = MemoryR2Store()
    saved = fixed.retain_economic_input(store, source, as_of=DAY, evaluation_time=AT)
    root = selection(store, saved)
    assert root["confirmations"]["fred-payems"] is not None
    assert set(root["months"]) == {"fred-payems/2026-01"}

    def inspect_projection(path, **kwargs):
        with closing(sqlite3.connect(path)) as c:
            assert (
                c.execute("SELECT COUNT(*) FROM economic_source_record_confirmations").fetchone()[0]
                == 1
            )
            assert (
                c.execute(
                    "SELECT COUNT(*) FROM economic_source_collection_confirmations"
                ).fetchone()[0]
                == 0
            )
        return reading_snapshot(path, **kwargs)

    monkeypatch.setattr(fixed, "reading_snapshot", inspect_projection)
    assert fixed.rebuild_economic_input(store, source, saved).context[
        "reading"
    ] == reading_snapshot(source, asof=DAY)


def test_v1_selection_is_not_reinterpreted_as_narrowed_v2(tmp_path):
    descriptor = {"format": "economic-input-v1", "selection_sha256": "a" * 64}
    saved = InputSnapshot(
        kind="native_l1",
        reference="old-selection",
        sha256=hashlib.sha256(canonical_json(descriptor).encode()).hexdigest(),
        descriptor=descriptor,
    )
    store = MemoryR2Store()
    with pytest.raises(ValueError, match="saved economic fixed selection is required"):
        fixed.rebuild_economic_input(store, tmp_path / "not-opened.sqlite", saved)
    assert not store.get_keys
