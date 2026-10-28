"""L2 reads current selected inputs without persisting derived values."""

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime

from tests.helpers.indicator_store import (
    observation,
    store_h15_observations,
    store_mof_observations,
)

from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_engine.machine_analysis.observations.economic_indicators.definitions import load_definitions
from baibai_engine.machine_analysis.observations.economic_indicators.reader import (
    build_store_observation_reader,
)


def test_read_only_derived_values_follow_source_corrections(tmp_path):
    path = tmp_path / "indicators.sqlite"
    definitions = load_definitions()
    registry = definitions.by_id()
    day = date(2026, 1, 30)
    vintage = datetime(2026, 2, 1, tzinfo=UTC)
    with closing(initialize_database(path)) as writer:
        store_mof_observations(writer, [observation(registry["jp.10y"], day, 1.0, vintage)])
        store_h15_observations(writer, [observation(registry["us.10y"], day, 4.0, vintage)])
        writer.commit()
    before = path.read_bytes()
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as reader:
        reader.row_factory = sqlite3.Row
        reader.execute("PRAGMA query_only=ON")
        read = build_store_observation_reader(reader, series=definitions.series)
        first = read("rate_diff.us_jp_10y", date(2026, 1, 1), date(2026, 2, 28))
        assert [(row.observed_at, row.value) for row in first] == [(day, 3.0)]
        assert first[0].vintage_at == vintage
    assert path.read_bytes() == before
    corrected_at = datetime(2026, 2, 2, tzinfo=UTC)
    with closing(sqlite3.connect(path)) as writer:
        writer.row_factory = sqlite3.Row
        store_h15_observations(writer, [observation(registry["us.10y"], day, 3.5, corrected_at)])
        writer.commit()
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as reader:
        reader.row_factory = sqlite3.Row
        read = build_store_observation_reader(reader, series=definitions.series)
        corrected = read("rate_diff.us_jp_10y", day, day)
        assert corrected[0].value == 2.5
        assert corrected[0].vintage_at == corrected_at


def test_option_reader_reuses_day_calculation_and_replaces_changed_native_vintage(
    tmp_path, monkeypatch
):
    from tests.engine.market_context.indicators.test_option_iv import ASOF, _chain_records

    from baibai_engine.data_collection.receipts.native_sources.records import store_snapshot
    import baibai_engine.machine_analysis.observations.economic_indicators.options as options

    path = tmp_path / "indicators.sqlite"
    definitions = load_definitions()
    first = datetime(2026, 7, 30, tzinfo=UTC)
    corrected = datetime(2026, 7, 31, tzinfo=UTC)
    with closing(initialize_database(path)) as writer, writer:
        store_snapshot(
            writer,
            source_id="jquants-options-n225",
            observed_at=ASOF,
            fetched_at=first,
            records=_chain_records(),
        )
    before = path.read_bytes()
    calculate = options.option_iv_readings
    calls = []

    def counted(quotes, day):
        calls.append(day)
        return calculate(quotes, day)

    monkeypatch.setattr(options, "option_iv_readings", counted)
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        read = build_store_observation_reader(connection, series=definitions.series)
        for series_id in ("jp.n225_iv_30d", "jp.n225_iv_skew", "jp.n225_iv_term"):
            assert read(series_id, ASOF, ASOF)
        assert calls == [ASOF]
        assert path.read_bytes() == before
        with closing(sqlite3.connect(path)) as writer, writer:
            store_snapshot(
                writer,
                source_id="jquants-options-n225",
                observed_at=ASOF,
                fetched_at=corrected,
                records=[],
            )
        assert read("jp.n225_iv_30d", ASOF, ASOF) == ()
        assert calls == [ASOF, ASOF]
