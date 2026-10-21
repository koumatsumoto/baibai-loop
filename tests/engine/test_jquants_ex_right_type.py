"""Preserve price-adjustment event kinds instead of guessing share-unit changes."""

import sqlite3
from contextlib import closing
from datetime import date

import pyarrow.parquet as pq
import pytest

from baibai_engine.market.jquants_decode import (
    JQuantsProviderError,
    normalize_adjustment_factor_event,
    normalize_daily_bar,
)
from baibai_engine.market.store import read_adjustment_factor_bars, read_daily_bars
from baibai_engine.market_context.prices import close_change_since
from baibai_engine.screening.calibration.forward import _load_ticker_adjustment_events
from baibai_engine.storage.lake.datasets import JQUANTS_DAILY_BARS
from baibai_engine.storage.lake.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.observations.jquants import store_jquants_daily_bars


@pytest.mark.parametrize(("kind", "factor"), [("1", 0.5), ("2", 2.0), ("3", 0.8), ("3", 1.0)])
@pytest.mark.parametrize("close", [None, 100.0])
def test_ex_right_type_survives_source_and_sqlite_roundtrip(tmp_path, kind, factor, close):
    day = date(2026, 10, 14)
    raw = {"Code": "130A0", "Date": day.isoformat(), "C": close, "AdjFactor": factor, "ExRT": kind}
    event = normalize_adjustment_factor_event(raw)
    assert event is not None
    assert event.ex_right_type == kind
    bar = normalize_daily_bar(raw)
    if close is None:
        assert bar is None
    else:
        assert bar is not None
        assert bar.ex_right_type == kind
    start = date(2026, 10, 13)
    prior = {"Code": "130A0", "Date": start.isoformat(), "C": 110.0, "AdjFactor": 1.0}
    prior_bar = normalize_daily_bar(prior)
    assert prior_bar is not None
    database = tmp_path / "market.sqlite"
    store_jquants_daily_bars(database, [prior, raw], requested_start=start, requested_end=day)
    assert read_adjustment_factor_bars(database, start, day) == [event]
    assert read_daily_bars(database, start, day) == [prior_bar, *([] if bar is None else [bar])]
    assert JQUANTS_DAILY_BARS.contract_version == 2
    assert "ex_right_type" in JQUANTS_DAILY_BARS.arrow_schema.names


@pytest.mark.parametrize(
    ("raw_value", "expected"), [("54775.0", 54775.0), ("", None), (None, None)]
)
def test_provider_market_cap_survives_sqlite_and_lake_without_price(tmp_path, raw_value, expected):
    day = date(2026, 10, 14)
    database = tmp_path / "market.sqlite"
    raw = {
        "Date": day.isoformat(),
        "Code": "13010",
        "O": None,
        "H": None,
        "L": None,
        "C": None,
        "UL": "0",
        "LL": "0",
        "Vo": None,
        "Va": None,
        "AdjFactor": 1.0,
        "MktCap": raw_value,
        "ExRT": None,
    }
    store_jquants_daily_bars(database, [raw], requested_start=day, requested_end=day)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT close, market_cap_million_yen FROM jquants_daily_bars"
        ).fetchone() == (None, expected)
    mirror = tmp_path / "lake"
    with sealed_sqlite_snapshot(sqlite_path=database, mirror_root=mirror) as snapshot:
        report = export_dataset_snapshot(
            dataset_name="jquants.daily_bars",
            mirror_root=mirror,
            source_snapshot=snapshot,
            producer_git_commit="a" * 40,
            build_id="market-cap-source",
        )
    obj = report.manifest.partitions[0].objects[0]
    rows = pq.ParquetFile(mirror / obj.key).read().to_pylist()
    assert len(rows) == 1
    assert rows[0]["market_cap_million_yen"] == expected
    assert rows[0]["close"] is None


@pytest.mark.parametrize("factor", [0.0, -1.0])
@pytest.mark.parametrize("close", [None, 100.0])
def test_invalid_factor_is_retained_but_not_hidden_from_calculation(tmp_path, factor, close):
    start, day = date(2026, 10, 13), date(2026, 10, 14)
    prior = {"Code": "13010", "Date": start.isoformat(), "C": 110.0, "AdjFactor": 1.0}
    raw = {"Code": "13010", "Date": day.isoformat(), "C": close, "AdjFactor": factor}
    with pytest.raises(ValueError, match="positive"):
        normalize_adjustment_factor_event(raw)
    database = tmp_path / "market.sqlite"
    store_jquants_daily_bars(database, [prior, raw], requested_start=start, requested_end=day)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT adjustment_factor FROM jquants_daily_bars WHERE traded_at = ?",
            (day.isoformat(),),
        ).fetchone() == (factor,)
        with pytest.raises(ValueError, match="positive"):
            _load_ticker_adjustment_events(connection, "1301", start=start)
    with pytest.raises(JQuantsProviderError, match="positive"):
        read_adjustment_factor_bars(database, start, day)
    with pytest.raises(ValueError, match="positive"):
        close_change_since(database, ["1301"], since=start)
