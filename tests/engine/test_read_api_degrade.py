"""One degrade policy for every read_api query: an unwritten store reads as empty."""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from tests.helpers.proposal_v1 import PUBLISHED, backend_case, reviewed_proposal

import baibai_engine.read_api as read_api
from baibai_engine.market_context.indicators.db import (
    initialize_database as initialize_indicator_database,
)
from baibai_engine.read_api.sqlite import read_rows
from baibai_engine.research.proposal_service import InvestmentProposalService
from baibai_engine.screening.rule_config import load_screening_rules
from baibai_engine.screening.run_store import initialize_run_store
from baibai_engine.screening.run_store.schema import RUN_STORE_SCHEMA_VERSION
from baibai_engine.storage.application.schema import APPLICATION_SCHEMA_VERSION
from baibai_engine.storage.application.write import initialize_database
from baibai_engine.storage.observations.schema import _SCHEMA_SQL as MARKET_SCHEMA_SQL
from baibai_engine.storage.observations.schema import SQLITE_SCHEMA_VERSION

# These parse Git-managed YAML paths, not runtime stores.
_NOT_STORE_READERS = frozenset(
    {
        "validate_application_store_schema",
        "validate_reading_rules",
    }
)
# A caller asking for one identity that is absent is told so; that is not a degrade.
_RAISES_FOR_UNKNOWN_ID = frozenset({"assessment_payload"})
# The rule is "readers that answer what has been published degrade; readers that gate a
# write raise". These two gate the daily batch, which must stop and name the fault
# rather than skip a run that looked like a holiday.
_WRITE_GATES = frozenset({"market_calendar_business_day", "previous_run_revision_id"})
# Public callables the sweep cannot drive: they read no store, or they take a value this
# file would have to invent. Anything not listed here must be reachable by the sweep.
_DECLARED_OUTSIDE_THE_SWEEP = frozenset(
    {
        *_NOT_STORE_READERS,
        "is_unwritten_store",  # requires a query exception as well as a store
        "registered_indicator",  # reads the bundled definitions, not a store
        "indicator_series_names",  # reads the bundled definitions, not a store
        "read_rows",  # takes the SQL to run, which this file would have to invent
        "machine_valuation_from_analysis",  # pure saved-value projection; owned by valuation tests
        "interpreted_next_earnings_status",  # pure legacy-status projection; owned by earnings tests
        "anchor_gap_pct",  # pure machine-anchor presentation; owned by valuation tests
        "candidate_er_bands",  # pure band selection over a validated calibration horizon
        "candidate_rows",  # pure same-run filter; owned by candidate discovery tests
        "reinvestment_coordinates",  # pure saved-metric projection; owned by valuation tests
        "load_er_calibration_context",  # reads Git-managed calibration YAML, not SQLite
        "replay_events_through",  # pure event replay; owned by ledger tests
        "summarize_portfolio",  # pure capital projection; owned by ledger tests
        "current_portfolio",  # composes an explicit ledger with market input; covered by current-capital tests
        "reject_noncanonical_store_paths",  # startup layout guard, not a query
        "repository_root_error",  # inspects a directory layout, not a store
        "safe_load",  # a YAML helper re-exported for callers
        "screening_calibration_method_identity",  # reads Git-managed method config
        "prepare_market_context_inputs",  # requires an explicit shared master; covered by prepared-context tests
    }
)

