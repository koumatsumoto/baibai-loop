from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from tests.helpers.outcome_publication import _publication

from baibai_engine.portfolio.performance.store import (
    PortfolioOutcomeConflictError,
    PortfolioOutcomePublication,
    PortfolioOutcomeStore,
)
from baibai_engine.storage.sqlite.application.write import initialize_database


def test_outcome_publication_is_create_only_and_queryable(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    store = PortfolioOutcomeStore(path)
    publication = _publication()

    assert publication.payload["schema_version"] == 2
    assert "market_data_sha256" not in publication.payload
    assert "market_data_fingerprint" not in publication.payload
    assert store.publish(publication)
    assert not store.publish(publication)
    before = path.read_bytes()
    assert store.list()[0]["outcome_id"] == publication.outcome_id
    assert store.list()[0]["benchmark_observation"]["cumulative_return_pct"] == 8.2  # type: ignore[index]
    assert path.read_bytes() == before


def test_outcome_conflict_rolls_back(tmp_path: Path) -> None:
    store = PortfolioOutcomeStore(tmp_path / "app.sqlite")
    store.publish(_publication())

    with pytest.raises(PortfolioOutcomeConflictError):
        store.publish(_publication(status="unresolved"))

    assert store.list()[0]["status"] == "resolved"


def test_outcome_requires_embedded_benchmark(tmp_path: Path) -> None:
    publication = _publication()
    invalid = PortfolioOutcomePublication(
        outcome_id=publication.outcome_id,
        horizon=publication.horizon,
        period_start_date=publication.period_start_date,
        period_end_date=publication.period_end_date,
        status=publication.status,
        payload={
            key: value
            for key, value in publication.payload.items()
            if key != "benchmark_observation"
        },
    )

    with pytest.raises(ValueError, match="benchmark_observation"):
        PortfolioOutcomeStore(tmp_path / "app.sqlite").publish(invalid)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"schema_version": 1}, "schema_version"),
        ({"market_data_sha256": "a" * 64}, "Extra inputs"),
        ({"market_data_fingerprint": "retired-signature"}, "Extra inputs"),
        ({"unexpected": True}, "Extra inputs"),
        ({"excess_percentage_points": 99.0}, "portfolio minus benchmark"),
        ({"benchmark_cumulative_return_pct": 8.1}, "benchmark return"),
        ({"ending_cash_yen": None}, "resolved outcome fields"),
        ({"estimated_exit_tax_yen": None}, "estimated exit tax requires a value"),
        ({"estimated_exit_tax_status": "unknown"}, "unknown exit tax must be null"),
    ],
)
def test_outcome_rejects_retired_schema_and_semantic_bypasses(
    tmp_path: Path,
    change: dict[str, object],
    message: str,
) -> None:
    publication = _publication()
    invalid = PortfolioOutcomePublication(
        outcome_id=publication.outcome_id,
        horizon=publication.horizon,
        period_start_date=publication.period_start_date,
        period_end_date=publication.period_end_date,
        status=publication.status,
        payload={**publication.payload, **change},
    )

    with pytest.raises(ValueError, match=message):
        PortfolioOutcomeStore(tmp_path / "app.sqlite").publish(invalid)


def test_outcome_rejects_invalid_embedded_benchmark_without_write(tmp_path: Path) -> None:
    publication = _publication()
    benchmark_source = publication.payload["benchmark_observation"]
    assert isinstance(benchmark_source, dict)
    benchmark = dict(benchmark_source)
    benchmark["horizon"] = "5y"
    invalid = PortfolioOutcomePublication(
        outcome_id=publication.outcome_id,
        horizon=publication.horizon,
        period_start_date=publication.period_start_date,
        period_end_date=publication.period_end_date,
        status=publication.status,
        payload={**publication.payload, "benchmark_observation": benchmark},
    )
    path = tmp_path / "app.sqlite"

    with pytest.raises(ValueError, match=r"period|horizon"):
        PortfolioOutcomeStore(path).publish(invalid)

    assert PortfolioOutcomeStore(path).list() == ()
    assert not path.exists()


@pytest.mark.parametrize("unwritten", [False, True])
def test_outcome_list_does_not_initialize_an_absent_or_unwritten_store(tmp_path, unwritten):
    path = tmp_path / "app.sqlite"
    if unwritten:
        path.touch()
    assert PortfolioOutcomeStore(path).list() == ()
    assert path.exists() == unwritten
    if unwritten:
        assert path.read_bytes() == b""


def test_outcome_list_does_not_repair_a_missing_table(tmp_path):
    path = tmp_path / "app.sqlite"
    store = PortfolioOutcomeStore(path)
    store.publish(_publication())
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("DROP TABLE portfolio_outcome")
        connection.commit()
    before = path.read_bytes()
    with pytest.raises(sqlite3.OperationalError, match="portfolio_outcome"):
        store.list()
    assert path.read_bytes() == before


def test_historical_schema_one_outcome_reads_without_rewrite_or_republication(tmp_path):
    database = tmp_path / "app.sqlite"
    current = _publication()
    legacy = {
        **current.payload,
        "schema_version": 1,
        "market_data_sha256": "a" * 64,
        "market_data_fingerprint": "historical-bars-v1",
    }
    encoded = json.dumps(legacy, indent=1)
    initialize_database(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "INSERT INTO portfolio_outcome "
            "(outcome_id, horizon, period_start_date, period_end_date, status, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                current.outcome_id,
                current.horizon,
                current.period_start_date,
                current.period_end_date,
                current.status,
                encoded,
            ),
        )
    before = database.read_bytes()
    expected = {"outcome_id": current.outcome_id, **legacy}

    assert PortfolioOutcomeStore(database).list() == (expected,)
    assert database.read_bytes() == before
    # A schema change does not authorize replacing the immutable saved result.
    with pytest.raises(PortfolioOutcomeConflictError):
        PortfolioOutcomeStore(database).publish(current)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT payload FROM portfolio_outcome WHERE outcome_id = ?",
            (current.outcome_id,),
        ).fetchone() == (encoded,)
    assert database.read_bytes() == before
