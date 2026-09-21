from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest
from tools.migrations.rebuild_edinet_metrics_v27 import rebuild_edinet_metrics_v27

from baibai_engine.market.sqlite import open_connection
from baibai_engine.screening.edinet_revision import compute_extractor_revision


def _source(path: Path) -> Path:
    connection = open_connection(path)
    try:
        for index, asof in enumerate(("2026-05-08", "2026-05-15"), start=1):
            connection.execute(
                "INSERT INTO edinet_metrics(asof_date,ticker,document_type,extractor_revision) "
                "VALUES (?, ?, '120', 'old')",
                (asof, f"130{index}"),
            )
            connection.execute(
                "INSERT INTO source_coverage(source,coverage_key,coverage_start,coverage_end,"
                "fetched_at_utc,record_count,status) VALUES "
                "('edinet_metrics',?,?,?,?,1,'ok')",
                (asof, asof, asof, "2026-09-21T00:00:00+00:00"),
            )
        connection.commit()
    finally:
        connection.close()
    return path


def test_rebuild_uses_each_partition_and_publishes_only_complete_copy(tmp_path: Path) -> None:
    source = _source(tmp_path / "source.sqlite")
    destination = tmp_path / "rebuilt.sqlite"
    calls: list[date] = []

    def runner(asof: date, path: Path) -> int:
        calls.append(asof)
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "UPDATE edinet_metrics SET extractor_revision = ? WHERE asof_date = ?",
                (compute_extractor_revision(), asof.isoformat()),
            )
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM edinet_metrics WHERE asof_date = ?",
                    (asof.isoformat(),),
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT INTO source_coverage(source,coverage_key,coverage_start,coverage_end,"
                "fetched_at_utc,record_count,status) VALUES "
                "('edinet_metrics',?,?,?,?,?,'ok')",
                (
                    asof.isoformat(),
                    asof.isoformat(),
                    asof.isoformat(),
                    "2026-09-21T01:00:00+00:00",
                    count,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        return 0

    assert rebuild_edinet_metrics_v27(source, destination, run_partition=runner) == (2, 2)
    assert calls == [date(2026, 5, 8), date(2026, 5, 15)]
    assert source.read_bytes() != destination.read_bytes()
    with sqlite3.connect(destination) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(DISTINCT extractor_revision) FROM edinet_metrics"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM source_coverage WHERE source='edinet_metrics'"
            ).fetchone()[0]
            == 2
        )


def test_rebuild_failure_leaves_no_destination(tmp_path: Path) -> None:
    source = _source(tmp_path / "source.sqlite")
    destination = tmp_path / "rebuilt.sqlite"

    with pytest.raises(RuntimeError, match="extractor failed"):
        rebuild_edinet_metrics_v27(
            source,
            destination,
            run_partition=lambda _asof, _path: 1,
        )

    assert not destination.exists()
    assert not destination.with_name(f".{destination.name}.building").exists()
