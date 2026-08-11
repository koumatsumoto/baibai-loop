"""The market-store merge must lose no row from either side."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import date
from pathlib import Path

import pytest
from tests.helpers.screening_sqlite import add_source_coverage

from baibai_batch.storage.merge_market_store import (
    ALL_TABLES,
    FACT_KEYS,
    SOURCE_MISSING_ALLOWED,
    UNCOMPARED,
    MergeError,
    main,
    merge_stores,
)
from baibai_engine.market.sqlite.schema import SQLITE_SCHEMA_VERSION
from baibai_engine.screening.sqlite_cache import (
    open_connection,
    store_jquants_short_sale_reports,
)


def _store(path: Path) -> Path:
    open_connection(path).close()
    return path


def _add_bar(path: Path, ticker: str, traded_at: str, close: float) -> None:
    conn = open_connection(path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO jquants_daily_bars"
            "(ticker, traded_at, close, adjustment_close) VALUES (?, ?, ?, ?)",
            (ticker, traded_at, close, close),
        )
        conn.commit()
    finally:
        conn.close()


def _add_coverage(path: Path, source: str, coverage_key: str, *, record_count: int = 1) -> None:
    conn = open_connection(path)
    try:
        add_source_coverage(
            conn,
            source=source,
            coverage_key=coverage_key,
            record_count=record_count,
            fetched_at_utc="2026-07-31T00:00:00+00:00",
        )
        conn.commit()
    finally:
        conn.close()


def _add_edinet_metric(path: Path, *, extractor_revision: str, sales_ttm: float = 100.0) -> None:
    conn = open_connection(path)
    try:
        conn.execute(
            "INSERT INTO edinet_metrics("
            "asof_date, ticker, sales_ttm, source_document_revision, extractor_revision"
            ") VALUES (?, ?, ?, ?, ?)",
            ("2026-08-07", "1301", sales_ttm, "same-document", extractor_revision),
        )
        conn.commit()
    finally:
        conn.close()


def _add_fin_summary(
    path: Path,
    *,
    ticker: str = "1301",
    disclosed_at: str = "2020-02-07",
    treasury_shares: float | None,
    equity_to_asset_ratio: float | None,
    forecast_profit: float | None = None,
    forecast_ordinary_profit: float | None = None,
) -> None:
    conn = open_connection(path)
    try:
        conn.execute(
            "INSERT INTO jquants_fin_summaries("
            "ticker, disclosed_at, sales, forecast_profit, forecast_ordinary_profit, "
            "treasury_shares, equity_to_asset_ratio"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ticker,
                disclosed_at,
                100.0,
                forecast_profit,
                forecast_ordinary_profit,
                treasury_shares,
                equity_to_asset_ratio,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _bars(path: Path) -> list[tuple[str, str, float]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [
            (str(row[0]), str(row[1]), float(row[2]))
            for row in conn.execute(
                "SELECT ticker, traded_at, close FROM jquants_daily_bars ORDER BY 1, 2"
            )
        ]
    finally:
        conn.close()


def _short_record(name: str, ratio: float) -> dict[str, object]:
    return {
        "DiscDate": "2026-08-01",
        "CalcDate": "2026-07-31",
        "Code": "72030",
        "SSName": name,
        "ShrtPosToSO": ratio,
    }


def _store_short_snapshot(
    path: Path,
    records: list[dict[str, object]],
    *,
    fetched_at_utc: str,
) -> None:
    store_jquants_short_sale_reports(
        path,
        records,
        requested_start=date.fromisoformat("2026-08-01"),
        requested_end=date.fromisoformat("2026-08-01"),
    )
    conn = open_connection(path)
    try:
        conn.execute(
            "UPDATE source_coverage SET fetched_at_utc = ? "
            "WHERE source = 'jquants_short_sale_reports'",
            (fetched_at_utc,),
        )
        conn.commit()
    finally:
        conn.close()


def _short_payload(path: Path) -> list[tuple[str, float]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [
            (str(row[0]), float(row[1]))
            for row in conn.execute(
                "SELECT short_seller_name, short_ratio FROM jquants_short_sale_reports "
                "ORDER BY short_seller_name"
            )
        ]
    finally:
        conn.close()


def test_every_market_table_is_merged(tmp_path: Path) -> None:
    """A table the store carries but the merge does not name would be dropped in silence."""

    path = _store(tmp_path / "market.sqlite")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        present = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        conn.close()
    assert set(ALL_TABLES) == present


def test_each_declared_key_is_the_tables_primary_key(tmp_path: Path) -> None:
    """A key that is not the one SQLite enforces would let the merge duplicate rows."""

    path = _store(tmp_path / "market.sqlite")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for table, keys in ALL_TABLES.items():
            primary = tuple(
                str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")') if row[5]
            )
            assert set(keys) == set(primary), table
    finally:
        conn.close()


def test_a_row_only_the_source_has_is_carried_over(tmp_path: Path) -> None:
    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    _add_bar(source, "7203", "2020-01-06", 100.0)
    _add_bar(target, "7203", "2024-01-05", 200.0)

    report = merge_stores(source, target)

    assert _bars(target) == [("7203", "2020-01-06", 100.0), ("7203", "2024-01-05", 200.0)]
    assert report.inserted == 1


def test_a_row_only_the_target_has_is_kept(tmp_path: Path) -> None:
    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    _add_bar(target, "7203", "2024-01-05", 200.0)

    merge_stores(source, target)

    assert _bars(target) == [("7203", "2024-01-05", 200.0)]


def test_short_sale_snapshot_merge_ignores_provider_response_order(tmp_path: Path) -> None:
    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    rows = [_short_record("alpha", 0.01), _short_record("beta", 0.02)]
    _store_short_snapshot(
        target,
        rows,
        fetched_at_utc="2026-08-01T01:00:00+00:00",
    )
    _store_short_snapshot(
        source,
        list(reversed(rows)),
        fetched_at_utc="2026-08-01T02:00:00+00:00",
    )

    merge_stores(source, target)

    assert _short_payload(target) == [("alpha", 0.01), ("beta", 0.02)]


def test_short_sale_snapshot_merge_uses_newer_complete_correction(tmp_path: Path) -> None:
    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    _store_short_snapshot(
        target,
        [_short_record("alpha", 0.01), _short_record("beta", 0.02)],
        fetched_at_utc="2026-08-01T01:00:00+00:00",
    )
    _store_short_snapshot(
        source,
        [_short_record("alpha", 0.03)],
        fetched_at_utc="2026-08-01T02:00:00+00:00",
    )

    merge_stores(source, target)

    assert _short_payload(target) == [("alpha", 0.03)]


def test_a_shared_key_that_disagrees_stops_the_merge(tmp_path: Path) -> None:
    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    _add_bar(source, "7203", "2024-01-05", 111.0)
    _add_bar(target, "7203", "2024-01-05", 222.0)
    _add_bar(source, "6758", "2020-01-06", 50.0)

    with pytest.raises(MergeError, match="payload disagrees"):
        merge_stores(source, target)

    # The target keeps its own value and gains nothing: the merge is one transaction.
    assert _bars(target) == [("7203", "2024-01-05", 222.0)]


def test_coverage_rows_the_cloud_recorded_are_carried_over(tmp_path: Path) -> None:
    """Coverage decides what a later fetch may skip, so losing it re-fetches silently."""

    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    _add_coverage(source, "jquants_daily_bars", "2026-07-31")
    _add_coverage(target, "jquants_daily_bars", "2016-08-01")

    merge_stores(source, target)

    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    try:
        keys = [str(row[0]) for row in conn.execute("SELECT coverage_key FROM source_coverage")]
    finally:
        conn.close()
    assert sorted(keys) == ["2016-08-01", "2026-07-31"]


def test_a_source_row_the_insert_could_not_place_stops_the_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last check must fire when a row is ignored rather than inserted.

    Declaring a key wider than the one SQLite enforces reproduces that: the insert is
    ignored on the real primary key while the merge still considers the row unmatched.
    A store whose declared key drifts from its schema would otherwise publish quietly.
    """

    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    _add_bar(source, "7203", "2024-01-05", 111.0)
    _add_bar(target, "7203", "2024-01-05", 222.0)
    wider: Mapping[str, tuple[str, ...]] = {"jquants_daily_bars": ("ticker", "traded_at", "close")}
    monkeypatch.setattr("baibai_batch.storage.merge_market_store.FACT_KEYS", wider)

    with pytest.raises(MergeError, match="still missing after the merge"):
        merge_stores(source, target)

    assert _bars(target) == [("7203", "2024-01-05", 222.0)]


