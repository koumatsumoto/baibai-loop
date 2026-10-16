"""L2 reads current selected inputs and ignores old derived materializations."""

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime

import pytest
from tests.helpers.indicator_store import observation, store_h15_observations

from baibai_engine.market_context.indicator_service import IndicatorsService
from baibai_engine.market_context.indicators.db import initialize_database, insert_observations
from baibai_engine.market_context.indicators.definitions import load_definitions
from baibai_engine.market_context.reading.reader import build_store_observation_reader
from baibai_engine.read_api.freshness import indicator_latest_observed_at


def test_read_only_derived_values_follow_source_corrections_not_old_cache(tmp_path):
    path = tmp_path / "indicators.sqlite"
    definitions = load_definitions()
    registry = definitions.by_id()
    day = date(2026, 1, 30)
    vintage = datetime(2026, 2, 1, tzinfo=UTC)
    with closing(initialize_database(path)) as writer:
        insert_observations(
            writer,
            [
                observation(registry["jp.10y"], day, 1.0, vintage),
                observation(
                    registry["rate_diff.us_jp_10y"],
                    date(2026, 2, 15),
                    9.0,
                    datetime(2026, 2, 16, tzinfo=UTC),
                ),
            ],
        )
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
    assert indicator_latest_observed_at(path) == day
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
    with pytest.raises(ValueError, match="no stored observation to retract"):
        IndicatorsService(path).retract("rate_diff.us_jp_10y", [(day, vintage)])


def test_option_reader_reuses_day_calculation_and_replaces_changed_native_vintage(
    tmp_path, monkeypatch
):
    from tests.engine.market_context.indicators.test_option_iv import ASOF, _chain_records

    from baibai_engine.market_context.indicators.source_snapshots import store_snapshot
    from baibai_engine.market_context.reading import options

    path = tmp_path / "indicators.sqlite"
    definitions = load_definitions()
    first = datetime(2026, 7, 30, tzinfo=UTC)
    corrected = datetime(2026, 7, 31, tzinfo=UTC)
    with closing(initialize_database(path)) as writer, writer:
        store_snapshot(
            writer,
            source_key="jquants_options:n225",
            observed_at=ASOF,
            fetched_at=first,
            records=_chain_records(),
        )
        insert_observations(writer, [observation("jp.n225_iv_30d", date(2026, 8, 20), 25.0, first)])
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
        assert indicator_latest_observed_at(path) == ASOF  # not the old scalar's later date
        with closing(sqlite3.connect(path)) as writer, writer:
            store_snapshot(
                writer,
                source_key="jquants_options:n225",
                observed_at=ASOF,
                fetched_at=corrected,
                records=[],
            )
        assert read("jp.n225_iv_30d", ASOF, ASOF) == ()
        assert calls == [ASOF, ASOF]
    assert indicator_latest_observed_at(path) is None
    with pytest.raises(ValueError, match="no stored observation to retract"):
        IndicatorsService(path).retract("jp.n225_iv_30d", [(ASOF, first)])
