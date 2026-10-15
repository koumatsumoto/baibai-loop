"""L2 reads current selected inputs and ignores old derived materializations."""

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime

import pytest
from tests.helpers.indicator_store import observation

from baibai_engine.market_context.indicators.db import initialize_database, insert_observations
from baibai_engine.market_context.indicators.definitions import load_definitions
from baibai_engine.market_context.indicator_service import IndicatorsService
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
                observation(registry["us.10y"], day, 4.0, vintage),
                observation(registry["jp.10y"], day, 1.0, vintage),
                observation(
                    registry["rate_diff.us_jp_10y"],
                    date(2026, 2, 15),
                    9.0,
                    datetime(2026, 2, 16, tzinfo=UTC),
                ),
            ],
        )
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
        insert_observations(writer, [observation(registry["us.10y"], day, 3.5, corrected_at)])
        writer.commit()
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as reader:
        reader.row_factory = sqlite3.Row
        read = build_store_observation_reader(reader, series=definitions.series)
        corrected = read("rate_diff.us_jp_10y", day, day)
        assert corrected[0].value == 2.5
        assert corrected[0].vintage_at == corrected_at
    with pytest.raises(ValueError, match="no stored observation to retract"):
        IndicatorsService(path).retract("rate_diff.us_jp_10y", [(day, vintage)])