def test_a_store_on_another_schema_is_refused(tmp_path: Path) -> None:
    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    _add_bar(source, "7203", "2020-01-06", 100.0)
    conn = sqlite3.connect(source)
    try:
        conn.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION - 1}")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(MergeError, match="schema is"):
        merge_stores(source, target)

    assert _bars(target) == []


def test_a_store_whose_table_shape_drifted_is_refused(tmp_path: Path) -> None:
    """The merge inserts whole rows, so a store one column wider must not reach it."""

    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    conn = sqlite3.connect(target)
    try:
        conn.execute("ALTER TABLE jquants_market_calendar ADD COLUMN extra TEXT")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(MergeError, match="schema contract is invalid"):
        merge_stores(source, target)


def test_a_missing_store_is_named_rather_than_crashing(tmp_path: Path) -> None:
    target = _store(tmp_path / "target.sqlite")

    with pytest.raises(MergeError, match="does not exist"):
        merge_stores(tmp_path / "absent.sqlite", target)


def test_the_command_reports_a_refusal_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = _store(tmp_path / "target.sqlite")

    code = main(["--source", str(tmp_path / "absent.sqlite"), "--target", str(target)])

    assert code == 1
    assert "does not exist" in capsys.readouterr().err


def test_the_command_prints_what_it_merged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    _add_bar(source, "7203", "2020-01-06", 100.0)

    code = main(["--source", str(source), "--target", str(target)])

    assert code == 0
    output = capsys.readouterr().out
    assert "jquants_daily_bars" in output
    assert "merged 1 rows" in output


