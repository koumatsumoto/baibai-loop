"""The market-store merge owns the four tables the lake does not, and loses no row."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest
from tests.helpers.screening_sqlite import add_source_coverage

from baibai_batch.storage.merge_market_store import (
    ALL_TABLES,
    DERIVED_KEYS,
    FACT_KEYS,
    UNCOMPARED,
    MergeError,
    main,
    merge_stores,
)
from baibai_engine.market.lake.datasets import LAKE_DATASETS
from baibai_engine.market.sqlite.schema import SQLITE_SCHEMA_VERSION
from baibai_engine.screening.sqlite_cache import (
    open_connection,
    store_jquants_short_sale_reports,
)

_FIN_RANGE = "get_fin_summary_range:2020-01-01..2020-12-31"


def _store(path: Path) -> Path:
    open_connection(path).close()
    return path


def _add_coverage(
    path: Path,
    source: str,
    coverage_key: str,
    *,
    record_count: int = 1,
    min_date: str | None = None,
    max_date: str | None = None,
    status: str = "ok",
    error: str | None = None,
    fetched_at_utc: str = "2026-07-31T00:00:00+00:00",
) -> None:
    conn = open_connection(path)
    try:
        add_source_coverage(
            conn,
            source=source,
            coverage_key=coverage_key,
            record_count=record_count,
            min_date=min_date,
            max_date=max_date,
            status=status,
            error=error,
            fetched_at_utc=fetched_at_utc,
        )
        conn.commit()
    finally:
        conn.close()


def _add_fin_summary(path: Path, *, ticker: str = "1301", disclosed_at: str = "2020-02-07") -> None:
    conn = open_connection(path)
    try:
        conn.execute(
            "INSERT INTO jquants_fin_summaries(ticker, disclosed_at, sales) VALUES (?, ?, 100.0)",
            (ticker, disclosed_at),
        )
        conn.commit()
    finally:
        conn.close()


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
    status: str = "ok",
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
            "UPDATE source_coverage SET fetched_at_utc = ?, status = ?, error = ? "
            "WHERE source = 'jquants_short_sale_reports'",
            (fetched_at_utc, status, None if status == "ok" else "partial fetch"),
        )
        conn.commit()
    finally:
        conn.close()


def _coverage(path: Path, source: str) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT coverage_key, record_count, status, fetched_at_utc FROM source_coverage "
                "WHERE source = ? ORDER BY coverage_key",
                (source,),
            )
        ]
    finally:
        conn.close()


def _tables(path: Path) -> set[str]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        conn.close()


def test_the_merge_and_the_lake_together_name_every_market_table(tmp_path: Path) -> None:
    """A table neither side claims would be dropped from the publish in silence.

    The merge carries what stays canonical in SQLite and the release carries the rest,
    so a new table has to be classified by someone who knows which it is. Binding the
    two lists to the schema is what makes forgetting fail here rather than in R2.
    """

    present = _tables(_store(tmp_path / "market.sqlite"))
    lake_tables = {dataset.sqlite_table for dataset in LAKE_DATASETS.values()}

    assert set(ALL_TABLES) | lake_tables == present
    assert set(ALL_TABLES) & lake_tables == set()


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


def test_the_exempt_columns_belong_to_a_table_the_merge_unions(tmp_path: Path) -> None:
    """An exemption naming a table nobody merges is a rule that stopped applying."""

    assert set(UNCOMPARED) <= set(FACT_KEYS)
    path = _store(tmp_path / "market.sqlite")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for table, columns in UNCOMPARED.items():
            present = {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}
            assert set(columns) <= present, table
    finally:
        conn.close()


class TestLakeOwnedTables:
    def test_rows_the_release_owns_are_not_carried_by_the_merge(self, tmp_path: Path) -> None:
        """The release reconciles the fifteen tables; copying them here would fork them."""

        source = _store(tmp_path / "source.sqlite")
        target = _store(tmp_path / "target.sqlite")
        _add_bar(source, "1301", "2026-05-01", 100.0)

        report = merge_stores(source, target)

        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        try:
            assert conn.execute("SELECT count(*) FROM jquants_daily_bars").fetchone()[0] == 0
        finally:
            conn.close()
        assert {item.table for item in report.tables} == set(ALL_TABLES)

    def test_a_store_the_lake_emptied_still_merges(self, tmp_path: Path) -> None:
        """The published copy carries claims whose rows the publication moved to the lake.

        This is the shape ``push-market`` actually reads: the object in R2 holds the
        coverage ledger and no fetched fact. Proving its claims against its own tables
        would refuse every merge after the cutover.
        """

        source = _store(tmp_path / "source.sqlite")
        target = _store(tmp_path / "target.sqlite")
        _add_fin_summary(target, ticker="1301")
        _add_fin_summary(target, ticker="1302", disclosed_at="2020-03-06")
        for path in (source, target):
            _add_coverage(
                path,
                "jquants_fin_summaries",
                _FIN_RANGE,
                record_count=2,
                min_date="2020-01-01",
                max_date="2020-12-31",
            )

        merge_stores(source, target)

        assert _coverage(target, "jquants_fin_summaries")[0][1] == 2

    def test_a_target_the_lake_has_not_filled_is_refused(self, tmp_path: Path) -> None:
        """Proving the target's claims is what requires the target to be hydrated.

        An emptied target would otherwise have its claims quietly rewritten down to
        zero, publishing a ledger that says nothing was ever fetched.
        """

        source = _store(tmp_path / "source.sqlite")
        target = _store(tmp_path / "target.sqlite")
        for path in (source, target):
            _add_coverage(
                path,
                "jquants_fin_summaries",
                _FIN_RANGE,
                record_count=2,
                min_date="2020-01-01",
                max_date="2020-12-31",
            )

        with pytest.raises(MergeError, match="coverage count does not match stored rows"):
            merge_stores(source, target)


class TestCoverageLedger:
    def test_a_coverage_row_only_the_source_has_is_carried_over(self, tmp_path: Path) -> None:
        source = _store(tmp_path / "source.sqlite")
        target = _store(tmp_path / "target.sqlite")
        _add_coverage(source, "edinet_documents", "2026-05-01", record_count=3)

        report = merge_stores(source, target)

        assert report.inserted == 1
        assert _coverage(target, "edinet_documents") == [
            ("2026-05-01", 3, "ok", "2026-07-31T00:00:00+00:00")
        ]

    def test_a_coverage_row_only_the_target_has_is_kept(self, tmp_path: Path) -> None:
        source = _store(tmp_path / "source.sqlite")
        target = _store(tmp_path / "target.sqlite")
        _add_coverage(target, "edinet_documents", "2026-05-01", record_count=3)

        report = merge_stores(source, target)

        assert report.inserted == 0
        assert len(_coverage(target, "edinet_documents")) == 1

    def test_a_shared_key_that_disagrees_stops_the_merge(self, tmp_path: Path) -> None:
        source = _store(tmp_path / "source.sqlite")
        target = _store(tmp_path / "target.sqlite")
        _add_coverage(source, "edinet_documents", "2026-05-01", record_count=3, status="ok")
        _add_coverage(
            target,
            "edinet_documents",
            "2026-05-01",
            record_count=3,
            status="partial",
            error="truncated",
        )

        with pytest.raises(MergeError, match="source_coverage payload disagrees"):
            merge_stores(source, target)

    def test_two_copies_that_read_the_same_range_at_different_times_still_merge(
        self,
        tmp_path: Path,
    ) -> None:
        source = _store(tmp_path / "source.sqlite")
        target = _store(tmp_path / "target.sqlite")
        for path, fetched in (
            (source, "2026-07-31T00:00:00+00:00"),
            (target, "2026-08-01T00:00:00+00:00"),
        ):
            _add_coverage(
                path, "edinet_documents", "2026-05-01", record_count=3, fetched_at_utc=fetched
            )

        merge_stores(source, target)

        assert _coverage(target, "edinet_documents")[0][3] == "2026-08-01T00:00:00+00:00"


class TestFinancialSummaryCoverage:
    def test_a_source_only_range_is_recounted_against_the_target(self, tmp_path: Path) -> None:
        """The carried claim states a count proved against a store that is not this one."""

        source = _store(tmp_path / "source.sqlite")
        target = _store(tmp_path / "target.sqlite")
        _add_fin_summary(target, ticker="1301")
        _add_coverage(
            source,
            "jquants_fin_summaries",
            _FIN_RANGE,
            record_count=9,
            min_date="2020-01-01",
            max_date="2020-12-31",
        )

        merge_stores(source, target)

        assert _coverage(target, "jquants_fin_summaries") == [
            (_FIN_RANGE, 1, "ok", "2026-07-31T00:00:00+00:00")
        ]

    def test_an_unclassified_status_and_error_state_is_refused(self, tmp_path: Path) -> None:
        source = _store(tmp_path / "source.sqlite")
        target = _store(tmp_path / "target.sqlite")
        _add_coverage(
            source,
            "jquants_fin_summaries",
            _FIN_RANGE,
            record_count=0,
            min_date="2020-01-01",
            max_date="2020-12-31",
            status="partial",
            error=None,
        )

        with pytest.raises(MergeError, match="source_coverage payload disagrees"):
            merge_stores(source, target)

    def test_a_failure_claim_keeps_its_provenance_and_is_not_recounted(
        self, tmp_path: Path
    ) -> None:
        source = _store(tmp_path / "source.sqlite")
        target = _store(tmp_path / "target.sqlite")
        _add_coverage(
            source,
            "jquants_fin_summaries",
            _FIN_RANGE,
            record_count=0,
            min_date="2020-01-01",
            max_date="2020-12-31",
            status="failed",
            error="provider 500",
        )

        merge_stores(source, target)

        assert _coverage(target, "jquants_fin_summaries") == [
            (_FIN_RANGE, 0, "failed", "2026-07-31T00:00:00+00:00")
        ]

    def test_a_target_claim_that_outruns_its_rows_is_refused(self, tmp_path: Path) -> None:
        source = _store(tmp_path / "source.sqlite")
        target = _store(tmp_path / "target.sqlite")
        _add_fin_summary(target, ticker="1301")
        _add_coverage(
            target,
            "jquants_fin_summaries",
            _FIN_RANGE,
            record_count=5,
            min_date="2020-01-01",
            max_date="2020-12-31",
        )

        with pytest.raises(MergeError, match="coverage count does not match stored rows"):
            merge_stores(source, target)


class TestShortSaleCoverage:
    def test_the_newer_complete_claim_wins(self, tmp_path: Path) -> None:
        source = _store(tmp_path / "source.sqlite")
        target = _store(tmp_path / "target.sqlite")
        _store_short_snapshot(
            source, [_short_record("A", 0.5)], fetched_at_utc="2026-08-02T00:00:00+00:00"
        )
        _store_short_snapshot(
            target, [_short_record("A", 0.5)], fetched_at_utc="2026-08-01T00:00:00+00:00"
        )

        merge_stores(source, target)

        assert _coverage(target, "jquants_short_sale_reports") == [
            (
                "get_mkt_short_sale_report:2026-08-01..2026-08-01",
                1,
                "ok",
                "2026-08-02T00:00:00+00:00",
            )
        ]

    def test_a_complete_claim_beats_a_newer_partial_one(self, tmp_path: Path) -> None:
        source = _store(tmp_path / "source.sqlite")
        target = _store(tmp_path / "target.sqlite")
        _store_short_snapshot(
            source,
            [_short_record("A", 0.5)],
            fetched_at_utc="2026-08-02T00:00:00+00:00",
            status="partial",
        )
        _store_short_snapshot(
            target, [_short_record("A", 0.5)], fetched_at_utc="2026-08-01T00:00:00+00:00"
        )

        merge_stores(source, target)

        assert _coverage(target, "jquants_short_sale_reports")[0][2] == "ok"

    def test_a_date_the_release_filled_before_its_claim_arrived_still_merges(
        self,
        tmp_path: Path,
    ) -> None:
        """Rows come from the release and claims come from this merge, one step apart.

        A date the cloud fetched reaches the target's table when it hydrates and the
        target's ledger only here, so requiring the target to already claim its own rows
        would refuse the ordinary sequence.
        """

        source = _store(tmp_path / "source.sqlite")
        target = _store(tmp_path / "target.sqlite")
        _store_short_snapshot(
            source, [_short_record("A", 0.5)], fetched_at_utc="2026-08-02T00:00:00+00:00"
        )
        _store_short_snapshot(
            target, [_short_record("A", 0.5)], fetched_at_utc="2026-08-01T00:00:00+00:00"
        )
        conn = open_connection(target)
        try:
            conn.execute("DELETE FROM source_coverage WHERE source = 'jquants_short_sale_reports'")
            conn.commit()
        finally:
            conn.close()

        merge_stores(source, target)

        assert _coverage(target, "jquants_short_sale_reports")[0][1] == 1

    def test_a_row_no_copy_claims_stops_the_merge(self, tmp_path: Path) -> None:
        source = _store(tmp_path / "source.sqlite")
        target = _store(tmp_path / "target.sqlite")
        _store_short_snapshot(
            target, [_short_record("A", 0.5)], fetched_at_utc="2026-08-01T00:00:00+00:00"
        )
        for path in (source, target):
            conn = open_connection(path)
            try:
                conn.execute(
                    "DELETE FROM source_coverage WHERE source = 'jquants_short_sale_reports'"
                )
                conn.commit()
            finally:
                conn.close()

        with pytest.raises(MergeError, match="rows lack coverage"):
            merge_stores(source, target)

    def test_the_claim_is_recounted_against_the_rows_the_release_filled(
        self,
        tmp_path: Path,
    ) -> None:
        source = _store(tmp_path / "source.sqlite")
        target = _store(tmp_path / "target.sqlite")
        _store_short_snapshot(
            source,
            [_short_record("A", 0.5), _short_record("B", 0.6)],
            fetched_at_utc="2026-08-02T00:00:00+00:00",
        )
        _store_short_snapshot(
            target, [_short_record("A", 0.5)], fetched_at_utc="2026-08-01T00:00:00+00:00"
        )

        merge_stores(source, target)

        assert _coverage(target, "jquants_short_sale_reports")[0][1] == 1


class TestDerivedTables:
    def test_a_retracted_exit_value_is_not_resurrected_from_the_published_copy(
        self,
        tmp_path: Path,
    ) -> None:
        """A later derivation that could not establish the price has to win.

        Reinserting the published row would put a price into the calibration forward
        that the current rules refuse to establish.
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

    def test_a_corrected_exit_value_does_not_block_the_publish(self, tmp_path: Path) -> None:
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

    def test_a_reworded_delisting_row_does_not_refuse_the_publish(self, tmp_path: Path) -> None:
        """JPX rewords its archive, and only the operator ever writes this table."""

        published = _store(tmp_path / "published.sqlite")
        local = _store(tmp_path / "local.sqlite")
        for path, reason in (
            (published, "株式の併合"),
            (local, "ＭＢＯ（公開買付け、株式併合）"),
        ):
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

    def test_every_derived_table_is_reported_as_kept_whole(self, tmp_path: Path) -> None:
        source = _store(tmp_path / "source.sqlite")
        target = _store(tmp_path / "target.sqlite")
        _add_exit_value(source, "2000", 1060.0)

        report = merge_stores(source, target)

        derived = {item.table: item for item in report.tables if item.table in DERIVED_KEYS}
        assert set(derived) == set(DERIVED_KEYS)
        assert all(item.inserted == 0 for item in derived.values())
        assert derived["tender_offer_exit_values"].skipped == 1