_ARGUMENTS: dict[str, object] = {
    "known_at": datetime(2026, 7, 29, tzinfo=UTC),
    "cutoff": datetime(2026, 7, 29, tzinfo=UTC),
    "evaluated_at": datetime(2026, 7, 29, tzinfo=UTC),
    "after": date(2026, 7, 28),
    "as_of": date(2026, 7, 29),
    "asof": date(2026, 7, 29),
    "asof_date": date(2026, 7, 29),
    "self_range_window_bars": 750,
    "assessment_id": "stock-assessment-00000000-0000-4000-8000-000000000001",
    "proposal_id": "investment-proposal-00000000-0000-4000-8000-000000000001",
    "now": datetime(2026, 7, 29, tzinfo=UTC),
    "context_id": "macro-context-2026-07-29-a",
    "day": date(2026, 7, 29),
    "end": date(2026, 7, 29),
    "operation_id": "op-20260729-a-1",
    "context_asof": date(2026, 7, 28),
    "requested_asof": date(2026, 7, 29),
    "run_revision_id": "run-revision-20260729",
    "series_id": "jp.cpi",
    "since": date(2026, 7, 28),
    "start": date(2026, 7, 1),
    "ticker": "1234",
    "tickers": ["1234"],
}


_STORE_PARAMETERS = ("path", "db_path", "indicators_db_path", "sqlite_path")
_RESEARCH_READERS = frozenset(
    {
        "list_stock_assessment_publications",
        "stock_assessment_publication",
        "list_assessment_review_publications",
        "reviewed_assessment_projection",
        "list_investment_proposal_payloads",
        "investment_proposal_payload",
        "preview_current_proposal_terms",
    }
)


def _published_callables() -> dict[str, object]:
    """Everything read_api publishes, from the package facade and from its modules alike.

    Following only the facade would tie this sweep's reach to how wide the facade happens
    to be: pruning one re-export whose consumers import the module directly would drop
    that query from the degrade policy without changing the query at all. The modules are
    where the queries live, so the sweep follows both and takes the union.
    """

    published: dict[str, object] = {name: getattr(read_api, name) for name in read_api.__all__}
    package = Path(read_api.__file__).parent
    for module_path in sorted(package.glob("*.py")):
        if module_path.name == "__init__.py":
            continue
        module = importlib.import_module(f"{read_api.__name__}.{module_path.stem}")
        for name in getattr(module, "__all__", ()):
            published.setdefault(name, getattr(module, name))
    return published


def _classify_readers() -> tuple[list[tuple[str, object, dict[str, object]]], set[str]]:
    """Split read_api's public callables into "this sweep drives it" and "it does not".

    The uncovered set is returned rather than dropped so a reader whose arguments this
    file cannot build has to be declared, instead of leaving the policy quietly.
    """

    readers: list[tuple[str, object, dict[str, object]]] = []
    uncovered: set[str] = set()
    for name, function in sorted(_published_callables().items()):
        if not callable(function) or inspect.isclass(function):
            continue
        if name in _NOT_STORE_READERS:
            uncovered.add(name)
            continue
        try:
            signature = inspect.signature(function)
        except (TypeError, ValueError):  # pragma: no cover - builtins have no signature
            uncovered.add(name)
            continue
        arguments: dict[str, object] = {}
        for parameter_name, parameter in signature.parameters.items():
            if parameter.default is not inspect.Parameter.empty:
                continue
            if parameter_name in _STORE_PARAMETERS:
                arguments[parameter_name] = None  # a store path, filled in per test
            elif parameter_name in _ARGUMENTS:
                arguments[parameter_name] = _ARGUMENTS[parameter_name]
            else:
                uncovered.add(name)
                break
        else:
            if any(key in arguments for key in _STORE_PARAMETERS):
                readers.append((name, function, arguments))
            else:
                uncovered.add(name)
    return readers, uncovered


def _store_readers() -> list[tuple[str, object, dict[str, object]]]:
    readers, _ = _classify_readers()
    return [item for item in readers if item[0] not in _WRITE_GATES]


def test_every_public_reader_is_swept_or_declared() -> None:
    """A reader the sweep cannot drive has to say so here, or the policy below stops
    covering it without anyone noticing."""

    _, uncovered = _classify_readers()

    assert uncovered - _DECLARED_OUTSIDE_THE_SWEEP == set()
    assert _DECLARED_OUTSIDE_THE_SWEEP - uncovered == set()