def test_a_fact_column_that_disagrees_still_stops_the_merge(tmp_path: Path) -> None:
    """The exemptions must not reach a column the source actually asserts."""

    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    _add_coverage(source, "jquants_daily_bars", "2026-07-31", record_count=10)
    _add_coverage(target, "jquants_daily_bars", "2026-07-31", record_count=99)

    with pytest.raises(MergeError, match="payload disagrees"):
        merge_stores(source, target)


def test_two_stores_that_read_the_same_day_at_different_times_still_merge(
    tmp_path: Path,
) -> None:
    """`fetched_at_utc` says when a store read, so it always differs and must not refuse."""

    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    for path, fetched in (
        (source, "2026-07-30T14:43:18+00:00"),
        (target, "2026-07-31T13:30:36+00:00"),
    ):
        conn = open_connection(path)
        try:
            add_source_coverage(
                conn,
                source="edinet_documents",
                coverage_key="2026-07-30",
                record_count=220,
                fetched_at_utc=fetched,
            )
            conn.commit()
        finally:
            conn.close()

    merge_stores(source, target)

    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    try:
        kept = conn.execute("SELECT fetched_at_utc FROM source_coverage").fetchone()
    finally:
        conn.close()
    assert kept is not None
    assert kept[0] == "2026-07-31T13:30:36+00:00"


def test_edinet_reader_revision_does_not_conflict_when_document_facts_agree(
    tmp_path: Path,
) -> None:
    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    _add_edinet_metric(source, extractor_revision="source-reader")
    _add_edinet_metric(target, extractor_revision="target-reader")

    report = merge_stores(source, target)

    assert report.inserted == 0
    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    try:
        kept = conn.execute("SELECT extractor_revision FROM edinet_metrics").fetchone()
    finally:
        conn.close()
    assert kept == ("target-reader",)


def test_edinet_fact_disagreement_still_conflicts_across_reader_revisions(
    tmp_path: Path,
) -> None:
    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    _add_edinet_metric(source, extractor_revision="source-reader", sales_ttm=100.0)
    _add_edinet_metric(target, extractor_revision="target-reader", sales_ttm=200.0)

    with pytest.raises(MergeError, match="payload disagrees"):
        merge_stores(source, target)


def test_fin_summary_source_may_lack_facts_held_by_rebuilt_target(tmp_path: Path) -> None:
    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    _add_fin_summary(source, treasury_shares=None, equity_to_asset_ratio=None)
    _add_fin_summary(
        target,
        treasury_shares=10.0,
        equity_to_asset_ratio=0.5,
        forecast_profit=30.0,
        forecast_ordinary_profit=40.0,
    )

    report = merge_stores(source, target)

    assert report.inserted == 0
    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    try:
        kept = conn.execute(
            "SELECT forecast_profit, forecast_ordinary_profit, treasury_shares, "
            "equity_to_asset_ratio FROM jquants_fin_summaries"
        ).fetchone()
    finally:
        conn.close()
    assert kept == (30.0, 40.0, 10.0, 0.5)


