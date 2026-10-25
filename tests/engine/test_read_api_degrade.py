"""Read boundaries distinguish unpublished state from broken queries and saved records."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest
from tests.helpers.proposal import PUBLISHED, backend_case, reviewed_proposal

import baibai_engine.read_api as read_api
from baibai_engine.data_collection.economic.store import (
    initialize_database as initialize_indicator_database,
)
from baibai_engine.investment_judgment.trade_proposal_service import TradeProposalService
from baibai_engine.machine_analysis.screening.rule_config import load_screening_rules
from baibai_engine.machine_analysis.screening.run_store import initialize_run_store
from baibai_engine.read_api.sqlite import read_rows
from baibai_engine.storage.application.schema import APPLICATION_SCHEMA_VERSION
from baibai_engine.storage.application.write import initialize_database
from baibai_engine.storage.machine_analysis import RUN_STORE_SCHEMA_VERSION
from baibai_engine.storage.observations.schema import _SCHEMA_SQL as MARKET_SCHEMA_SQL
from baibai_engine.storage.observations.schema import SQLITE_SCHEMA_VERSION


def _research_reads(path: Path):
    assessment_id = "stock-assessment-00000000-0000-4000-8000-000000000001"
    proposal_id = "investment-proposal-00000000-0000-4000-8000-000000000001"
    return (
        (lambda: read_api.list_stock_assessment_publications(path), []),
        (lambda: read_api.stock_assessment_publication(path, assessment_id=assessment_id), None),
        (lambda: read_api.list_assessment_review_publications(path), []),
        (lambda: read_api.reviewed_assessment_projection(path, assessment_id=assessment_id), {}),
        (lambda: read_api.list_investment_proposal_payloads(path), []),
        (lambda: read_api.investment_proposal_payload(path, proposal_id=proposal_id), None),
        (
            lambda: read_api.preview_current_proposal_terms(
                path, proposal_id=proposal_id, sqlite_path=path, now=PUBLISHED
            ),
            None,
        ),
    )


@pytest.mark.parametrize("store", ["absent-file", "empty-file"])
def test_unwritten_stores_return_absence_without_initializing(tmp_path: Path, store: str) -> None:
    path = tmp_path / f"{store}.sqlite"
    if store == "empty-file":
        sqlite3.connect(path).close()
    original = path.read_bytes() if path.exists() else None
    assert read_rows(path, "SELECT payload FROM example_payload") == []
    assert read_api.list_operation_sessions(path) == []
    assert read_api.screening_run_asof_dates(path) == []
    assert read_api.screening_run_payload(path) is None
    assert read_api.reading_snapshot(path, asof=date(2026, 7, 29)) is None
    assert read_api.indicator_series(path, series_id="jp.cpi.headline") is None
    assert read_api.next_earnings_dates(path, ["2331"], asof=date(2026, 7, 29)) == {}
    for reader, expected in _research_reads(path):
        assert reader() == expected
    assert (path.read_bytes() if path.exists() else None) == original


def test_the_daily_batch_gates_report_an_unwritten_store_instead_of_degrading(
    tmp_path: Path,
) -> None:
    """Skipping a run because the calendar was unreadable must stay impossible."""

    empty = tmp_path / "empty.sqlite"
    sqlite3.connect(empty).close()

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        read_api.market_calendar_business_day(empty, date(2026, 7, 29))
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        read_api.previous_run_revision_id(empty, date(2026, 7, 29))


def test_screening_read_helpers_accept_the_current_run_store(tmp_path: Path) -> None:
    current = tmp_path / "runs.sqlite"
    initialize_run_store(current)

    assert read_api.screening_run_asof_dates(current) == []
    assert read_api.previous_run_revision_id(current, date(2026, 7, 29)) is None


@pytest.mark.parametrize(
    "reader",
    [
        lambda path: read_api.screening_run_asof_dates(path),
        lambda path: read_api.previous_run_revision_id(path, date(2026, 7, 29)),
    ],
)
def test_screening_read_helpers_reject_an_obsolete_run_store(
    tmp_path: Path, reader: Callable[[Path], object]
) -> None:
    obsolete = tmp_path / "runs.sqlite"
    initialize_run_store(obsolete)
    with closing(sqlite3.connect(obsolete)) as connection, connection:
        connection.execute(f"PRAGMA user_version = {RUN_STORE_SCHEMA_VERSION - 1}")

    with pytest.raises(
        RuntimeError,
        match=rf"found {RUN_STORE_SCHEMA_VERSION - 1}, expected {RUN_STORE_SCHEMA_VERSION}",
    ):
        reader(obsolete)


def test_read_rows_still_raises_for_a_broken_query(tmp_path: Path) -> None:
    store = tmp_path / "store.sqlite"
    with closing(sqlite3.connect(store)) as connection, connection:
        connection.execute("CREATE TABLE example_payload (payload TEXT)")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        read_rows(store, "SELECT payload FROM absent_table")
    with pytest.raises(sqlite3.OperationalError, match="no such column"):
        read_rows(store, "SELECT absent_column FROM example_payload")
    with pytest.raises(sqlite3.OperationalError, match="syntax error"):
        read_rows(store, "SELEKT payload FROM example_payload")


def test_application_reader_rejects_an_obsolete_store_before_querying(tmp_path: Path) -> None:
    store = tmp_path / "application-v16.sqlite"
    initialize_database(store)
    with closing(sqlite3.connect(store)) as connection, connection:
        connection.execute(f"PRAGMA user_version = {APPLICATION_SCHEMA_VERSION - 1}")

    with pytest.raises(RuntimeError, match="obsolete application database schema"):
        read_api.list_operation_sessions(store)


def test_all_research_readers_reject_an_obsolete_application_store(tmp_path: Path) -> None:
    store = tmp_path / "obsolete.sqlite"
    initialize_database(store)
    with closing(sqlite3.connect(store)) as connection, connection:
        connection.execute(f"PRAGMA user_version = {APPLICATION_SCHEMA_VERSION - 1}")
    original = store.read_bytes()
    for reader, _ in _research_reads(store):
        with pytest.raises(RuntimeError, match="obsolete application database schema"):
            reader()
    assert store.read_bytes() == original


def test_market_reader_rejects_an_obsolete_store_instead_of_degrading(tmp_path: Path) -> None:
    store = tmp_path / "market-v24.sqlite"
    with closing(sqlite3.connect(store)) as connection, connection:
        connection.executescript(MARKET_SCHEMA_SQL)
        connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION - 1}")

    with pytest.raises(RuntimeError, match="obsolete market SQLite schema"):
        read_api.next_earnings_dates(store, ["2331"], asof=date(2026, 8, 30))


@pytest.mark.parametrize("kind", ["assessment", "proposal"])
@pytest.mark.parametrize("version", [0, 99, True, 1.0, "1"])
def test_research_readers_reject_noncurrent_or_nonstrict_payload_versions(
    tmp_path: Path, kind: str, version: object
) -> None:
    case = backend_case(tmp_path)
    proposal = reviewed_proposal(case)
    TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
        proposal
    )
    table = "stock_assessment" if kind == "assessment" else "investment_proposal"
    raw = case.pair.document.model_dump(mode="json") if kind == "assessment" else proposal.payload()
    raw["schema_version"] = version
    with closing(sqlite3.connect(case.app)) as connection, connection:
        connection.execute(f"DROP TRIGGER {table}_immutable")
        connection.execute(f"UPDATE {table} SET payload=?", (json.dumps(raw),))
    original = case.app.read_bytes()
    readers = (
        (
            lambda: read_api.list_stock_assessment_publications(case.app),
            lambda: read_api.stock_assessment_publication(
                case.app, assessment_id=case.pair.assessment_id
            ),
            lambda: read_api.list_assessment_review_publications(case.app),
        )
        if kind == "assessment"
        else (
            lambda: read_api.list_investment_proposal_payloads(case.app),
            lambda: read_api.investment_proposal_payload(
                case.app, proposal_id=proposal.proposal_id
            ),
            lambda: read_api.preview_current_proposal_terms(
                case.app, proposal_id=proposal.proposal_id, sqlite_path=case.market, now=PUBLISHED
            ),
        )
    )
    for reader in readers:
        with pytest.raises(ValueError, match="schema_version"):
            reader()
    assert case.app.read_bytes() == original


@pytest.mark.parametrize(
    "mutation", ["corrupt-payload", "core-hash", "review-hash", "missing-review"]
)
def test_assessment_publications_never_degrade_a_corrupt_saved_pair(
    tmp_path: Path, mutation: str
) -> None:
    case = backend_case(tmp_path)
    with closing(sqlite3.connect(case.app)) as connection, connection:
        if mutation == "missing-review":
            connection.execute("DELETE FROM stock_assessment_review")
        elif mutation == "review-hash":
            raw = case.pair.review.model_dump(mode="json")
            raw["reviewed_assessment_sha256"] = "0" * 64
            connection.execute("DROP TRIGGER stock_assessment_review_immutable")
            connection.execute("UPDATE stock_assessment_review SET payload=?", (json.dumps(raw),))
        else:
            connection.execute("DROP TRIGGER stock_assessment_immutable")
            if mutation == "corrupt-payload":
                connection.execute("UPDATE stock_assessment SET payload='{}'")
            else:
                connection.execute("UPDATE stock_assessment SET core_sha256=?", ("0" * 64,))
    original = case.app.read_bytes()
    for reader in (
        lambda: read_api.list_stock_assessment_publications(case.app),
        lambda: read_api.stock_assessment_publication(
            case.app, assessment_id=case.pair.assessment_id
        ),
        lambda: read_api.reviewed_assessment_projection(
            case.app, assessment_id=case.pair.assessment_id
        ),
    ):
        with pytest.raises(ValueError, match="要再評価"):
            reader()
    assert case.app.read_bytes() == original


@pytest.mark.parametrize("storage", ["document", "native"])
@pytest.mark.parametrize("mutation", ["missing-table", "missing-column"])
def test_indicator_partial_store_is_not_unpublished(
    tmp_path: Path, mutation: str, storage: str
) -> None:
    path = tmp_path / "indicators.sqlite"
    initialize_indicator_database(path).close()
    # Break the storage boundary this series actually reads. Bankruptcies read
    # original JSON documents; CPI reads native e-Stat records.
    table = "economic_source_documents" if storage == "document" else "economic_source_records"
    series_id = "jp.bankruptcies" if storage == "document" else "jp.cpi.headline"
    readers = [
        lambda: read_api.reading_snapshot(path, asof=date(2026, 7, 29)),
        lambda: read_api.indicator_series(path, series_id=series_id),
    ]
    assert all(isinstance(reader(), dict) for reader in readers)
    assert read_api.indicator_series(path, series_id="unknown") is None
    with closing(sqlite3.connect(path)) as connection, connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if storage == "document":
            connection.execute("DROP TABLE economic_source_documents")
            if mutation == "missing-column":
                connection.execute("CREATE TABLE economic_source_documents (source_id TEXT)")
        else:
            connection.execute("DROP TABLE economic_source_records")
            if mutation == "missing-column":
                connection.execute("CREATE TABLE economic_source_records (source_id TEXT)")
    original = path.read_bytes()
    for reader in readers:
        with pytest.raises(
            sqlite3.OperationalError,
            match=f"no such table: {table}" if mutation == "missing-table" else "no such column",
        ):
            reader()
    assert path.read_bytes() == original
    with closing(sqlite3.connect(path)) as connection, connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == version


def test_run_payload_partial_store_is_not_an_absent_id(tmp_path: Path) -> None:
    from tests.helpers.screening_run import screening_run_payload, stock_analysis

    from baibai_engine.machine_analysis.screening.run_store import ScreeningRunStore

    path = tmp_path / "runs.sqlite"
    ScreeningRunStore(path).publish_run(
        screening_run_payload(stock_analyses=(stock_analysis(),)),
        run_revision_id="run-test",
        rules=load_screening_rules(),
    )
    assert isinstance(read_api.screening_run_payload(path, run_revision_id="run-test"), dict)
    assert read_api.screening_run_payload(path, run_revision_id="absent") is None
    with closing(sqlite3.connect(path)) as connection, connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        connection.execute("DROP TABLE stock_analysis")
    original = path.read_bytes()
    with pytest.raises(sqlite3.OperationalError, match="no such table: stock_analysis"):
        read_api.screening_run_payload(path, run_revision_id="run-test")
    assert path.read_bytes() == original
    with closing(sqlite3.connect(path)) as connection, connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == version
