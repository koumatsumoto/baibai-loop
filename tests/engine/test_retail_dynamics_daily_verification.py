from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from baibai_engine.market.retail_dynamics.contract import (
    ANALYST_RATING_TABLE,
    MODEL_VALUATION_TABLE,
)
from baibai_engine.market.retail_dynamics.models import (
    AnalystRatingObservation,
    ModelValuationObservation,
)
from baibai_engine.market.retail_dynamics.reader import read_observations
from baibai_engine.market.retail_dynamics.store import save_observations
from baibai_engine.market.retail_dynamics.universe import frozen_universe
from baibai_engine.market.retail_dynamics.verification import history_digests, verify_observations
from baibai_engine.market.sqlite.lake_origin import LakeStoreOrigin, write_lake_store_origin
from baibai_engine.market.sqlite.schema import SQLITE_SCHEMA_VERSION, open_connection
from baibai_engine.market.universe import TSE_33_SECTORS, UniverseSourceDriftError

ASOF = date(2026, 10, 11)
RECEIVED = datetime(2026, 10, 11, 0, 0, 1, 123456, tzinfo=UTC)
ORIGIN = LakeStoreOrigin("synthetic-fixed-release", "a" * 64)


def seed_database(path, *, count=2500, snapshot=ASOF, origin=True):
    sectors = sorted(TSE_33_SECTORS)
    markets = ("PRIME", "standard", "グロース", "東証一部")
    tickers = tuple(str(1000 + index) for index in range(count))
    connection = open_connection(path)
    try:
        connection.executemany(
            "INSERT INTO jquants_master_snapshots VALUES (?,?,?,?,?,?)",
            [
                (
                    snapshot.isoformat(),
                    ticker,
                    f"合成会社{index}",
                    markets[index % 4],
                    sectors[index % 33],
                    index % 2,
                )
                for index, ticker in enumerate(tickers)
            ],
        )
        if origin:
            write_lake_store_origin(connection, ORIGIN)
        connection.execute(
            "INSERT INTO source_coverage(source,coverage_key,fetched_at_utc,record_count,status) VALUES (?,?,?,?,?)",
            ("synthetic", "saved", RECEIVED.isoformat(), count, "ok"),
        )
        connection.commit()
    finally:
        connection.close()
    return tickers


def model(at=RECEIVED, **kwargs):
    return ModelValuationObservation.model_validate(
        {
            "ticker": "7203",
            "fetched_at_utc": at,
            "distributor": "monex",
            "origin": "synthetic-distributor",
            "source_url": "https://synthetic.invalid/model",
            "parser_version": "synthetic-1",
            "consensus_target": 100,
            "per_company_forecast": 101,
            "per_consensus": 102,
            "pbr": 103,
            "currency": "JPY",
            "unit": "JPY/share",
            "share_basis": "synthetic-common",
            "per_consensus_origin": "synthetic-origin",
            "per_consensus_period": "synthetic-FY",
            "per_consensus_count": 4,
            "per_consensus_updated_label": "synthetic-update",
            **kwargs,
        }
    )


def rating(at=RECEIVED, **kwargs):
    return AnalystRatingObservation.model_validate(
        {
            "ticker": "7203",
            "fetched_at_utc": at,
            "distributor": "rakuten",
            "origin": "synthetic-origin",
            "source_url": "https://synthetic.invalid/rating",
            "parser_version": "synthetic-1",
            "rating": 3.5,
            "rating_scale_min": 1,
            "rating_scale_max": 5,
            "rating_scale_direction": "higher_is_positive",
            "rating_count": 4,
            "rating_updated_label": "synthetic-update",
            **kwargs,
        }
    )