def test_fin_summary_coverage_accepts_a_recounted_source_subset(tmp_path: Path) -> None:
    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    _add_fin_summary(source, treasury_shares=None, equity_to_asset_ratio=None)
    _add_fin_summary(target, treasury_shares=None, equity_to_asset_ratio=None)
    _add_fin_summary(
        target,
        ticker="1302",
        disclosed_at="2020-03-06",
        treasury_shares=None,
        equity_to_asset_ratio=None,
    )
    coverage_key = "get_fin_summary_range:2020-01-01..2020-12-31"
    for path, record_count in ((source, 1), (target, 2)):
        conn = open_connection(path)
        try:
            add_source_coverage(
                conn,
                source="jquants_fin_summaries",
                coverage_key=coverage_key,
                record_count=record_count,
                min_date="2020-01-01",
                max_date="2020-12-31",
                fetched_at_utc="2026-08-08T00:00:00+00:00",
            )
            conn.commit()
        finally:
            conn.close()

    report = merge_stores(source, target)

    assert report.inserted == 0


def test_fin_summary_coverage_accepts_a_recounted_source_superset(tmp_path: Path) -> None:
    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    _add_fin_summary(source, treasury_shares=None, equity_to_asset_ratio=None)
    _add_fin_summary(
        source,
        ticker="1302",
        disclosed_at="2020-03-06",
        treasury_shares=None,
        equity_to_asset_ratio=None,
    )
    _add_fin_summary(target, treasury_shares=None, equity_to_asset_ratio=None)
    coverage_key = "get_fin_summary_range:2020-01-01..2020-12-31"
    for path, record_count in ((source, 2), (target, 1)):
        conn = open_connection(path)
        try:
            add_source_coverage(
                conn,
                source="jquants_fin_summaries",
                coverage_key=coverage_key,
                record_count=record_count,
                min_date="2020-01-01",
                max_date="2020-12-31",
                fetched_at_utc="2026-08-08T00:00:00+00:00",
            )
            conn.commit()
        finally:
            conn.close()

    report = merge_stores(source, target)

    assert report.inserted == 1
    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT ticker FROM jquants_fin_summaries ORDER BY ticker").fetchall()
        claim = conn.execute(
            "SELECT record_count FROM source_coverage "
            "WHERE source = 'jquants_fin_summaries' AND coverage_key = ?",
            (coverage_key,),
        ).fetchone()
    finally:
        conn.close()
    assert rows == [("1301",), ("1302",)]
    assert claim == (2,)


def test_fin_summary_coverage_recounts_equal_claims_with_different_rows(tmp_path: Path) -> None:
    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    _add_fin_summary(
        source,
        ticker="1301",
        treasury_shares=None,
        equity_to_asset_ratio=None,
    )
    _add_fin_summary(
        target,
        ticker="1302",
        treasury_shares=None,
        equity_to_asset_ratio=None,
    )
    coverage_key = "get_fin_summary_range:2020-01-01..2020-12-31"
    for path in (source, target):
        conn = open_connection(path)
        try:
            add_source_coverage(
                conn,
                source="jquants_fin_summaries",
                coverage_key=coverage_key,
                record_count=1,
                min_date="2020-01-01",
                max_date="2020-12-31",
                fetched_at_utc="2026-08-08T00:00:00+00:00",
            )
            conn.commit()
        finally:
            conn.close()

    merge_stores(source, target)

    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    try:
        kept = conn.execute("SELECT ticker FROM jquants_fin_summaries ORDER BY ticker").fetchall()
        claim = conn.execute(
            "SELECT record_count FROM source_coverage "
            "WHERE source = 'jquants_fin_summaries' AND coverage_key = ?",
            (coverage_key,),
        ).fetchone()
    finally:
        conn.close()
    assert kept == [("1301",), ("1302",)]
    assert claim == (2,)


def test_fin_summary_coverage_recounts_a_source_only_range_after_merge(tmp_path: Path) -> None:
    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    _add_fin_summary(
        source,
        ticker="1301",
        treasury_shares=None,
        equity_to_asset_ratio=None,
    )
    _add_fin_summary(
        target,
        ticker="1301",
        treasury_shares=None,
        equity_to_asset_ratio=None,
    )
    _add_fin_summary(
        target,
        ticker="1302",
        disclosed_at="2020-03-06",
        treasury_shares=None,
        equity_to_asset_ratio=None,
    )
    coverage_key = "get_fin_summary_range:2020-01-01..2020-12-31"
    conn = open_connection(source)
    try:
        add_source_coverage(
            conn,
            source="jquants_fin_summaries",
            coverage_key=coverage_key,
            record_count=1,
            min_date="2020-01-01",
            max_date="2020-12-31",
            fetched_at_utc="2026-08-08T00:00:00+00:00",
        )
        conn.commit()
    finally:
        conn.close()

    merge_stores(source, target)

    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    try:
        kept = conn.execute("SELECT ticker FROM jquants_fin_summaries ORDER BY ticker").fetchall()
        claim = conn.execute(
            "SELECT record_count FROM source_coverage "
            "WHERE source = 'jquants_fin_summaries' AND coverage_key = ?",
            (coverage_key,),
        ).fetchone()
    finally:
        conn.close()
    assert kept == [("1301",), ("1302",)]
    assert claim == (2,)


