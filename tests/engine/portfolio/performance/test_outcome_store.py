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


@pytest.mark.parametrize("retrieved_at", ["2025-12-30T18:00:00+09:00", "2026-01-01T12:00:00+09:00"])
@pytest.mark.parametrize("status", ["resolved", "unresolved"])
def test_new_outcome_rejects_benchmark_published_before_period_end(tmp_path, retrieved_at, status):
    publication = _publication(status=status)
    benchmark = publication.payload["benchmark_observation"]
    assert isinstance(benchmark, dict)
    benchmark.update(published_at="2025-12-30", retrieved_at=retrieved_at)
    path = tmp_path / "app.sqlite"
    initialize_database(path)
    before = path.read_bytes()

    with pytest.raises(ValueError, match="period_end_date cannot be after published_at"):
        PortfolioOutcomeStore(path).publish(publication)

    assert PortfolioOutcomeStore(path).list() == ()
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("basis", "published_at", "retrieved_at"),
    [
        ("official_explicit", "2025-12-31", "2025-12-30T15:00:00Z"),
        ("official_month_end_rule", "2025-12-31", "2025-12-31T00:00:00+09:00"),
        ("official_explicit", "2026-01-01", "2026-01-02T12:00:00+09:00"),
    ],
)
def test_new_outcome_preserves_benchmark_date_precision(
    tmp_path, basis, published_at, retrieved_at
):
    publication = _publication()
    benchmark = publication.payload["benchmark_observation"]
    assert isinstance(benchmark, dict)
    benchmark.update(
        period_basis=basis,
        period_rule_source_url="https://www.jpx.co.jp/rule"
        if basis == "official_month_end_rule"
        else None,
        published_at=published_at,
        retrieved_at=retrieved_at,
    )
    store = PortfolioOutcomeStore(tmp_path / "app.sqlite")
    assert store.publish(publication)
    assert not store.publish(publication)
    assert store.list() == ({"outcome_id": publication.outcome_id, **publication.payload},)


def test_saved_benchmark_clock_conflict_keeps_readback_retry_and_identity(tmp_path):
    from dataclasses import replace

    from baibai_engine.primitives.json import canonical_json

    publication = _publication()
    benchmark = publication.payload["benchmark_observation"]
    assert isinstance(benchmark, dict)
    benchmark.update(published_at="2025-12-30", retrieved_at="2025-12-30T18:00:00+09:00")
    encoded = canonical_json(publication.payload)
    path = tmp_path / "app.sqlite"
    initialize_database(path)
    # A schema-2 row accepted before the publication-time check, without rewriting it.
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO portfolio_outcome "
            "(outcome_id, horizon, period_start_date, period_end_date, status, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                publication.outcome_id,
                publication.horizon,
                publication.period_start_date,
                publication.period_end_date,
                publication.status,
                encoded,
            ),
        )
    before = path.read_bytes()
    store = PortfolioOutcomeStore(path)
    assert store.list() == ({"outcome_id": publication.outcome_id, **publication.payload},)
    assert not store.publish(publication)
    with pytest.raises(ValueError, match="period_end_date cannot be after published_at"):
        store.publish(replace(publication, outcome_id="new-id"))
    with pytest.raises(PortfolioOutcomeConflictError):
        store.publish(replace(publication, payload={**publication.payload, "ending_cash_yen": 101}))
    assert path.read_bytes() == before
