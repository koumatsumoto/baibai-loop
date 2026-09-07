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
from datetime import date
from pathlib import Path

import pytest

import baibai_engine.read_api as read_api
from baibai_engine.appdb.schema import APPLICATION_SCHEMA_VERSION
from baibai_engine.appdb.write import initialize_database
from baibai_engine.macro.indicators.db import initialize_database as initialize_macro_database
from baibai_engine.market.sqlite.schema import _SCHEMA_SQL as MARKET_SCHEMA_SQL
from baibai_engine.market.sqlite.schema import SQLITE_SCHEMA_VERSION
from baibai_engine.read_api.sqlite import read_rows
from baibai_engine.screening.run_store import initialize_run_store
from baibai_engine.screening.run_store.schema import RUN_STORE_SCHEMA_VERSION

# These parse Git-managed YAML paths, not runtime stores.
_NOT_STORE_READERS = frozenset(
    {
        "validate_application_store_schema",
        "validate_macro_reading_rules",
    }
)
# A caller asking for one identity that is absent is told so; that is not a degrade.
_RAISES_FOR_UNKNOWN_ID = frozenset({"macro_context_payload"})
# The rule is "readers that answer what has been published degrade; readers that gate a
# write raise". These two gate the daily batch, which must stop and name the fault
# rather than skip a run that looked like a holiday.
_WRITE_GATES = frozenset({"market_calendar_business_day", "previous_run_revision_id"})
# Public callables the sweep cannot drive: they read no store, or they take a value this
# file would have to invent. Anything not listed here must be reachable by the sweep.
_DECLARED_OUTSIDE_THE_SWEEP = frozenset(
    {
        *_NOT_STORE_READERS,
        "is_unwritten_store",  # classifies an exception, not a store
        "macro_registered_series",  # reads the bundled definitions, not a store
        "macro_series_names",  # reads the bundled definitions, not a store
        "read_rows",  # takes the SQL to run, which this file would have to invent
        "replay_events_through",  # pure event replay; owned by ledger tests
        "summarize_portfolio",  # pure capital projection; owned by ledger tests
        "current_portfolio",  # composes an explicit ledger with market input; covered by current-capital tests
        "reject_noncanonical_store_paths",  # startup layout guard, not a query
        "repository_root_error",  # inspects a directory layout, not a store
        "research_triage_payload_hash",  # hashes a typed payload, not a store
        "safe_load",  # a YAML helper re-exported for callers
        "screening_calibration_method_identity",  # reads Git-managed method config
    }
)

_ARGUMENTS: dict[str, object] = {
    "after": date(2026, 7, 28),
    "as_of": date(2026, 7, 29),
    "asof": date(2026, 7, 29),
    "capital_allocation_assessment_id": "assessment-20260729-a",
    "context_id": "macro-context-2026-07-29-a",
    "day": date(2026, 7, 29),
    "end": date(2026, 7, 29),
    "operation_id": "op-20260729-a-1",
    "context_asof": date(2026, 7, 28),
    "requested_asof": date(2026, 7, 29),
    "run_revision_id": "run-revision-20260729",
    "review_set_id": "review-set-20260729-a",
    "series_id": "jp.cpi",
    "since": date(2026, 7, 28),
    "research_triage_id": "research_triage-20260729-a",
    "start": date(2026, 7, 1),
    "thesis_id": "thesis-20260729-1234-r1",
    "ticker": "1234",
    "tickers": ["1234"],
}


_STORE_PARAMETERS = ("path", "db_path", "indicators_db_path")


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
    macro = tmp_path / "macro.sqlite"
    initialize_macro_database(macro).close()

    # Every schema is built here rather than listed, so renaming a table on either
    # side of the read is what the assertion sees.
    known = {"sqlite_schema"}
    for store in (application, runs, market, macro):
        known |= table_names(store)

    unknown = sorted(_tables_named_in_read_api_sql() - known)

    assert unknown == [], (
        f"read_api reads tables no migrated store defines: {unknown}. "
        "A renamed table would otherwise degrade to an empty view forever."
    )


def test_read_rows_still_raises_for_a_broken_query(tmp_path: Path) -> None:
    store = tmp_path / "store.sqlite"
    with sqlite3.connect(store) as connection:
        connection.execute("CREATE TABLE research_triage (payload TEXT)")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        read_rows(store, "SELECT payload FROM absent_table")
    with pytest.raises(sqlite3.OperationalError, match="no such column"):
        read_rows(store, "SELECT absent_column FROM research_triage")
    with pytest.raises(sqlite3.OperationalError, match="syntax error"):
        read_rows(store, "SELEKT payload FROM research_triage")


def test_application_reader_rejects_an_obsolete_store_before_querying(tmp_path: Path) -> None:
    store = tmp_path / "application-v16.sqlite"
    initialize_database(store)
    with sqlite3.connect(store) as connection:
        connection.execute(f"PRAGMA user_version = {APPLICATION_SCHEMA_VERSION - 1}")

    with pytest.raises(RuntimeError, match="obsolete application database schema"):
        read_api.list_research_triage_payloads(store)