def test_fin_summary_coverage_recounts_a_target_only_range_after_merge(tmp_path: Path) -> None:
    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    _add_fin_summary(
        source,
        ticker="1301",
        treasury_shares=None,
        equity_to_asset_ratio=None,
    )
    _add_fin_summary(
        target,
        ticker="1302",
        treasury_shares=None,
        equity_to_asset_ratio=None,
    )
    coverage_key = "get_fin_summary_range:2020-01-01..2020-12-31"
    conn = open_connection(target)
    try:
        add_source_coverage(
            conn,
            source="jquants_fin_summaries",
            coverage_key=coverage_key,
            record_count=1,
            min_date="2020-01-01",
            max_date="2020-12-31",
            fetched_at_utc="2026-08-08T00:00:00+00:00",
        )
        conn.commit()
    finally:
        conn.close()

    merge_stores(source, target)

    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    try:
        kept = conn.execute("SELECT ticker FROM jquants_fin_summaries ORDER BY ticker").fetchall()
        claim = conn.execute(
            "SELECT record_count FROM source_coverage "
            "WHERE source = 'jquants_fin_summaries' AND coverage_key = ?",
            (coverage_key,),
        ).fetchone()
    finally:
        conn.close()
    assert kept == [("1301",), ("1302",)]
    assert claim == (2,)


@pytest.mark.parametrize("status", ["partial", "failed"])
def test_fin_summary_non_ok_coverage_with_matching_payload_still_merges(
    tmp_path: Path, status: str
) -> None:
    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    coverage_key = "get_fin_summary_range:2020-01-01..2020-12-31"
    for path in (source, target):
        conn = open_connection(path)
        try:
            add_source_coverage(
                conn,
                source="jquants_fin_summaries",
                coverage_key=coverage_key,
                record_count=0,
                min_date="2020-01-01",
                max_date="2020-12-31",
                status=status,
                error="one rejected row",
                fetched_at_utc="2026-08-08T00:00:00+00:00",
            )
            conn.commit()
        finally:
            conn.close()

    report = merge_stores(source, target)

    assert report.inserted == 0


def test_fin_summary_source_only_non_ok_coverage_is_preserved(tmp_path: Path) -> None:
    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    coverage_key = "get_fin_summary_range:2020-01-01..2020-12-31"
    conn = open_connection(source)
    try:
        add_source_coverage(
            conn,
            source="jquants_fin_summaries",
            coverage_key=coverage_key,
            record_count=0,
            min_date="2020-01-01",
            max_date="2020-12-31",
            status="partial",
            error="one rejected row",
            fetched_at_utc="2026-08-08T00:00:00+00:00",
        )
        conn.commit()
    finally:
        conn.close()

    report = merge_stores(source, target)

    assert report.inserted == 1
    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    try:
        kept = conn.execute(
            "SELECT record_count, status, error FROM source_coverage "
            "WHERE source = 'jquants_fin_summaries' AND coverage_key = ?",
            (coverage_key,),
        ).fetchone()
    finally:
        conn.close()
    assert kept == (0, "partial", "one rejected row")


@pytest.mark.parametrize(
    ("placement", "status", "error"),
    [
        ("source", "ok", "provider failed"),
        ("target", "ok", "provider failed"),
        ("both", "ok", "provider failed"),
        ("source", "unknown", "provider failed"),
        ("source", "partial", None),
        ("source", "failed", ""),
    ],
)
def test_fin_summary_coverage_rejects_unclassified_status_error_states(
    tmp_path: Path,
    placement: str,
    status: str,
    error: str | None,
) -> None:
    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    coverage_key = "get_fin_summary_range:2020-01-01..2020-12-31"
    paths = {
        "source": (source,),
        "target": (target,),
        "both": (source, target),
    }[placement]
    for path in paths:
        conn = open_connection(path)
        try:
            add_source_coverage(
                conn,
                source="jquants_fin_summaries",
                coverage_key=coverage_key,
                record_count=0,
                min_date="2020-01-01",
                max_date="2020-12-31",
                status=status,
                error=error,
                fetched_at_utc="2026-08-08T00:00:00+00:00",
            )
            conn.commit()
        finally:
            conn.close()

    with pytest.raises(MergeError, match="payload disagrees"):
        merge_stores(source, target)


