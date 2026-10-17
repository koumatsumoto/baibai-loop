from __future__ import annotations

from dataclasses import fields

import pytest

from baibai_engine.collection.economic_sources import SourceDefinition, load_sources


def test_source_catalog_has_no_derived_calculations_or_l2_metadata():
    sources = load_sources()
    assert sources
    assert all(source.provider != "derived" for source in sources.values())
    assert not {"unit", "plausible_min", "plausible_max", "category", "series_id"} & {
        field.name for field in fields(SourceDefinition)
    }


def test_shared_packages_do_not_choose_an_l2_output_column():
    sources = load_sources()
    for source_id in ("fed-h15", "ecb-fx", "mof-jgbcm-all", "jquants-options-n225"):
        assert sources[source_id].provider_series_id == ""


def test_derived_source_is_rejected(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text(
        "sources:\n  local:\n    provider: derived\n    source_url: local\n    frequency: daily\n"
    )
    with pytest.raises(ValueError, match="not an external collection source"):
        load_sources(path)


def test_mof_acquisition_preserves_table_without_a_requested_l2_column():
    from datetime import date

    from baibai_engine.collection.economic_sources import require_source
    from baibai_engine.market_context.indicators.definitions import load_definitions
    from baibai_engine.market_context.indicators.providers.mof_jgb import parse_mof_jgb_csv
    from baibai_engine.market_context.reading.native import _one_value

    day = date(2026, 5, 1)
    source = require_source("mof-jgbcm-all")
    records = parse_mof_jgb_csv(source, "基準日,2年,別項目\nR8.5.1,0.8,未定\n", start=day, end=day)
    assert records[day][0]["row"] == {"基準日": "R8.5.1", "2年": "0.8", "別項目": "未定"}
    series = next(
        item
        for item in load_definitions().series
        if item.provider == "mof_jgb" and item.provider_series_id == "10年"
    )
    with pytest.raises(ValueError, match="MOF source missing column 10年"):
        _one_value(series, records[day])


def test_umich_acquisition_preserves_other_columns_before_l2_selection():
    from datetime import date

    from baibai_engine.collection.economic_sources import load_sources
    from baibai_engine.market_context.indicators.definitions import load_definitions
    from baibai_engine.market_context.indicators.providers.umich_sca import parse_umich_table
    from baibai_engine.market_context.reading.native import _one_value

    day = date(2026, 5, 1)
    source = next(item for item in load_sources().values() if item.provider == "umich_sca")
    records = parse_umich_table(source, "Month,YYYY,OTHER\nMay,2026,\n", start=day, end=day)
    assert records[day][0] == {"Month": "May", "YYYY": "2026", "OTHER": ""}
    series = next(item for item in load_definitions().series if item.provider == "umich_sca")
    with pytest.raises(ValueError, match="UMich table missing column"):
        _one_value(series, records[day])


def test_projection_failure_does_not_relabel_or_refetch_successful_acquisition(
    tmp_path, monkeypatch
):
    from contextlib import closing
    from datetime import date

    from baibai_engine.market_context import indicator_service
    from baibai_engine.market_context.indicators import db
    from baibai_engine.market_context.indicators.definitions import load_definitions
    from baibai_engine.market_context.indicators.read_contracts import native_source_key
    from baibai_engine.market_context.indicators.source_snapshots import read_snapshots

    day = date(2026, 5, 1)
    path = tmp_path / "indicators.sqlite"
    series = load_definitions().by_id()["jp.10y"]
    calls = []
    rows = [{"row": {"基準日": "R8.5.1", "2年": "0.8"}, "source_url": series.source_url}]

    def fetch(source, **kwargs):
        calls.append(source.source_id)
        return {day: rows}

    monkeypatch.setattr(indicator_service, "fetch_native_records", fetch)
    service = indicator_service.IndicatorsService(path)
    for refresh in (True, False):
        with pytest.raises(ValueError, match="MOF source missing column 10年"):
            service.get_range(series.series_id, start=day, end=day, refresh=refresh)
    assert calls == [series.source_id]
    with closing(db.open_read_only_connection(path)) as connection:
        snapshots = list(read_snapshots(connection, day, day, source_key=native_source_key(series)))
        assert snapshots[0].records == tuple(rows)
        assert [
            tuple(row)
            for row in connection.execute("SELECT status, record_count FROM provider_runs")
        ] == [("ok", 1)]


def test_l2_resolves_shared_source_metadata_from_one_catalog(monkeypatch):
    from dataclasses import replace

    from baibai_engine.market_context.indicators import definitions

    sources = load_sources()
    sources["fed-h15"] = replace(sources["fed-h15"], source_url="https://example.com/new-h15.csv")
    monkeypatch.setattr(definitions, "load_sources", lambda: sources)
    readings = [
        item for item in definitions.load_definitions().series if item.source_id == "fed-h15"
    ]
    assert len(readings) > 1
    assert all(item.source_url == "https://example.com/new-h15.csv" for item in readings)
    assert len({item.provider_series_id for item in readings}) == len(readings)


def test_l2_registry_rejects_inline_external_source_configuration(tmp_path):
    from baibai_engine.market_context.indicators.definitions import load_definitions

    path = tmp_path / "reading.yaml"
    path.write_text("series: [{source_id: fed-h15, provider: frb_h15}]")
    with pytest.raises(ValueError, match="belong in the Collection source catalog"):
        load_definitions(path)


def test_l2_registry_rejects_an_unknown_source_binding(tmp_path):
    from baibai_engine.market_context.indicators.definitions import load_definitions

    path = tmp_path / "reading.yaml"
    path.write_text("series: [{source_id: unknown-source}]")
    with pytest.raises(ValueError, match="unknown economic collection source"):
        load_definitions(path)
