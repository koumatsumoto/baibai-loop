"""One degrade policy for every read_api query: an unwritten store reads as empty."""

from __future__ import annotations

import inspect
import sqlite3
from datetime import date
from pathlib import Path

import pytest

import baibai_engine.read_api as read_api
from baibai_engine.read_api.sqlite import read_rows

# ``load_portfolio_ledger`` / ``load_thesis`` parse a YAML path, not a store.
_NOT_STORE_READERS = frozenset({"load_portfolio_ledger", "load_thesis"})
# A caller asking for one identity that is absent is told so; that is not a degrade.
_RAISES_FOR_UNKNOWN_ID = frozenset({"macro_context_payload"})
# The rule is "readers that answer what has been published degrade; readers that gate a
# write raise". These two gate the daily batch, which must stop and name the fault
# rather than skip a run that looked like a holiday.
_WRITE_GATES = frozenset({"market_calendar_business_day", "previous_run_revision_id"})

_ARGUMENTS: dict[str, object] = {
    "as_of": date(2026, 7, 29),
    "asof": date(2026, 7, 29),
    "assessment_id": "assessment-20260729-a",
    "context_id": "macro-context-2026-07-29-a",
    "day": date(2026, 7, 29),
    "operation_id": "op-20260729-a-1",
    "run_revision_id": "run-revision-20260729",
    "series_id": "jp.cpi",
    "shortlist_id": "shortlist-20260729-a",
    "thesis_id": "thesis-20260729-1234-r1",
    "ticker": "1234",
    "tickers": ["1234"],
}


def _store_readers() -> list[tuple[str, object, dict[str, object]]]:
    readers: list[tuple[str, object, dict[str, object]]] = []
    for name in sorted(read_api.__all__):
        function = getattr(read_api, name)
        if not callable(function) or inspect.isclass(function):
            continue
        if name in _NOT_STORE_READERS or name in _WRITE_GATES:
            continue
        try:
            signature = inspect.signature(function)
        except (TypeError, ValueError):  # pragma: no cover - builtins have no signature
            continue
        arguments: dict[str, object] = {}
        for parameter_name, parameter in signature.parameters.items():
            if parameter.default is not inspect.Parameter.empty:
                continue
            if parameter_name in ("path", "db_path"):
                arguments[parameter_name] = None  # filled in per test
            elif parameter_name in _ARGUMENTS:
                arguments[parameter_name] = _ARGUMENTS[parameter_name]
            else:  # a reader this sweep cannot call without inventing a value
                break
        else:
            if any(key in arguments for key in ("path", "db_path")):
                readers.append((name, function, arguments))
    return readers


def test_the_sweep_covers_every_store_reader() -> None:
    covered = {name for name, _, _ in _store_readers()}

    # A reader added without a store argument would silently escape the policy below.
    assert len(covered) >= 30
    assert "list_shortlist_payloads" in covered
    assert "portfolio_ledger_document" in covered


def test_no_store_reader_raises_when_the_writer_has_not_run(tmp_path: Path) -> None:
    empty = tmp_path / "empty.sqlite"
    sqlite3.connect(empty).close()

    leaked: list[str] = []
    for name, function, arguments in _store_readers():
        call = {key: (empty if value is None else value) for key, value in arguments.items()}
        try:
            function(**call)
        except sqlite3.OperationalError as error:
            leaked.append(f"{name}: {error}")
        except ValueError:
            assert name in _RAISES_FOR_UNKNOWN_ID, f"{name} raised for an empty store"

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


def test_read_rows_still_raises_for_a_broken_query(tmp_path: Path) -> None:
    store = tmp_path / "store.sqlite"
    with sqlite3.connect(store) as connection:
        connection.execute("CREATE TABLE shortlist (payload TEXT)")

    assert read_rows(store, "SELECT payload FROM absent_table") == []
    with pytest.raises(sqlite3.OperationalError, match="no such column"):
        read_rows(store, "SELECT absent_column FROM shortlist")
    with pytest.raises(sqlite3.OperationalError, match="syntax error"):
        read_rows(store, "SELEKT payload FROM shortlist")


def test_shortlist_list_and_latest_agree_on_the_newest_row(tmp_path: Path) -> None:
    """Same as-of and same published_at: the two queries must not disagree."""

    store = tmp_path / "app.sqlite"
    with sqlite3.connect(store) as connection:
        connection.execute(
            "CREATE TABLE shortlist (shortlist_id TEXT, as_of TEXT, published_at TEXT, "
            "payload TEXT)"
        )
        for shortlist_id in ("shortlist-20260729-a", "shortlist-20260729-b"):
            connection.execute(
                "INSERT INTO shortlist VALUES (?, ?, ?, ?)",
                (
                    shortlist_id,
                    "2026-07-29",
                    "2026-07-29T14:00:00+09:00",
                    f'{{"shortlist_id": "{shortlist_id}"}}',
                ),
            )

    payloads = read_api.list_shortlist_payloads(store)
    latest = read_api.latest_shortlist_payload(store)

    assert latest is not None
    assert payloads[0]["shortlist_id"] == latest["shortlist_id"]