def non_retail_digest(path):
    with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as connection:
        rows = {
            table: connection.execute(f'SELECT * FROM "{table}" ORDER BY 1,2').fetchall()
            for table in ("jquants_master_snapshots", "source_coverage", "lake_store_origin")
        }
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def test_exact_saved_universe_uses_market_and_all_33_sectors_keeps_alpha_and_fixed_binding(
    tmp_path,
):
    database = tmp_path / "market.sqlite"
    base = seed_database(database)
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO jquants_master_snapshots VALUES (?,?,?,?,?,?)",
            [
                (ASOF.isoformat(), "130A", "合成英字会社", "PRIME", "電気機器", 0),
                (ASOF.isoformat(), "9401", "合成ETF", "PRIME", "その他", 1),
                (ASOF.isoformat(), "9402", "合成PRO", "TOKYO PRO MARKET", "電気機器", 1),
                (ASOF.isoformat(), "9403", "合成市場外", "RESIDUAL", "電気機器", 1),
            ],
        )
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    tickers, binding = frozen_universe(database, ASOF)
    assert tickers == tuple(sorted((*base, "130A")))
    assert len(tickers) == 2501
    assert frozen_universe(database, ASOF) == (tickers, binding)
    assert json.loads(binding) == {
        "master_asof": ASOF.isoformat(),
        "release_id": ORIGIN.release_id,
        "release_manifest_sha256": ORIGIN.release_manifest_sha256,
        "tickers_sha256": hashlib.sha256("\n".join(tickers).encode()).hexdigest(),
    }
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("PRAGMA user_version").fetchone()[0] == SQLITE_SCHEMA_VERSION == 29
        )


@pytest.mark.parametrize(
    ("snapshot", "requested"),
    [
        (ASOF, ASOF - timedelta(days=1)),
        (ASOF - timedelta(days=1), ASOF),
    ],
)
def test_future_or_prior_master_fallback_cannot_form_a_daily_plan(tmp_path, snapshot, requested):
    database = tmp_path / "market.sqlite"
    seed_database(database, snapshot=snapshot)
    with pytest.raises(ValueError, match="exact saved master"):
        frozen_universe(database, requested)


def test_exact_future_dated_master_is_rejected_even_when_complete(tmp_path):
    database = tmp_path / "market.sqlite"
    future = datetime.now(ZoneInfo("Asia/Tokyo")).date() + timedelta(days=1)
    seed_database(database, snapshot=future)
    with pytest.raises(ValueError, match="master cannot be in the future"):
        frozen_universe(database, future)


@pytest.mark.parametrize("condition", ["below_floor", "not_hydrated", "empty"])
def test_incomplete_or_unhydrated_saved_master_is_rejected(tmp_path, condition):
    database = tmp_path / "market.sqlite"
    seed_database(
        database,
        count=2499 if condition == "below_floor" else 0 if condition == "empty" else 2500,
        origin=condition != "not_hydrated",
    )
    with pytest.raises(ValueError, match="exact saved master"):
        frozen_universe(database, ASOF)


@pytest.mark.parametrize("classification", ["その他", "TOKYO PRO MARKET"])
def test_non_common_or_excluded_market_rows_cannot_fill_the_common_stock_floor(
    tmp_path, classification
):
    database = tmp_path / "market.sqlite"
    seed_database(database)
    with sqlite3.connect(database) as connection:
        if classification == "その他":
            connection.execute(
                "UPDATE jquants_master_snapshots SET sector_33='その他' WHERE ticker <> '1000'"
            )
        else:
            connection.execute(
                "UPDATE jquants_master_snapshots SET market='TOKYO PRO MARKET' WHERE ticker <> '1000'"
            )
    with pytest.raises(ValueError, match=r"common.stock|common stock|production floor"):
        frozen_universe(database, ASOF)


def test_unknown_sector_cannot_be_silently_excluded(tmp_path):
    database = tmp_path / "market.sqlite"
    seed_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE jquants_master_snapshots SET sector_33='未知の新業種' WHERE ticker='1000'"
        )
    with pytest.raises(UniverseSourceDriftError, match="Unknown J-Quants sector"):
        frozen_universe(database, ASOF)


@pytest.mark.parametrize("ticker", ["ABCD", "７２０３", "7203.T"])
def test_invalid_saved_ticker_cannot_enter_frozen_universe(tmp_path, ticker):
    database = tmp_path / "market.sqlite"
    seed_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE jquants_master_snapshots SET ticker=? WHERE ticker='1000'", (ticker,)
        )
    with pytest.raises(
        (ValueError, UniverseSourceDriftError), match=r"ticker|Ticker|security code"
    ):
        frozen_universe(database, ASOF)


def test_frozen_binding_changes_with_selected_membership_and_exact_lake_origin(tmp_path):
    database = tmp_path / "market.sqlite"
    seed_database(database)
    initial_tickers, initial_binding = frozen_universe(database, ASOF)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE jquants_master_snapshots SET ticker='130A' WHERE ticker='1000'")
    changed_tickers, changed_binding = frozen_universe(database, ASOF)
    assert changed_tickers != initial_tickers
    assert changed_binding != initial_binding
    with sqlite3.connect(database) as connection:
        write_lake_store_origin(connection, LakeStoreOrigin("synthetic-new-release", "b" * 64))
    final_tickers, final_binding = frozen_universe(database, ASOF)
    assert final_tickers == changed_tickers
    assert final_binding != changed_binding
    assert json.loads(final_binding)["release_manifest_sha256"] == "b" * 64


