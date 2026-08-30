"""The export preconditions, tested where they are decided.

`validate_market_store_hydration` is what stands between an emptied published copy
and an export that writes valuations with no price behind them. Its cases were
reachable only through a whole export, so two of them were paying for a full view
tree to observe one comparison.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from tests.helpers.screening_sqlite import market_store_with_fetch_claim

from baibai_engine.appdb.schema import APPLICATION_SCHEMA_VERSION
from baibai_engine.appdb.write import initialize_database
from baibai_engine.market.sqlite.schema import SQLITE_SCHEMA_VERSION, open_connection
from baibai_engine.read_api.materialization import (
    MaterializationPreconditionError,
    validate_application_store_schema,
    validate_market_store_hydration,
    validate_market_store_schema,
)


def test_current_application_and_market_schemas_are_accepted(tmp_path: Path) -> None:
    application = tmp_path / "application.sqlite"
    initialize_database(application)
    market = tmp_path / "market.sqlite"
    open_connection(market).close()

    validate_application_store_schema(application)
    validate_market_store_schema(market)


def test_obsolete_application_and_market_schemas_are_rejected(tmp_path: Path) -> None:
    application = tmp_path / "application-v16.sqlite"
    initialize_database(application)
    with sqlite3.connect(application) as connection:
        connection.execute(f"PRAGMA user_version = {APPLICATION_SCHEMA_VERSION - 1}")
    market = tmp_path / "market-v24.sqlite"
    open_connection(market).close()
    with sqlite3.connect(market) as connection:
        connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION - 1}")

    with pytest.raises(MaterializationPreconditionError, match="obsolete application"):
        validate_application_store_schema(application)
    with pytest.raises(MaterializationPreconditionError, match="obsolete market"):
        validate_market_store_schema(market)


def test_versioned_market_store_without_tables_is_rejected(tmp_path: Path) -> None:
    incomplete = tmp_path / "market-incomplete.sqlite"
    with sqlite3.connect(incomplete) as connection:
        connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION}")

    with pytest.raises(MaterializationPreconditionError, match="missing tables"):
        validate_market_store_schema(incomplete)


def test_an_emptied_table_under_a_positive_claim_is_refused(tmp_path: Path) -> None:
    """The published copy's lake-owned tables are emptied and its fetch ledger travels
    with it, so every query still answers — with nothing. The 2026-08-17 manual publish
    exported from exactly that state and reported success."""

    path = market_store_with_fetch_claim(tmp_path, claimed_rows=15_052_807, held_rows=0)

    with pytest.raises(MaterializationPreconditionError) as caught:
        validate_market_store_hydration(path)

    message = str(caught.value)
    assert "market store is not hydrated" in message
    assert "jquants.daily_bars claims 15052807 row(s) and holds none" in message


def test_a_store_holding_fewer_rows_than_its_windows_claim_is_accepted(tmp_path: Path) -> None:
    """Overlapping coverage windows make the claim exceed the rows on a healthy store
    — on the working store by 4.9M — so only an empty table may stop the export."""

    path = market_store_with_fetch_claim(tmp_path, claimed_rows=15_052_807, held_rows=1)

    validate_market_store_hydration(path)


def test_a_table_whose_ledger_claims_nothing_is_accepted(tmp_path: Path) -> None:
    """A dataset nothing has fetched here is empty for a reason the store cannot tell
    from an unfilled one, so the claim is what makes emptiness a fault."""

    path = market_store_with_fetch_claim(tmp_path, claimed_rows=0, held_rows=0)

    validate_market_store_hydration(path)


def test_a_store_that_does_not_exist_yet_is_accepted(tmp_path: Path) -> None:
    """A first run has no market store; the views over it render empty by design."""

    validate_market_store_hydration(tmp_path / "missing.sqlite")