@pytest.mark.parametrize("store", ["absent-file", "empty-file"])
def test_no_store_reader_raises_when_the_writer_has_not_run(tmp_path: Path, store: str) -> None:
    """Both shapes of "not written here" must read the same: an absent file and a
    file the migrations never touched. Only exercising one lets the other regress."""

    path = tmp_path / f"{store}.sqlite"
    if store == "empty-file":
        sqlite3.connect(path).close()

    leaked: list[str] = []
    for name, function, arguments in _store_readers():
        call = {key: (path if value is None else value) for key, value in arguments.items()}
        try:
            function(**call)
        except sqlite3.OperationalError as error:
            leaked.append(f"{name}: {error}")
        except ValueError:
            assert name in _RAISES_FOR_UNKNOWN_ID, f"{name} raised for an unwritten store"

    assert leaked == []


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
    with sqlite3.connect(obsolete) as connection:
        connection.execute(f"PRAGMA user_version = {RUN_STORE_SCHEMA_VERSION - 1}")

    with pytest.raises(
        RuntimeError,
        match=rf"found {RUN_STORE_SCHEMA_VERSION - 1}, expected {RUN_STORE_SCHEMA_VERSION}",
    ):
        reader(obsolete)


def _tables_named_in_read_api_sql() -> set[str]:
    """Every table read_api's own SQL names, taken from the source rather than a list."""

    package = Path(read_api.__file__).parent
    tables: set[str] = set()
    for module in sorted(package.glob("*.py")):
        for node in ast.walk(ast.parse(module.read_text())):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if not re.search(r"\bSELECT\b", node.value, re.IGNORECASE):
                continue
            tables.update(
                match.group(1)
                for match in re.finditer(
                    r"\b(?:FROM|JOIN)\s+([a-z_][a-z0-9_]*)", node.value, re.IGNORECASE
                )
            )
    return tables


def test_every_table_read_api_reads_exists_in_a_migrated_store(tmp_path: Path) -> None:
    """The degrade rule reads a missing table as "not written yet", so a table renamed
    by a migration without its reader would turn into a permanently empty view instead
    of an error. Pin the reader-to-schema correspondence the degrade cannot check."""

    def table_names(path: Path) -> set[str]:
        with closing(sqlite3.connect(path)) as connection:
            rows = connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
            return {str(row[0]) for row in rows}

    application = tmp_path / "app.sqlite"
    initialize_database(application)
    runs = tmp_path / "runs.sqlite"
    initialize_run_store(runs)
    market = tmp_path / "market.sqlite"
    with closing(sqlite3.connect(market)) as connection:
        connection.executescript(MARKET_SCHEMA_SQL)
    market_context = tmp_path / "indicators.sqlite"
    initialize_indicator_database(market_context).close()

    # Every schema is built here rather than listed, so renaming a table on either
    # side of the read is what the assertion sees.
    known = {"sqlite_schema"}
    for store in (application, runs, market, market_context):
        known |= table_names(store)

    unknown = sorted(_tables_named_in_read_api_sql() - known)

    assert unknown == [], (
        f"read_api reads tables no migrated store defines: {unknown}. "
        "A renamed table would otherwise degrade to an empty view forever."
    )


def test_read_rows_still_raises_for_a_broken_query(tmp_path: Path) -> None:
    store = tmp_path / "store.sqlite"
    with sqlite3.connect(store) as connection:
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
    with sqlite3.connect(store) as connection:
        connection.execute(f"PRAGMA user_version = {APPLICATION_SCHEMA_VERSION - 1}")

    with pytest.raises(RuntimeError, match="obsolete application database schema"):
        read_api.list_operation_sessions(store)