def test_market_reader_rejects_an_obsolete_store_instead_of_degrading(tmp_path: Path) -> None:
    store = tmp_path / "market-v24.sqlite"
    with sqlite3.connect(store) as connection:
        connection.executescript(MARKET_SCHEMA_SQL)
        connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION - 1}")

    with pytest.raises(RuntimeError, match="obsolete market SQLite schema"):
        read_api.next_earnings_dates(store, ["2331"], asof=date(2026, 8, 30))


def test_research_triage_list_and_latest_agree_on_the_newest_row(tmp_path: Path) -> None:
    """Same as-of and same published_at: the two queries must not disagree."""

    store = tmp_path / "app.sqlite"
    with sqlite3.connect(store) as connection:
        connection.execute(f"PRAGMA user_version = {APPLICATION_SCHEMA_VERSION}")
        connection.execute(
            "CREATE TABLE research_triage (research_triage_id TEXT, as_of TEXT, published_at TEXT, "
            "payload TEXT)"
        )
        for research_triage_id in ("research_triage-20260729-a", "research_triage-20260729-b"):
            connection.execute(
                "INSERT INTO research_triage VALUES (?, ?, ?, ?)",
                (
                    research_triage_id,
                    "2026-07-29",
                    "2026-07-29T14:00:00+09:00",
                    f'{{"schema_version": 3, "entries": [], "research_triage_id": "{research_triage_id}"}}',
                ),
            )

    payloads = read_api.list_research_triage_payloads(store)
    latest = read_api.latest_research_triage_payload(store)

    assert latest is not None
    assert payloads[0]["research_triage_id"] == latest["research_triage_id"]


def test_research_triage_head_orders_offset_timestamps_by_real_instant(tmp_path: Path) -> None:
    store = tmp_path / "app.sqlite"
    with sqlite3.connect(store) as connection:
        connection.execute(f"PRAGMA user_version = {APPLICATION_SCHEMA_VERSION}")
        connection.execute(
            "CREATE TABLE research_triage (research_triage_id TEXT, as_of TEXT, published_at TEXT, "
            "payload TEXT)"
        )
        for research_triage_id, published_at in (
            ("triage-earlier-instant", "2026-07-20T10:00:00+09:00"),
            ("triage-later-instant", "2026-07-20T02:30:00+00:00"),
        ):
            connection.execute(
                "INSERT INTO research_triage VALUES (?, ?, ?, ?)",
                (
                    research_triage_id,
                    "2026-07-20",
                    published_at,
                    json.dumps(
                        {
                            "schema_version": 3,
                            "entries": [],
                            "research_triage_id": research_triage_id,
                        }
                    ),
                ),
            )

    latest = read_api.latest_research_triage_payload(store)

    assert latest is not None
    assert latest["research_triage_id"] == "triage-later-instant"


def _store_with_research_triage(store: Path, payload: dict[str, object]) -> None:
    with sqlite3.connect(store) as connection:
        connection.execute(f"PRAGMA user_version = {APPLICATION_SCHEMA_VERSION}")
        connection.execute(
            "CREATE TABLE research_triage (research_triage_id TEXT, as_of TEXT, published_at TEXT, "
            "payload TEXT)"
        )
        connection.execute(
            "INSERT INTO research_triage VALUES (?, ?, ?, ?)",
            (
                str(payload["research_triage_id"]),
                "2026-07-01",
                "2026-07-01T14:00:00+09:00",
                json.dumps(payload),
            ),
        )


@pytest.mark.parametrize("schema_version", [1, 2])
def test_research_triage_reader_excludes_each_retired_version(
    tmp_path: Path, schema_version: int
) -> None:
    """Runtime readers do not infer fields for retired research_triage payloads."""

    store = tmp_path / "app.sqlite"
    _store_with_research_triage(
        store,
        {
            "schema_version": schema_version,
            "research_triage_id": f"research_triage-legacy-v{schema_version}",
            "entries": [],
        },
    )

    assert read_api.latest_research_triage_payload(store) is None


def test_research_triage_payloads_for_review_set_answers_by_the_review_set_it_judged(
    tmp_path: Path,
) -> None:
    """`research prepare` asks which judgment covers a cycle, not which ID exists."""

    store = tmp_path / "app.sqlite"
    with sqlite3.connect(store) as connection:
        connection.execute(f"PRAGMA user_version = {APPLICATION_SCHEMA_VERSION}")
        connection.execute(
            "CREATE TABLE research_triage (research_triage_id TEXT, review_set_id TEXT, as_of TEXT, "
            "published_at TEXT, payload TEXT)"
        )
        for research_triage_id, review_set_id, schema_version, published_at in (
            (
                "research_triage-20260729-current",
                "review-set-20260729-a",
                3,
                "2026-07-29T14:00:00+09:00",
            ),
        ):
            connection.execute(
                "INSERT INTO research_triage VALUES (?, ?, ?, ?, ?)",
                (
                    research_triage_id,
                    review_set_id,
                    "2026-07-29",
                    published_at,
                    json.dumps(
                        {
                            "schema_version": schema_version,
                            "entries": [],
                            "research_triage_id": research_triage_id,
                            "review_set_id": review_set_id,
                        }
                    ),
                ),
            )

    current = read_api.research_triage_payloads_for_review_set(store, "review-set-20260729-a")
    assert [payload["research_triage_id"] for payload in current] == [
        "research_triage-20260729-current"
    ]
    assert current[0]["schema_version"] == 3

    assert read_api.research_triage_payloads_for_review_set(store, "review-set-absent") == []