def test_fin_summary_coverage_rejects_a_count_not_proven_by_stored_rows(
    tmp_path: Path,
) -> None:
    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    _add_fin_summary(source, treasury_shares=None, equity_to_asset_ratio=None)
    _add_fin_summary(target, treasury_shares=None, equity_to_asset_ratio=None)
    coverage_key = "get_fin_summary_range:2020-01-01..2020-12-31"
    for path, record_count in ((source, 1), (target, 2)):
        conn = open_connection(path)
        try:
            add_source_coverage(
                conn,
                source="jquants_fin_summaries",
                coverage_key=coverage_key,
                record_count=record_count,
                min_date="2020-01-01",
                max_date="2020-12-31",
                fetched_at_utc="2026-08-08T00:00:00+00:00",
            )
            conn.commit()
        finally:
            conn.close()

    with pytest.raises(MergeError, match="coverage count does not match stored rows"):
        merge_stores(source, target)


@pytest.mark.parametrize(
    ("source_treasury", "target_treasury"),
    [(10.0, None), (10.0, 20.0)],
)
def test_fin_summary_missing_allowance_is_directional_and_rejects_fact_conflicts(
    tmp_path: Path,
    source_treasury: float,
    target_treasury: float | None,
) -> None:
    source = _store(tmp_path / "source.sqlite")
    target = _store(tmp_path / "target.sqlite")
    _add_fin_summary(source, treasury_shares=source_treasury, equity_to_asset_ratio=0.5)
    _add_fin_summary(target, treasury_shares=target_treasury, equity_to_asset_ratio=0.5)

    with pytest.raises(MergeError, match="payload disagrees"):
        merge_stores(source, target)


def test_the_exempt_columns_are_only_the_ones_named(tmp_path: Path) -> None:
    """A column added to a table must be compared unless someone exempts it deliberately."""

    path = _store(tmp_path / "market.sqlite")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for table, exempt in UNCOMPARED.items():
            present = {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}
            assert set(exempt) <= present, table
            assert set(exempt) & set(FACT_KEYS[table]) == set(), table
        for table, source_missing in SOURCE_MISSING_ALLOWED.items():
            present = {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}
            assert set(source_missing) <= present, table
            assert set(source_missing) & set(FACT_KEYS[table]) == set(), table
    finally:
        conn.close()
    assert set(UNCOMPARED) <= set(FACT_KEYS)
    assert set(SOURCE_MISSING_ALLOWED) <= set(FACT_KEYS)


