from __future__ import annotations

from dataclasses import fields
from importlib.resources import files

import pytest

from baibai_engine.data_collection.catalog import SourceDefinition, load_sources


def test_relocated_economic_resources_resolve_from_their_owner_packages():
    from baibai_engine.data_collection.catalog import SOURCE_CATALOG
    from baibai_engine.data_collection.providers.spglobal.pmi import MANIFEST_PATH
    from baibai_engine.data_collection.receipts.native_sources.store import SCHEMA_PATH

    assert (
        SOURCE_CATALOG.read_bytes()
        == files("baibai_engine.data_collection").joinpath("catalog.yaml").read_bytes()
    )
    assert (
        SCHEMA_PATH.read_bytes()
        == files("baibai_engine.data_collection.receipts.native_sources")
        .joinpath("store.sql")
        .read_bytes()
    )
    assert (
        MANIFEST_PATH.read_bytes()
        == files("baibai_engine.data_collection.providers.spglobal")
        .joinpath("pmi_release_urls.yaml")
        .read_bytes()
    )


def test_collection_list_and_help_do_not_load_env_or_create_a_store(tmp_path, monkeypatch, capsys):
    import json
    import sqlite3

    import baibai_jobs.acquisition.economic_cli as economic_cli
    from baibai_jobs import cli

    def unexpected(*args, **kwargs):
        raise AssertionError("read-only Collection command opened a store or loaded env")

    monkeypatch.setattr(sqlite3, "connect", unexpected)
    monkeypatch.setattr(economic_cli, "load_project_env", unexpected)
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.rglob("*"))
    assert cli.main(["acquire-economic", "list", "--format", "json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert "fed-h15" in {item["source_id"] for item in listed}
    with pytest.raises(SystemExit) as help_exit:
        cli.main(["acquire-economic", "--help"])
    assert help_exit.value.code == 0
    assert "refresh" in capsys.readouterr().out
    assert set(tmp_path.rglob("*")) == before


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

    from baibai_engine.data_collection.catalog import require_source
    from baibai_engine.data_collection.providers.mof.jgb import parse_mof_jgb_csv
    from baibai_engine.machine_analysis.observations.economic_indicators.definitions import (
        load_definitions,
    )
    from baibai_engine.machine_analysis.observations.economic_indicators.native import _one_value

    day = date(2026, 5, 1)
    source = require_source("mof-jgbcm-all")
    records = parse_mof_jgb_csv(source, "基準日,2年,別項目\nR8.5.1,0.8,未定\n", start=day, end=day)
    assert records[day][0]["row"] == {"基準日": "R8.5.1", "2年": "0.8", "別項目": "未定"}
    series = next(
        item
        for item in load_definitions().series
        if item.provider == "mof_jgb" and item.reading_key == "10年"
    )
    with pytest.raises(ValueError, match="MOF source missing column 10年"):
        _one_value(series, records[day])


def test_umich_acquisition_preserves_other_columns_before_l2_selection():
    from datetime import date

    from baibai_engine.data_collection.catalog import load_sources
    from baibai_engine.data_collection.providers.university_of_michigan.sentiment import (
        parse_umich_table,
    )
    from baibai_engine.machine_analysis.observations.economic_indicators.definitions import (
        load_definitions,
    )
    from baibai_engine.machine_analysis.observations.economic_indicators.native import _one_value

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

    import baibai_engine.data_collection.acquisition.refresh as economic_acquisition
    import baibai_engine.machine_analysis.observations.economic_indicators.service as indicator_service
    from baibai_engine.data_collection.receipts.native_sources import store as economic_store
    from baibai_engine.data_collection.receipts.native_sources.records import read_snapshots
    from baibai_engine.machine_analysis.observations.economic_indicators.definitions import (
        load_definitions,
    )

    day = date(2026, 5, 1)
    path = tmp_path / "indicators.sqlite"
    series = load_definitions().by_id()["jp.10y"]
    calls = []
    rows = [{"row": {"基準日": "R8.5.1", "2年": "0.8"}, "source_url": series.source_url}]

    def fetch(source, **kwargs):
        calls.append(source.source_id)
        return {day: rows}

    monkeypatch.setattr(economic_acquisition, "fetch_native_records", fetch)
    outcomes = economic_acquisition.refresh_sources(
        [series.source_id], db_path=path, start=day, end=day
    )
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], economic_acquisition.SourceRefreshSuccess)
    assert outcomes[0].record_count == 1
    service = indicator_service.IndicatorsService(path)
    for _ in range(2):
        with pytest.raises(ValueError, match="MOF source missing column 10年"):
            service.get_range(series.series_id, start=day, end=day)
    assert calls == [series.source_id]
    with closing(economic_store.open_read_only_connection(path)) as connection:
        snapshots = list(read_snapshots(connection, day, day, source_id=series.source_id))
        assert snapshots[0].records == tuple(rows)
        assert [
            tuple(row)
            for row in connection.execute("SELECT status, record_count FROM economic_source_runs")
        ] == [("ok", 1)]


def test_l2_resolves_shared_source_metadata_from_one_catalog(monkeypatch):
    from dataclasses import replace

    from baibai_engine.machine_analysis.observations.economic_indicators import definitions

    sources = load_sources()
    sources["fed-h15"] = replace(sources["fed-h15"], source_url="https://example.com/new-h15.csv")
    monkeypatch.setattr(definitions, "load_sources", lambda: sources)
    readings = [
        item for item in definitions.load_definitions().series if item.source_id == "fed-h15"
    ]
    assert len(readings) > 1
    assert all(item.source_url == "https://example.com/new-h15.csv" for item in readings)
    assert sources["fed-h15"].provider_series_id == ""
    assert len({item.reading_key for item in readings}) == len(readings)


def test_l2_registry_rejects_inline_external_source_configuration(tmp_path):
    from baibai_engine.machine_analysis.observations.economic_indicators.definitions import (
        load_definitions,
    )

    path = tmp_path / "reading.yaml"
    path.write_text("series: [{source_id: fed-h15, provider: frb_h15}]")
    with pytest.raises(ValueError, match="belong in the Collection source catalog"):
        load_definitions(path)


def test_l2_registry_rejects_an_unknown_source_binding(tmp_path):
    from baibai_engine.machine_analysis.observations.economic_indicators.definitions import (
        load_definitions,
    )

    path = tmp_path / "reading.yaml"
    path.write_text("series: [{source_id: unknown-source}]")
    with pytest.raises(ValueError, match="unknown economic collection source"):
        load_definitions(path)
