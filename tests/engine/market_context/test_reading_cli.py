from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from tests.helpers.indicator_store import observations, series_definition, store_reader

from baibai_engine.market_context.indicators import db
from baibai_engine.market_context.indicators.observations import ObservationRecord
from baibai_engine.market_context.reading import cli
from baibai_engine.market_context.reading.compute import compute_reading
from baibai_engine.market_context.reading.models import ReadingSnapshot
from baibai_engine.market_context.reading.rules import (
    DEFAULT_RULES_PATH,
    ReadingRules,
    SeriesOverride,
    load_reading_rules,
)


def test_table_labels_each_trend_with_its_actual_window(capsys: pytest.CaptureFixture[str]) -> None:
    definition = series_definition(frequency="daily")
    published = load_reading_rules(DEFAULT_RULES_PATH)
    rules = ReadingRules(
        schema_version=published.schema_version,
        defaults=published.defaults,
        overrides={
            definition.series_id: SeriesOverride(short_trend_months=1, long_trend_months=6)
        },
    )
    points = observations(
        definition,
        [
            (date(2026, 1, 15), 10.0),
            (date(2026, 6, 15), 15.0),
            (date(2026, 7, 15), 20.0),
        ],
    )
    snapshot = compute_reading(
        series=[definition],
        reader=store_reader(points),
        rules=rules,
        rules_revision="test",
        asof=date(2026, 7, 15),
    )
    cli._print_table(snapshot)
    lines = capsys.readouterr().out.splitlines()
    header = lines[1].split("\t")
    row = dict(zip(header, lines[2].split("\t"), strict=True))
    assert row["short_trend"] == "1m:up(+5)"
    assert row["long_trend"] == "6m:up(+10)"
    assert "3m" not in header
    assert "12m" not in header


def test_cli_reads_one_snapshot_during_concurrent_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "indicators.sqlite"
    with closing(db.initialize_database(path)) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        db.insert_observations(connection, [_yield_observation(1.0, 1)])
        connection.commit()

    read_snapshot = cli.read_store_snapshot

    def read_while_writer_commits(
        connection: sqlite3.Connection, *, asof: date, rules_path: Path
    ) -> ReadingSnapshot:
        # The first SELECT pins the reader's snapshot before a separate writer commits.
        assert connection.in_transaction
        before = connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        with closing(db.open_connection(path)) as writer:
            db.insert_observations(writer, [_yield_observation(2.0, 2)])
            writer.commit()
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == before
        return read_snapshot(connection, asof=asof, rules_path=rules_path)

    monkeypatch.setattr(cli, "read_store_snapshot", read_while_writer_commits)
    assert cli.main(["--asof", "2026-07-15", "--db", str(path), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    row = next(item for item in payload["series"] if item["series_id"] == "us.10y")
    assert row["latest_value"] == 1.0
    with closing(db.open_read_only_connection(path)) as connection:
        latest = db.latest_observation(connection, "us.10y")
        assert latest is not None
        assert latest.value == 2.0


def _yield_observation(value: float, second: int) -> ObservationRecord:
    return ObservationRecord(
        series_id="us.10y",
        observed_at=date(2026, 7, 15),
        value=value,
        unit="percent",
        vintage_at=datetime(2026, 7, 15, 12, 0, second, tzinfo=UTC),
        source_url="https://example.com/yield",
    )