class TestStoreContract:
    def test_a_store_on_another_schema_is_refused(self, tmp_path: Path) -> None:
        source = _store(tmp_path / "source.sqlite")
        target = _store(tmp_path / "target.sqlite")
        conn = sqlite3.connect(source)
        try:
            conn.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION - 1}")
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(MergeError, match="market store schema is"):
            merge_stores(source, target)

    def test_a_store_whose_table_shape_drifted_is_refused(self, tmp_path: Path) -> None:
        source = _store(tmp_path / "source.sqlite")
        target = _store(tmp_path / "target.sqlite")
        conn = sqlite3.connect(source)
        try:
            conn.execute("DROP TABLE jpx_delistings")
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(MergeError, match="schema contract is invalid"):
            merge_stores(source, target)

    def test_a_missing_store_is_named_rather_than_crashing(self, tmp_path: Path) -> None:
        target = _store(tmp_path / "target.sqlite")

        with pytest.raises(MergeError, match="market store does not exist"):
            merge_stores(tmp_path / "absent.sqlite", target)


class TestCommand:
    def test_the_command_reports_a_refusal_without_a_traceback(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = _store(tmp_path / "target.sqlite")

        exit_code = main(["--source", str(tmp_path / "absent.sqlite"), "--target", str(target)])

        assert exit_code == 1
        assert "market store does not exist" in capsys.readouterr().err

    def test_the_command_prints_what_it_merged(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        source = _store(tmp_path / "source.sqlite")
        target = _store(tmp_path / "target.sqlite")
        _add_coverage(source, "edinet_documents", "2026-05-01", record_count=3)

        exit_code = main(["--source", str(source), "--target", str(target)])

        assert exit_code == 0
        out = capsys.readouterr().out
        assert "merged 1 rows" in out
        for table in ALL_TABLES:
            assert table in out