def test_research_triage_payloads_for_review_set_returns_every_judgment_newest_first(
    tmp_path: Path,
) -> None:
    """Two judgments over one Review Set is a store the caller must refuse, not pick from.

    Publication cannot produce this state, so the query reports what it found and
    leaves the refusal to the boundary that knows one judgment is required.
    """

    store = tmp_path / "app.sqlite"
    with sqlite3.connect(store) as connection:
        connection.execute(f"PRAGMA user_version = {APPLICATION_SCHEMA_VERSION}")
        connection.execute(
            "CREATE TABLE research_triage (research_triage_id TEXT, review_set_id TEXT, as_of TEXT, "
            "published_at TEXT, payload TEXT)"
        )
        for research_triage_id, published_at in (
            ("research_triage-20260729-first", "2026-07-29T14:00:00+09:00"),
            ("research_triage-20260729-second", "2026-07-29T15:00:00+09:00"),
        ):
            connection.execute(
                "INSERT INTO research_triage VALUES (?, ?, ?, ?, ?)",
                (
                    research_triage_id,
                    "review-set-20260729-a",
                    "2026-07-29",
                    published_at,
                    json.dumps(
                        {
                            "schema_version": 3,
                            "entries": [],
                            "research_triage_id": research_triage_id,
                            "review_set_id": "review-set-20260729-a",
                        }
                    ),
                ),
            )

    payloads = read_api.research_triage_payloads_for_review_set(store, "review-set-20260729-a")

    assert [payload["research_triage_id"] for payload in payloads] == [
        "research_triage-20260729-second",
        "research_triage-20260729-first",
    ]


def test_research_triage_reader_excludes_an_unknown_version(tmp_path: Path) -> None:
    store = tmp_path / "app.sqlite"
    with sqlite3.connect(store) as connection:
        connection.execute(f"PRAGMA user_version = {APPLICATION_SCHEMA_VERSION}")
        connection.execute(
            "CREATE TABLE research_triage (research_triage_id TEXT, as_of TEXT, published_at TEXT, "
            "payload TEXT)"
        )
        connection.execute(
            "INSERT INTO research_triage VALUES (?, ?, ?, ?)",
            (
                "unknown",
                "2026-07-01",
                "2026-07-01T14:00:00+09:00",
                json.dumps({"schema_version": 99, "entries": [], "research_triage_id": "unknown"}),
            ),
        )

    assert read_api.latest_research_triage_payload(store) is None


@pytest.mark.parametrize("schema_version", [2])
def test_assessment_reader_rejects_each_retired_version(
    tmp_path: Path, schema_version: int
) -> None:
    store = tmp_path / "app.sqlite"
    with sqlite3.connect(store) as connection:
        connection.execute(f"PRAGMA user_version = {APPLICATION_SCHEMA_VERSION}")
        connection.execute(
            "CREATE TABLE capital_allocation_assessment (capital_allocation_assessment_id TEXT, as_of TEXT, "
            "published_at TEXT, payload TEXT)"
        )
        connection.execute(
            "INSERT INTO capital_allocation_assessment VALUES (?, ?, ?, ?)",
            (
                f"assessment-legacy-v{schema_version}",
                "2026-07-01",
                "2026-07-01T14:00:00+09:00",
                json.dumps(
                    {
                        "schema_version": schema_version,
                        "capital_allocation_assessment_id": f"assessment-legacy-v{schema_version}",
                        "lanes": [{"ticker": "2331"}],
                    }
                ),
            ),
        )

    with pytest.raises(ValueError, match="unsupported capital allocation schema_version"):
        read_api.list_capital_allocation_assessment_payloads(store)


def test_assessment_reader_rejects_an_unknown_version(tmp_path: Path) -> None:
    store = tmp_path / "app.sqlite"
    with sqlite3.connect(store) as connection:
        connection.execute(f"PRAGMA user_version = {APPLICATION_SCHEMA_VERSION}")
        connection.execute(
            "CREATE TABLE capital_allocation_assessment (capital_allocation_assessment_id TEXT, as_of TEXT, "
            "published_at TEXT, payload TEXT)"
        )
        connection.execute(
            "INSERT INTO capital_allocation_assessment VALUES (?, ?, ?, ?)",
            (
                "assessment-unknown",
                "2026-07-01",
                "2026-07-01T14:00:00+09:00",
                json.dumps(
                    {"schema_version": 99, "capital_allocation_assessment_id": "assessment-unknown"}
                ),
            ),
        )

    with pytest.raises(ValueError, match="unsupported capital allocation schema_version"):
        read_api.list_capital_allocation_assessment_payloads(store)