def test_all_research_readers_reject_an_obsolete_application_store(tmp_path: Path) -> None:
    store = tmp_path / "obsolete.sqlite"
    initialize_database(store)
    with sqlite3.connect(store) as connection:
        connection.execute(f"PRAGMA user_version = {APPLICATION_SCHEMA_VERSION - 1}")
    original = store.read_bytes()
    readers = [entry for entry in _store_readers() if entry[0] in _RESEARCH_READERS]
    assert {name for name, _, _ in readers} == _RESEARCH_READERS
    for _, function, arguments in readers:
        call = {key: (store if value is None else value) for key, value in arguments.items()}
        with pytest.raises(RuntimeError, match="obsolete application database schema"):
            function(**call)
    assert store.read_bytes() == original


def test_market_reader_rejects_an_obsolete_store_instead_of_degrading(tmp_path: Path) -> None:
    store = tmp_path / "market-v24.sqlite"
    with sqlite3.connect(store) as connection:
        connection.executescript(MARKET_SCHEMA_SQL)
        connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION - 1}")

    with pytest.raises(RuntimeError, match="obsolete market SQLite schema"):
        read_api.next_earnings_dates(store, ["2331"], asof=date(2026, 8, 30))


@pytest.mark.parametrize("kind", ["assessment", "proposal"])
@pytest.mark.parametrize("version", [2, 99, True, 1.0, "1"])
def test_research_readers_reject_noncurrent_or_nonstrict_payload_versions(
    tmp_path: Path, kind: str, version: object
) -> None:
    case = backend_case(tmp_path)
    proposal = reviewed_proposal(case)
    InvestmentProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
        proposal
    )
    table = "stock_assessment" if kind == "assessment" else "investment_proposal"
    raw = case.pair.document.model_dump(mode="json") if kind == "assessment" else proposal.payload()
    raw["schema_version"] = version
    with sqlite3.connect(case.app) as connection:
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
    with sqlite3.connect(case.app) as connection:
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


@pytest.mark.parametrize("mutation", ["missing-table", "missing-column"])
def test_indicator_partial_store_is_not_unpublished(tmp_path: Path, mutation: str) -> None:
    from baibai_engine.market_context.indicators.db import seed_definitions
    from baibai_engine.market_context.indicators.definitions import load_definitions

    path = tmp_path / "indicators.sqlite"
    with closing(initialize_indicator_database(path)) as connection:
        seed_definitions(connection, load_definitions())
        connection.commit()
    readers = [
        lambda: read_api.reading_snapshot(path, asof=date(2026, 7, 29)),
        lambda: read_api.indicator_series(path, series_id="jp.cpi.headline"),
    ]
    assert all(isinstance(reader(), dict) for reader in readers)
    assert read_api.indicator_series(path, series_id="unknown") is None
    with sqlite3.connect(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        connection.execute("DROP TABLE observations")
        if mutation == "missing-column":
            connection.execute("CREATE TABLE observations (series_id TEXT)")
    original = path.read_bytes()
    for reader in readers:
        with pytest.raises(
            sqlite3.OperationalError,
            match="no such table: observations"
            if mutation == "missing-table"
            else "no such column",
        ):
            reader()
    assert path.read_bytes() == original
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == version


def test_run_payload_partial_store_is_not_an_absent_id(tmp_path: Path) -> None:
    from tests.helpers.screening_run import screening_run_payload, stock_analysis

    from baibai_engine.screening.run_store import ScreeningRunStore

    path = tmp_path / "runs.sqlite"
    ScreeningRunStore(path).publish_run(
        screening_run_payload(stock_analyses=(stock_analysis(),)),
        run_revision_id="run-test",
        rules=load_screening_rules(),
    )
    assert isinstance(read_api.screening_run_payload(path, run_revision_id="run-test"), dict)
    assert read_api.screening_run_payload(path, run_revision_id="absent") is None
    with sqlite3.connect(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        connection.execute("DROP TABLE stock_analysis")
    original = path.read_bytes()
    with pytest.raises(sqlite3.OperationalError, match="no such table: stock_analysis"):
        read_api.screening_run_payload(path, run_revision_id="run-test")
    assert path.read_bytes() == original
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == version
