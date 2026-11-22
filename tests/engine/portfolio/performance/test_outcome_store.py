from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from tests.helpers.outcome_publication import _publication

from baibai_engine.portfolio.performance.store import (
    PortfolioOutcomeConflictError,
    PortfolioOutcomePublication,
    PortfolioOutcomeStore,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.sqlite.application.write import initialize_database


def test_outcome_publication_is_create_only_and_queryable(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    store = PortfolioOutcomeStore(path)
    publication = _publication()

    assert publication.payload["schema_version"] == 2
    assert "market_data_sha256" not in publication.payload
    assert "market_data_fingerprint" not in publication.payload
    first = store.publish(publication)
    retry = store.publish(publication)
    assert first.created
    assert not retry.created
    assert first.outcome_id == retry.outcome_id == publication.outcome_id
    assert first.payload_sha256 == retry.payload_sha256
    before = path.read_bytes()
    fixed = store.get(publication.outcome_id)
    assert fixed is not None
    assert fixed.payload_sha256 == first.payload_sha256
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
        ({"schema_version": 2.0}, "schema_version"),
        ({"period_end_date": "2025-12-31T00:00:00"}, "period_end_date"),
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
    store = PortfolioOutcomeStore(path)
    assert store.list() == ()
    assert store.get("not-saved") is None
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
    with pytest.raises(sqlite3.OperationalError, match="portfolio_outcome"):
        store.get(_publication().outcome_id)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("horizon", "summary"),
    [("3y", 11.3), ("3y", None), ("1y", 1.0)],
)
def test_resolved_outcome_matches_embedded_benchmark_annualized_return(tmp_path, horizon, summary):
    publication = _publication()
    if horizon == "3y":
        start = "2022-12-31"
        source = publication.payload["benchmark_observation"]
        assert isinstance(source, dict)
        benchmark = {
            **source,
            "horizon": "3y",
            "period_start_date": start,
            "cumulative_return_pct": 33.1,
            "annualized_return_pct": 10.0,
        }
        publication = replace(
            publication,
            horizon="3y",
            period_start_date=start,
            payload={
                **publication.payload,
                "horizon": "3y",
                "period_start_date": start,
                "market_data_coverage_start_date": start,
                "benchmark_cumulative_return_pct": 33.1,
                "excess_percentage_points": 10.2 - 33.1,
                "benchmark_annualized_return_pct": 10.0,
                "benchmark_observation": benchmark,
            },
        )
    incoherent = {**publication.payload, "benchmark_annualized_return_pct": summary}
    path = tmp_path / "app.sqlite"
    store = PortfolioOutcomeStore(path)
    with pytest.raises(ValueError, match="annualized return does not match benchmark"):
        store.publish(replace(publication, payload=incoherent))
    assert not path.exists()

    assert store.publish(publication).created
    saved = store.get(publication.outcome_id)
    assert saved is not None
    assert saved.payload["benchmark_annualized_return_pct"] == (10.0 if horizon == "3y" else None)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE portfolio_outcome SET payload = ?", (canonical_json(incoherent),)
        )
    before = path.read_bytes()
    with pytest.raises(ValueError, match="invalid stored content"):
        store.get(publication.outcome_id)
    with pytest.raises(ValueError, match="invalid stored content"):
        store.list()
    assert path.read_bytes() == before


def test_outcome_writer_validates_numeric_types_after_serialization(tmp_path):
    path = tmp_path / "app.sqlite"
    publication = _publication()
    # The pre-encoding Pydantic float accepted this Decimal. The actual JSON
    # number would instead be a string, which the exact saved reader cannot accept.
    payload = {**publication.payload, "portfolio_twr_pct": Decimal("10.2")}
    with pytest.raises(ValueError, match="portfolio_twr_pct"):
        PortfolioOutcomeStore(path).publish(replace(publication, payload=payload))
    assert not path.exists()