def test_actual_receipt_readback_preserves_latest_null_and_both_previous_histories(tmp_path):
    database = tmp_path / "market.sqlite"
    seed_database(database, count=1)
    old_model, old_rating = model(), rating()
    latest_at = RECEIVED + timedelta(hours=1)
    missing_model = model(
        latest_at, consensus_target=None, per_company_forecast=None, per_consensus=None, pbr=None
    )
    missing_rating = rating(latest_at, rating=None)
    assert save_observations(database, [old_model, old_rating, missing_model, missing_rating]) == 4
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    verify_observations(database, [old_model, old_rating, missing_model, missing_rating])
    history = read_observations(database, "7203", latest_at)
    assert history.model_valuation.latest == missing_model
    assert history.model_valuation.previous == old_model
    assert history.analyst_rating.latest == missing_rating
    assert history.analyst_rating.previous == old_rating
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_readback_requires_exact_received_key_even_when_values_are_identical(tmp_path):
    database = tmp_path / "market.sqlite"
    save_observations(database, [model(), rating()])
    for missing in (
        model(RECEIVED + timedelta(microseconds=1)),
        rating(RECEIVED + timedelta(microseconds=1)),
    ):
        with pytest.raises(ValueError, match="receipt readback mismatch"):
            verify_observations(database, [missing])


def test_readback_compares_definition_provenance_and_source_metadata_not_only_numeric_values(
    tmp_path,
):
    database = tmp_path / "market.sqlite"
    save_observations(database, [model(), rating()])
    changed = [
        model(source_url="https://synthetic.invalid/other"),
        model(parser_version="synthetic-2"),
        model(per_consensus_origin="other-origin"),
        model(per_consensus_updated_label=None),
        model(currency="USD"),
        model(share_basis="other-share-basis"),
        model(per_consensus_count=5),
        rating(rating_scale_direction="lower_is_positive"),
        rating(rating_count=5),
        rating(rating_updated_label=None),
    ]
    for item in changed:
        with pytest.raises(ValueError, match="receipt readback mismatch"):
            verify_observations(database, [item])


def test_history_digests_cover_both_tables_and_old_rows_without_mutating_other_tables(tmp_path):
    database = tmp_path / "market.sqlite"
    seed_database(database, count=1)
    other_before = non_retail_digest(database)
    empty = history_digests(database)
    assert set(empty) == {MODEL_VALUATION_TABLE, ANALYST_RATING_TABLE}
    assert set(empty.values()) == {hashlib.sha256(b"").hexdigest()}
    save_observations(database, [model(), rating()])
    initial = history_digests(database)
    save_observations(database, [model(RECEIVED + timedelta(hours=1))])
    later = history_digests(database)
    assert later[MODEL_VALUATION_TABLE] != initial[MODEL_VALUATION_TABLE]
    assert later[ANALYST_RATING_TABLE] == initial[ANALYST_RATING_TABLE]
    with sqlite3.connect(database) as connection:
        updated = connection.execute(
            "UPDATE retail_model_valuation_observations SET per_consensus_origin='changed-old-row' WHERE fetched_at_utc=?",
            (RECEIVED.isoformat(timespec="microseconds"),),
        )
        assert updated.rowcount == 1
    changed_old = history_digests(database)
    assert changed_old[MODEL_VALUATION_TABLE] != later[MODEL_VALUATION_TABLE]
    assert changed_old[ANALYST_RATING_TABLE] == later[ANALYST_RATING_TABLE]
    save_observations(database, [rating(RECEIVED + timedelta(hours=1))])
    changed_rating = history_digests(database)
    assert changed_rating[MODEL_VALUATION_TABLE] == changed_old[MODEL_VALUATION_TABLE]
    assert changed_rating[ANALYST_RATING_TABLE] != changed_old[ANALYST_RATING_TABLE]
    assert non_retail_digest(database) == other_before
    before_read = hashlib.sha256(database.read_bytes()).hexdigest()
    assert history_digests(database) == changed_rating
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before_read