def _add_exit_value(path: Path, ticker: str, price: float) -> None:
    conn = open_connection(path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO tender_offer_exit_values("
            "ticker, delisted_on, offer_price_yen, offer_doc_id, result_doc_id, filed_on"
            ") VALUES (?, '2026-05-01', ?, 'REG', 'RES', '2026-02-01')",
            (ticker, price),
        )
        conn.commit()
    finally:
        conn.close()


def test_a_retracted_exit_value_is_not_resurrected_from_the_published_copy(
    tmp_path: Path,
) -> None:
    """A later derivation that could not establish the price has to win.

    The published copy holds what an earlier derivation could see. Reinserting it would
    put a price into the calibration forward that the current rules refuse to establish.
    """

    published = _store(tmp_path / "published.sqlite")
    local = _store(tmp_path / "local.sqlite")
    _add_exit_value(published, "2000", 1060.0)

    merge_stores(published, local)

    conn = sqlite3.connect(f"file:{local}?mode=ro", uri=True)
    try:
        assert conn.execute("SELECT count(*) FROM tender_offer_exit_values").fetchone()[0] == 0
    finally:
        conn.close()


def test_a_corrected_exit_value_does_not_block_the_publish(tmp_path: Path) -> None:
    published = _store(tmp_path / "published.sqlite")
    local = _store(tmp_path / "local.sqlite")
    _add_exit_value(published, "2000", 1060.0)
    _add_exit_value(local, "2000", 1200.0)

    merge_stores(published, local)

    conn = sqlite3.connect(f"file:{local}?mode=ro", uri=True)
    try:
        assert (
            conn.execute("SELECT offer_price_yen FROM tender_offer_exit_values").fetchone()[0]
            == 1200.0
        )
    finally:
        conn.close()


def test_identity_columns_the_published_copy_never_wrote_do_not_refuse_the_publish(
    tmp_path: Path,
) -> None:
    """The daily refresh only rewrites the current day, so history stays null cloud-side.

    Comparing those nulls strictly would refuse every publish, and the only way to
    advance the published copy is a publish.
    """

    published = _store(tmp_path / "published.sqlite")
    local = _store(tmp_path / "local.sqlite")
    for path, code in ((published, None), (local, "E00001")):
        conn = open_connection(path)
        try:
            conn.execute(
                "INSERT INTO edinet_documents("
                "doc_date, sequence_number, doc_id, doc_type_code, edinet_code"
                ") VALUES ('2026-05-01', 1, 'S1', '120', ?)",
                (code,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO edinet_document_lists("
                "doc_date, result_count, fetched_at_utc, is_final"
                ") VALUES ('2026-05-01', 1, '2026-05-01T00:00:00+00:00', 1)"
            )
            conn.commit()
        finally:
            conn.close()

    merge_stores(published, local)

    conn = sqlite3.connect(f"file:{local}?mode=ro", uri=True)
    try:
        assert conn.execute("SELECT edinet_code FROM edinet_documents").fetchone()[0] == "E00001"
    finally:
        conn.close()


def test_two_populated_identity_values_that_disagree_still_refuse_the_publish(
    tmp_path: Path,
) -> None:
    published = _store(tmp_path / "published.sqlite")
    local = _store(tmp_path / "local.sqlite")
    for path, code in ((published, "E00002"), (local, "E00001")):
        conn = open_connection(path)
        try:
            conn.execute(
                "INSERT INTO edinet_documents("
                "doc_date, sequence_number, doc_id, doc_type_code, edinet_code"
                ") VALUES ('2026-05-01', 1, 'S1', '120', ?)",
                (code,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO edinet_document_lists("
                "doc_date, result_count, fetched_at_utc, is_final"
                ") VALUES ('2026-05-01', 1, '2026-05-01T00:00:00+00:00', 1)"
            )
            conn.commit()
        finally:
            conn.close()

    with pytest.raises(MergeError, match="edinet_documents payload disagrees"):
        merge_stores(published, local)


def test_a_reworded_delisting_row_does_not_refuse_the_publish(tmp_path: Path) -> None:
    """JPX rewords its archive, and only the operator ever writes this table."""

    published = _store(tmp_path / "published.sqlite")
    local = _store(tmp_path / "local.sqlite")
    for path, reason in ((published, "株式の併合"), (local, "ＭＢＯ（公開買付け、株式併合）")):
        conn = open_connection(path)
        try:
            conn.execute(
                "INSERT INTO jpx_delistings(delisted_on, ticker, name, market, reason) "
                "VALUES ('2026-05-01', '2000', 'テスト', 'プライム', ?)",
                (reason,),
            )
            conn.commit()
        finally:
            conn.close()

    merge_stores(published, local)

    conn = sqlite3.connect(f"file:{local}?mode=ro", uri=True)
    try:
        assert (
            conn.execute("SELECT reason FROM jpx_delistings").fetchone()[0]
            == "ＭＢＯ（公開買付け、株式併合）"
        )
    finally:
        conn.close()


# What a filing's list entry looks like while EDINET still serves it, and what the same
# entry becomes once its public-inspection period ends. The copy that read the day first
# holds the description; no fetch can recover it afterwards.
_SERVED_DOCUMENT = {
    "doc_id": "S100AAAA",
    "sec_code": "72030",
    "doc_type_code": "220",
    "parent_doc_id": "S100PARENT",
    "submit_datetime": "2026-05-01 15:49",
    "doc_description": "自己株券買付状況報告書",
    "csv_flag": "1",
    "xbrl_flag": "1",
    "legal_status": "1",
    "withdrawal_status": "0",
}
_EXPIRED_DOCUMENT = {
    "doc_id": "S100AAAA",
    "sec_code": None,
    "doc_type_code": None,
    "parent_doc_id": None,
    "submit_datetime": None,
    "doc_description": None,
    "csv_flag": "0",
    "xbrl_flag": "0",
    "legal_status": "0",
    "withdrawal_status": "0",
}


def _add_document(path: Path, columns: Mapping[str, str | None]) -> None:
    names = ("doc_date", "sequence_number", *columns)
    values = ("2026-05-01", 1, *columns.values())
    placeholders = ", ".join("?" for _ in names)
    conn = open_connection(path)
    try:
        conn.execute(
            f"INSERT INTO edinet_documents({', '.join(names)}) "  # nosec B608
            f"VALUES ({placeholders})",
            values,
        )
        conn.execute(
            "INSERT OR REPLACE INTO edinet_document_lists("
            "doc_date, result_count, fetched_at_utc, is_final"
            ") VALUES ('2026-05-01', 1, '2026-05-01T00:00:00+00:00', 1)"
        )
        conn.commit()
    finally:
        conn.close()


def _read_document(path: Path) -> Mapping[str, str | None]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM edinet_documents").fetchone()
        return dict(zip(row.keys(), row, strict=True))
    finally:
        conn.close()


def test_a_description_the_local_copy_read_too_late_is_restored_from_the_published_copy(
    tmp_path: Path,
) -> None:
    """Neither copy can fetch it again, so the merge keeps whichever one observed it."""

    published = _store(tmp_path / "published.sqlite")
    local = _store(tmp_path / "local.sqlite")
    _add_document(published, _SERVED_DOCUMENT)
    _add_document(local, _EXPIRED_DOCUMENT)

    report = merge_stores(published, local)

    row = _read_document(local)
    assert row["doc_type_code"] == "220"
    assert row["sec_code"] == "72030"
    assert row["doc_description"] == "自己株券買付状況報告書"
    assert "restored: 1" in report.render()


def test_restoring_an_expired_description_leaves_the_local_lifecycle_reading_alone(
    tmp_path: Path,
) -> None:
    published = _store(tmp_path / "published.sqlite")
    local = _store(tmp_path / "local.sqlite")
    _add_document(published, _SERVED_DOCUMENT)
    _add_document(local, _EXPIRED_DOCUMENT)

    merge_stores(published, local)

    row = _read_document(local)
    assert row["legal_status"] == "0"
    assert row["csv_flag"] == "0"


def test_restoring_expired_descriptions_is_idempotent(tmp_path: Path) -> None:
    published = _store(tmp_path / "published.sqlite")
    local = _store(tmp_path / "local.sqlite")
    _add_document(published, _SERVED_DOCUMENT)
    _add_document(local, _EXPIRED_DOCUMENT)

    merge_stores(published, local)
    second = merge_stores(published, local)

    assert "restored" not in second.render()
    assert _read_document(local)["doc_type_code"] == "220"


def test_a_description_only_the_local_copy_still_holds_does_not_refuse_the_publish(
    tmp_path: Path,
) -> None:
    """The published copy is the one that read the day after the period ended."""

    published = _store(tmp_path / "published.sqlite")
    local = _store(tmp_path / "local.sqlite")
    _add_document(published, _EXPIRED_DOCUMENT)
    _add_document(local, _SERVED_DOCUMENT)

    merge_stores(published, local)

    assert _read_document(local)["doc_type_code"] == "220"


def test_two_populated_descriptions_that_disagree_still_refuse_the_publish(
    tmp_path: Path,
) -> None:
    """Expiry only ever removes a value, so two different values are a corruption."""

    published = _store(tmp_path / "published.sqlite")
    local = _store(tmp_path / "local.sqlite")
    _add_document(published, _SERVED_DOCUMENT)
    _add_document(local, {**_SERVED_DOCUMENT, "doc_type_code": "230"})

    with pytest.raises(MergeError, match="edinet_documents payload disagrees"):
        merge_stores(published, local)


def test_a_description_is_not_grafted_onto_a_different_filing_at_the_same_position(
    tmp_path: Path,
) -> None:
    """A day returned in a different order must refuse rather than mix two filings."""

    published = _store(tmp_path / "published.sqlite")
    local = _store(tmp_path / "local.sqlite")
    _add_document(published, _SERVED_DOCUMENT)
    _add_document(local, {**_EXPIRED_DOCUMENT, "doc_id": "S100BBBB"})

    with pytest.raises(MergeError, match="edinet_documents payload disagrees"):
        merge_stores(published, local)

    assert _read_document(local)["doc_type_code"] is None


def test_lifecycle_columns_read_at_different_moments_do_not_refuse_the_publish(
    tmp_path: Path,
) -> None:
    published = _store(tmp_path / "published.sqlite")
    local = _store(tmp_path / "local.sqlite")
    _add_document(published, _SERVED_DOCUMENT)
    _add_document(
        local,
        {**_SERVED_DOCUMENT, "legal_status": "2", "withdrawal_status": "2", "csv_flag": "0"},
    )

    merge_stores(published, local)

    row = _read_document(local)
    assert row["legal_status"] == "2"
    assert row["withdrawal_status"] == "2"


def test_a_column_outside_the_two_classifications_still_refuses_the_publish(
    tmp_path: Path,
) -> None:
    published = _store(tmp_path / "published.sqlite")
    local = _store(tmp_path / "local.sqlite")
    _add_document(published, {**_SERVED_DOCUMENT, "operation_datetime": "2026-05-01 15:50"})
    _add_document(local, {**_SERVED_DOCUMENT, "operation_datetime": "2026-05-01 15:51"})

    with pytest.raises(MergeError, match="edinet_documents payload disagrees"):
        merge_stores(published, local)