@pytest.mark.parametrize(
    ("variant", "diagnostic"),
    [
        ("wrong_metric", "invalid stored content"),
        ("duplicate_v2_period", "duplicate JSON field"),
        ("duplicate_v1_period", "duplicate JSON field"),
        ("duplicate_nested_benchmark", "duplicate JSON field"),
        ("overflowing_v1_number", "non-finite JSON number"),
        ("impossible_v1_date", "period dates must be canonical"),
        ("noncanonical_v1_date", "period dates must be canonical"),
    ],
)
def test_outcome_get_never_exports_corrupt_content_even_with_its_digest(
    tmp_path, capsys, variant, diagnostic
):
    from baibai_engine.cli.app import main

    path = tmp_path / "app.sqlite"
    publication = _publication()
    PortfolioOutcomeStore(path).publish(publication)
    raw = dict(publication.payload)
    if "_v1_" in variant:
        raw["schema_version"] = 1
    period_end = {
        "impossible_v1_date": "2025-12-99",
        "noncanonical_v1_date": "2026-W01-3",  # Cutoffs must stay YYYY-MM-DD, not merely ISO-like.
    }.get(variant, publication.period_end_date)
    raw["period_end_date"] = period_end
    encoded = canonical_json(raw)
    if "_period" in variant:
        # Escaping the last key does not make it distinct from the preceding keys.
        encoded = encoded[:-1] + (
            r',"period_end_date":"2099-01-01","period_end_\u0064ate":"2025-12-31"}'
        )
        assert json.loads(encoded) == raw  # A permissive parser silently chooses the last value.
    elif variant == "duplicate_nested_benchmark":
        encoded = encoded.replace(
            '"annualized_return_pct":null',
            '"annualized_return_pct":999.0,"annualized_return_pct":null',
        )
        assert json.loads(encoded) == raw
    elif variant in {"overflowing_v1_number", "wrong_metric"}:
        number = "1e999" if variant == "overflowing_v1_number" else "999.0"
        encoded = encoded.replace('"portfolio_twr_pct":10.2', f'"portfolio_twr_pct":{number}')
    with closing(sqlite3.connect(path)) as connection, connection:
        assert connection.execute("SELECT json_valid(?)", (encoded,)).fetchone() == (1,)
        connection.execute(
            "UPDATE portfolio_outcome SET period_end_date=?, payload=?", (period_end, encoded)
        )
    before = path.read_bytes()
    destination = tmp_path / "export.yaml"
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    assert (
        main(
            [
                "portfolio",
                "outcome-get",
                publication.outcome_id,
                "--db",
                str(path),
                "--sha256",
                digest,
                "--out",
                str(destination),
            ]
        )
        == 2
    )
    observed = capsys.readouterr()
    assert observed.out == ""
    assert diagnostic in observed.err
    assert "999.0" not in observed.err
    with pytest.raises(ValueError, match=diagnostic):
        PortfolioOutcomeStore(path).list()
    assert not destination.exists()
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

    store = PortfolioOutcomeStore(database)
    assert store.list() == (expected,)
    fixed = store.get(current.outcome_id)
    assert fixed is not None
    assert fixed.payload == expected
    assert fixed.payload_sha256 == hashlib.sha256(encoded.encode("utf-8")).hexdigest()
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


@pytest.mark.parametrize(
    ("changed", "diagnostic"),
    [
        ({"outcome_id": "private-wrong-id"}, "identity"),
        ({"horizon": "3y"}, "identity"),
        ({"period_start_date": "2024-12-25"}, "identity"),
        ({"period_end_date": "2099-01-01"}, "identity"),
        ({"status": "unresolved"}, "identity"),
        ({"schema_version": True}, "schema"),
        ({"schema_version": 3}, "schema"),
        ({"benchmark_cumulative_return_pct": 91.0}, "stored content"),
        ({"portfolio_twr_pct": 91.0}, "stored content"),
        ({"market_data_sha256": "retired"}, "stored content"),
    ],
)
def test_saved_list_and_exact_read_reject_incoherent_rows_without_mutation(
    tmp_path, changed, diagnostic
):
    path = tmp_path / "app.sqlite"
    store = PortfolioOutcomeStore(path)
    publication = _publication()
    store.publish(publication)
    store.publish(replace(publication, outcome_id="another-id"))
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE portfolio_outcome SET payload=? WHERE outcome_id=?",
            (canonical_json({**publication.payload, **changed}), publication.outcome_id),
        )
    before = path.read_bytes()
    with pytest.raises(ValueError, match=diagnostic) as list_failure:
        store.list()
    with pytest.raises(ValueError, match=diagnostic) as exact_failure:
        store.get(publication.outcome_id)
    assert str(list_failure.value) == str(exact_failure.value)
    assert "private-wrong-id" not in str(exact_failure.value)
    good = store.get("another-id")
    assert good is not None
    assert good.payload == {"outcome_id": "another-id", **publication.payload}
    assert store.get("not-saved") is None
    assert path.read_bytes() == before


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
    assert store.publish(publication).created
    assert not store.publish(publication).created
    assert store.list() == ({"outcome_id": publication.outcome_id, **publication.payload},)


def test_saved_benchmark_clock_conflict_keeps_readback_retry_and_identity(tmp_path):
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
    fixed = store.get(publication.outcome_id)
    assert fixed is not None
    assert fixed.payload == store.list()[0]
    assert not store.publish(publication).created
    with pytest.raises(ValueError, match="period_end_date cannot be after published_at"):
        store.publish(replace(publication, outcome_id="new-id"))
    with pytest.raises(PortfolioOutcomeConflictError):
        store.publish(replace(publication, payload={**publication.payload, "ending_cash_yen": 101}))
    assert path.read_bytes() == before
