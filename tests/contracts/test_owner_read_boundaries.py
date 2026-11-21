"""Read boundaries distinguish unpublished state from broken queries and saved records."""

from __future__ import annotations

import gzip
import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from tests.helpers.model_generation import empty_selection, generation, publish_generation
from tests.helpers.proposal import PUBLISHED, backend_case, read_saved_proposal, reviewed_proposal

from baibai_engine.data_collection.receipts.native_sources.store import (
    initialize_database as initialize_indicator_database,
)
from baibai_engine.investment_judgment.stock_assessment.store import load_reviewed_assessment
from baibai_engine.investment_judgment.trade_proposal.preview import preview_current_terms
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.machine_analysis.generation.runners.market_reading import reading_snapshot
from baibai_engine.machine_analysis.observations.economic_indicators.service import (
    IndicatorsService,
)
from baibai_engine.machine_analysis.observations.prices.calendar_context import (
    observation_calendar_context,
)
from baibai_engine.machine_analysis.screening.selection_query import read_case_publication
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.storage.sqlite.application.read import (
    connect_read_only as connect_application_read_only,
)
from baibai_engine.storage.sqlite.application.schema import APPLICATION_SCHEMA_VERSION
from baibai_engine.storage.sqlite.application.write import initialize_database
from baibai_engine.storage.sqlite.machine_analysis import (
    RUN_STORE_SCHEMA_VERSION,
    initialize_run_store,
)
from baibai_engine.storage.sqlite.read import read_rows
from baibai_engine.storage.sqlite.source_cache.read import (
    connect_read_only as connect_market_read_only,
)


@pytest.mark.parametrize("store", ["absent-file", "empty-file"])
def test_unwritten_stores_return_absence_without_initializing(tmp_path: Path, store: str) -> None:
    path = tmp_path / f"{store}.sqlite"
    if store == "empty-file":
        sqlite3.connect(path).close()
    original = path.read_bytes() if path.exists() else None
    assert read_rows(path, "SELECT payload FROM example_payload") == []
    assert reading_snapshot(path, asof=date(2026, 7, 29)) is None
    assert (path.read_bytes() if path.exists() else None) == original


def test_observation_calendar_resolver_reports_unreadable_store_not_a_closed_day(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty.sqlite"
    empty.touch()
    with (
        closing(connect_market_read_only(empty)) as connection,
        pytest.raises(sqlite3.OperationalError, match="no such table: jquants_market_calendar"),
    ):
        observation_calendar_context(connection, date(2026, 7, 29))
    assert empty.stat().st_size == 0


def test_screening_read_helpers_accept_the_current_run_store(tmp_path: Path) -> None:
    current = tmp_path / "runs.sqlite"
    initialize_run_store(current)

    assert (
        read_case_publication(
            path=current, selection_id="absent", known_at=datetime(2027, 1, 1, tzinfo=UTC)
        )
        is None
    )


def test_screening_reader_rejects_an_obsolete_run_store(tmp_path: Path) -> None:
    obsolete = tmp_path / "runs.sqlite"
    initialize_run_store(obsolete)
    with closing(sqlite3.connect(obsolete)) as connection, connection:
        connection.execute(f"PRAGMA user_version = {RUN_STORE_SCHEMA_VERSION - 1}")

    with pytest.raises(
        RuntimeError,
        match=rf"found {RUN_STORE_SCHEMA_VERSION - 1}, expected {RUN_STORE_SCHEMA_VERSION}",
    ):
        read_case_publication(
            path=obsolete, selection_id="absent", known_at=datetime(2027, 1, 1, tzinfo=UTC)
        )


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
    store = tmp_path / "old-application.sqlite"
    initialize_database(store)
    with closing(sqlite3.connect(store)) as connection, connection:
        connection.execute(f"PRAGMA user_version = {APPLICATION_SCHEMA_VERSION - 1}")

    with (
        pytest.raises(RuntimeError, match="obsolete application database schema"),
        closing(connect_application_read_only(store)),
    ):
        pass


@pytest.mark.parametrize("kind", ["assessment", "proposal"])
@pytest.mark.parametrize("version", [0, 99, True, 1.0, "1"])
def test_research_readers_reject_noncurrent_or_nonstrict_payload_versions(
    tmp_path: Path, kind: str, version: object
) -> None:
    case = backend_case(tmp_path)
    proposal = reviewed_proposal(case)
    proposal = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(proposal)
    table = "stock_assessment" if kind == "assessment" else "investment_proposal"
    raw = case.pair.document.model_dump(mode="json") if kind == "assessment" else proposal.payload()
    raw["schema_version"] = version
    with closing(sqlite3.connect(case.app)) as connection, connection:
        connection.execute(f"DROP TRIGGER {table}_immutable")
        connection.execute(f"UPDATE {table} SET payload=?", (json.dumps(raw),))
    original = case.app.read_bytes()
    if kind == "assessment":
        with (
            closing(connect_application_read_only(case.app)) as connection,
            pytest.raises(ValueError, match="schema_version"),
        ):
            load_reviewed_assessment(connection, case.pair.assessment_id)
    else:
        for reader in (
            lambda: read_saved_proposal(db_path=case.app, proposal_id=proposal.proposal_id),
            lambda: preview_current_terms(
                db_path=case.app,
                proposal_id=proposal.proposal_id,
                sqlite_path=case.market,
                now=PUBLISHED,
            ),
        ):
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
    with (
        closing(connect_application_read_only(case.app)) as connection,
        pytest.raises(ValueError, match="要再評価"),
    ):
        load_reviewed_assessment(connection, case.pair.assessment_id)
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
        lambda: reading_snapshot(path, asof=date(2026, 7, 29)),
        lambda: IndicatorsService(path).get_range(series_id, start=date.min, end=date(2026, 7, 29)),
    ]
    assert isinstance(readers[0](), dict)
    assert readers[1]().observations == ()
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


def test_damaged_fixed_selection_is_not_an_absent_id(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite"
    fixed = generation()
    publish_generation(fixed, path=path)
    selection_id = "fixed-selection"
    ScreeningSelectionStore(path).publish(selection_id, empty_selection(fixed.generation_id))
    known_at = max(datetime.now(UTC), fixed.evaluation_time)
    assert (
        read_case_publication(path=path, selection_id=selection_id, known_at=known_at) is not None
    )
    assert read_case_publication(path=path, selection_id="absent", known_at=known_at) is None
    with closing(sqlite3.connect(path)) as connection, connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        connection.execute(
            "UPDATE screening_selection SET payload_gzip = ? WHERE selection_id = ?",
            (b"not gzip", selection_id),
        )
    original = path.read_bytes()
    with pytest.raises(gzip.BadGzipFile):
        read_case_publication(path=path, selection_id=selection_id, known_at=known_at)
    assert path.read_bytes() == original
    with closing(sqlite3.connect(path)) as connection, connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == version
