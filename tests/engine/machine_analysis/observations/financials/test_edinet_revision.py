from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from io import StringIO

import pytest

from baibai_engine.machine_analysis.observations.financials.edinet_metrics.revision import (
    EDINET_METRIC_REVISION,
)


@pytest.mark.parametrize(
    ("change", "expected_exit", "downloads"),
    [
        ("current_revision", 0, 0),
        ("old_scope_revision", 0, 1),
        ("legacy", 0, 1),
        ("other_revision", 0, 1),
        ("null_revision", 0, 1),
        ("failed_coverage", 0, 1),
        ("hard_failure", 1, 0),
        ("corrupt_count", 1, 0),
        ("invalid_ticker", 1, 0),
    ],
)
def test_existing_baseline_reuse_and_failure_contract(tmp_path, change, expected_exit, downloads):
    from tests.helpers.edinet_metrics import (
        FakeEDINETMetricReader,
        _edinet_document,
    )
    from tests.helpers.edinet_metrics import _edinet_csv_zip as csv_zip

    from baibai_engine.machine_analysis.observations.financials.edinet_metrics.service import (
        extract_edinet_metrics_command,
    )

    db = tmp_path / "market.sqlite"
    document = _edinet_document(ticker="72030", doc_id="S100BASELINE")
    initial = FakeEDINETMetricReader(
        documents=[document], zip_by_doc_id={"S100BASELINE": csv_zip()}
    )
    assert (
        extract_edinet_metrics_command(
            asof_date=date(2026, 4, 24),
            lookback_days=0,
            reader=initial,
            sqlite_path=db,
            stdout=StringIO(),
        )
        == 0
    )
    with closing(sqlite3.connect(db)) as connection, connection:
        original = connection.execute("SELECT * FROM edinet_metrics ORDER BY ticker").fetchall()
        assert connection.execute("SELECT extractor_revision FROM edinet_metrics").fetchone() == (
            EDINET_METRIC_REVISION,
        )
        if change == "legacy":
            connection.execute(
                "UPDATE edinet_metrics SET extractor_revision=?",
                ("9b82e8d0d13ac87aaf325561de0d93fb54466178254f49856beff1b433e07187",),
            )
        elif change == "old_scope_revision":
            connection.execute(
                "UPDATE edinet_metrics SET extractor_revision=?",
                ("edinet-metrics-v6-capex-total-presence",),
            )
        elif change in {"other_revision", "null_revision"}:
            connection.execute(
                "UPDATE edinet_metrics SET extractor_revision=?",
                ("different-meaning" if change == "other_revision" else None,),
            )
        elif change == "failed_coverage":
            connection.execute(
                "UPDATE source_coverage SET status='failed' WHERE source='edinet_metrics'"
            )
        elif change == "hard_failure":
            connection.execute(
                "UPDATE edinet_metrics SET failure_reasons=?", ('["csv_parse_failed"]',)
            )
        elif change == "corrupt_count":
            connection.execute(
                "UPDATE source_coverage SET record_count=999 WHERE source='edinet_metrics'"
            )
        elif change == "invalid_ticker":
            connection.execute("UPDATE edinet_metrics SET ticker='invalid'")
        retained = connection.execute("SELECT * FROM edinet_metrics ORDER BY ticker").fetchall()
    # A missing ZIP for the current revision makes accidental re-extraction fail.
    retry = FakeEDINETMetricReader(
        documents=[document], zip_by_doc_id={"S100BASELINE": csv_zip()} if downloads else {}
    )
    assert (
        extract_edinet_metrics_command(
            asof_date=date(2026, 4, 25),
            lookback_days=0,
            reader=retry,
            sqlite_path=db,
            stdout=StringIO(),
        )
        == expected_exit
    )
    assert len(retry.read_calls) == downloads
    with closing(sqlite3.connect(db)) as connection, connection:
        latest = connection.execute(
            "SELECT * FROM edinet_metrics WHERE asof_date='2026-04-25'"
        ).fetchall()
    if expected_exit:
        assert latest == []
        with closing(sqlite3.connect(db)) as connection:
            assert (
                connection.execute("SELECT * FROM edinet_metrics ORDER BY ticker").fetchall()
                == retained
            )
    elif change == "current_revision":
        # All extracted columns, including provenance and quality, are reused unchanged.
        assert [row[1:] for row in latest] == [row[1:] for row in original]
    elif change == "old_scope_revision":
        with closing(sqlite3.connect(db)) as connection:
            assert connection.execute(
                "SELECT extractor_revision FROM edinet_metrics WHERE asof_date='2026-04-25'"
            ).fetchone() == (EDINET_METRIC_REVISION,)
