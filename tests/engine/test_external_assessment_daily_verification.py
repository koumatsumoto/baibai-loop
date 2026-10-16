from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.master import TSE_33_SECTORS, insert_master_rows
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.external_assessment.acquisition.universe import (
    frozen_lake_universe,
    frozen_universe,
)
from baibai_engine.external_assessment.acquisition.verification import (
    history_digests,
    verify_observations,
)
from baibai_engine.external_assessment.observations.contract import (
    ANALYST_RATING_TABLE,
    MODEL_VALUATION_TABLE,
    QUANTS_SCORE_TABLE,
)
from baibai_engine.external_assessment.observations.models import (
    AnalystRatingObservation,
    ModelValuationObservation,
)
from baibai_engine.external_assessment.reader import read_observations
from baibai_engine.external_assessment.store import save_observations
from baibai_engine.market.jquants_decode import JQuantsProviderError
from baibai_engine.storage.lake.duck import lake_session
from baibai_engine.storage.lake.keys import current_l1_pointer_key
from baibai_engine.storage.lake.models import canonical_lake_model_bytes
from baibai_engine.storage.lake.objects import LakeObjectCache, LakeObjectError, LocalMirrorSource
from baibai_engine.storage.lake.reader import LakeReadError, resolve_current_release
from baibai_engine.storage.lake.release import L1ReleasePointer, create_l1_release
from baibai_engine.storage.lake.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.observations.lake_origin import LakeStoreOrigin, write_lake_store_origin
from baibai_engine.storage.observations.schema import SQLITE_SCHEMA_VERSION, open_connection

ASOF = date(2026, 10, 11)
RECEIVED = datetime(2026, 10, 11, 0, 0, 1, 123456, tzinfo=UTC)
ORIGIN = LakeStoreOrigin("synthetic-fixed-release", "a" * 64)


def seed_database(path, *, count=2500, snapshot=ASOF, origin=True):
    sectors = sorted(TSE_33_SECTORS)
    markets = ("PRIME", "standard", "グロース", "東証一部")
    tickers = tuple(str(1000 + index) for index in range(count))
    connection = open_connection(path)
    try:
        insert_master_rows(
            connection,
            [
                (
                    snapshot.isoformat(),
                    ticker,
                    f"合成会社{index}",
                    markets[index % 4],
                    sectors[index % 33],
                    "011",
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


def build_master_lake(
    tmp_path, monkeypatch, *, snapshot=ASOF, count=2500, mutate=None, calendar=None
):
    """Real contracts and Parquet, plus unrelated data that must never be fetched."""
    datasets = ("jquants.master_snapshots", "jquants.daily_bars")
    if calendar is not None:
        datasets += ("jquants.market_calendar",)
    narrow_release_policy(
        monkeypatch, datasets=datasets, minimum_population_count=None if calendar is not None else 1
    )
    database = tmp_path / "market.sqlite"
    seed_database(database, snapshot=snapshot, count=count)
    with closing(sqlite3.connect(database)) as connection, connection:
        insert_master_rows(
            connection,
            [
                (snapshot.isoformat(), "130A", "合成英字会社", "PRIME", "電気機器", "011"),
                (snapshot.isoformat(), "9401", "合成ETF", "PRIME", "その他", "014"),
                (snapshot.isoformat(), "9402", "合成PRO", "TOKYO PRO MARKET", "電気機器", "011"),
                (snapshot.isoformat(), "9403", "合成市場外", "RESIDUAL", "電気機器", "011"),
                ("2025-12-31", "130A", "合成旧年会社", "PRIME", "電気機器", "011"),
            ],
        )
        connection.execute(
            "INSERT INTO jquants_daily_bars(ticker,traded_at,close,volume) VALUES (?,?,?,?)",
            ("130A", "2026-09-30", 1, 1),
        )
        if calendar is not None:
            connection.executemany("INSERT INTO jquants_market_calendar VALUES (?,?)", calendar)
            connection.execute(
                "INSERT INTO source_coverage(source,coverage_key,coverage_start,coverage_end,"
                "fetched_at_utc,record_count,status) VALUES (?,?,?,?,?,?,?)",
                (
                    "jquants_market_calendar",
                    "synthetic-range",
                    min(day for day, _ in calendar),
                    max(day for day, _ in calendar),
                    RECEIVED.isoformat(),
                    len(calendar),
                    "ok",
                ),
            )
        if mutate is not None:
            mutate(connection)
    mirror = tmp_path / "published"
    with sealed_sqlite_snapshot(sqlite_path=database, mirror_root=mirror) as snapshot_source:
        manifests = [
            export_dataset_snapshot(
                dataset_name=name,
                mirror_root=mirror,
                producer_git_commit="a" * 40,
                source_snapshot=snapshot_source,
                build_id=f"synthetic-{name.replace('.', '-')}",
                created_at=RECEIVED,
            ).manifest_path
            for name in datasets
        ]
    path, release = create_l1_release(
        dataset_manifest_paths=manifests,
        mirror_root=mirror,
        release_id="synthetic-master-only",
        created_at=RECEIVED,
    )
    pointer = L1ReleasePointer(
        release_id=release.release_id,
        manifest_key=path.relative_to(mirror).as_posix(),
        manifest_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    pointer_path = mirror / current_l1_pointer_key()
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.write_bytes(canonical_lake_model_bytes(pointer))
    fixed = resolve_current_release(LocalMirrorSource(mirror))
    store = MemoryR2Store()
    for item in mirror.rglob("*"):
        if item.is_file():
            store.store_object(item.relative_to(mirror).as_posix(), item.read_bytes())
    return store, fixed, database


class MasterOnlySource:
    def __init__(self, store):
        self.store = store

    def read_bytes(self, key):
        assert "jquants.master_snapshots/" in key
        assert "/year=2026/" in key
        return self.store.get_bytes(key)

    def uri(self, key):
        raise AssertionError("master objects must pass the digest-verifying cache")


def test_lake_master_only_keeps_all_sectors_alpha_and_exact_release_binding(tmp_path, monkeypatch):
    store, release, database = build_master_lake(tmp_path, monkeypatch)
    cache = LakeObjectCache(root=tmp_path / "cache", source=MasterOnlySource(store))
    with lake_session() as session:
        tickers, binding = frozen_lake_universe(
            session, release=release, cache=cache, master_asof=ASOF
        )
        assert tickers == tuple(sorted((*map(str, range(1000, 3500)), "130A")))
        assert json.loads(binding) == {
            "master_asof": ASOF.isoformat(),
            "release_id": release.release_id,
            "release_manifest_sha256": release.manifest_sha256,
            "tickers_sha256": hashlib.sha256("\n".join(tickers).encode()).hexdigest(),
        }
        first_reads = tuple(store.get_keys)
        assert first_reads
        assert all("jquants.master_snapshots/" in key for key in first_reads)
        assert all("/year=2026/" in key for key in first_reads)
        assert frozen_lake_universe(session, release=release, cache=cache, master_asof=ASOF) == (
            tickers,
            binding,
        )
        assert tuple(store.get_keys) == first_reads
    with closing(sqlite3.connect(database)) as connection, connection:
        write_lake_store_origin(
            connection, LakeStoreOrigin(release.release_id, release.manifest_sha256)
        )
    assert frozen_universe(database, ASOF) == (tickers, binding)


@pytest.mark.parametrize("saved", [ASOF - timedelta(days=1), ASOF + timedelta(days=1)])
def test_lake_master_never_falls_back_to_another_saved_date(tmp_path, monkeypatch, saved):
    # Keep both dates historical for this boundary, independently of today's clock.
    store, release, _ = build_master_lake(
        tmp_path, monkeypatch, snapshot=saved - timedelta(days=10)
    )
    with lake_session() as session, pytest.raises(ValueError, match="exact saved master"):
        frozen_lake_universe(
            session,
            release=release,
            cache=LakeObjectCache(root=tmp_path / "cache", source=MasterOnlySource(store)),
            master_asof=ASOF - timedelta(days=10),
        )


@pytest.mark.parametrize("future_location", ["request", "saved_master"])
def test_lake_future_master_rejected_before_any_parquet_get(tmp_path, monkeypatch, future_location):
    future = datetime.now(ZoneInfo("Asia/Tokyo")).date() + timedelta(days=1)
    store, release, _ = build_master_lake(
        tmp_path, monkeypatch, snapshot=future if future_location == "saved_master" else ASOF
    )
    with lake_session() as session, pytest.raises(ValueError, match="cannot be in the future"):
        frozen_lake_universe(
            session,
            release=release,
            cache=LakeObjectCache(root=tmp_path / "cache", source=MasterOnlySource(store)),
            master_asof=future if future_location == "request" else ASOF,
        )
    assert store.get_keys == []


@pytest.mark.parametrize(
    ("mutation", "error", "message"),
    [
        (
            "UPDATE jquants_master_snapshots SET record_json=json_set(record_json,'$.ProdCat','014')",
            ValueError,
            "common-stock",
        ),
        (
            "UPDATE jquants_master_snapshots SET record_json=json_set(record_json,'$.ProdCat',NULL) WHERE security_code='10000'",
            JQuantsProviderError,
            "product category",
        ),
        (
            "UPDATE jquants_master_snapshots SET record_json=json_set(record_json,'$.Code','ABCD') WHERE security_code='10000'",
            ValueError,
            "ticker",
        ),
    ],
)
def test_lake_saved_master_classification_and_ascii_code_are_required(
    tmp_path, monkeypatch, mutation, error, message
):
    store, release, _ = build_master_lake(
        tmp_path, monkeypatch, mutate=lambda connection: connection.execute(mutation)
    )
    with lake_session() as session, pytest.raises(error, match=message):
        frozen_lake_universe(
            session,
            release=release,
            cache=LakeObjectCache(root=tmp_path / "cache", source=MasterOnlySource(store)),
            master_asof=ASOF,
        )


def test_lake_corrupt_master_cannot_form_universe_and_other_data_is_not_fetched(
    tmp_path, monkeypatch
):
    store, release, _ = build_master_lake(tmp_path, monkeypatch)
    master = next(
        key
        for key in store.values
        if key.endswith(".parquet") and "/year=2026/" in key and "jquants.master_snapshots/" in key
    )
    original = store.values[master].body
    store.store_object(master, bytes([original[0] ^ 1]) + original[1:])
    with lake_session() as session, pytest.raises(LakeObjectError, match="digest differs"):
        frozen_lake_universe(
            session,
            release=release,
            cache=LakeObjectCache(root=tmp_path / "cache", source=MasterOnlySource(store)),
            master_asof=ASOF,
        )
    assert store.get_keys == [master]


def test_lake_missing_master_year_rejects_without_reading_another_year(tmp_path, monkeypatch):
    store, release, _ = build_master_lake(tmp_path, monkeypatch)
    with lake_session() as session, pytest.raises(LakeReadError, match="does not publish"):
        frozen_lake_universe(
            session,
            release=release,
            cache=LakeObjectCache(root=tmp_path / "cache", source=MasterOnlySource(store)),
            master_asof=date(2024, 10, 11),
        )
    assert store.get_keys == []


@pytest.mark.parametrize("count", [2000, 2498])
def test_lake_master_requires_both_saved_population_and_selected_common_stock_floor(
    tmp_path, monkeypatch, count
):
    store, release, _ = build_master_lake(tmp_path, monkeypatch, count=count)
    with lake_session() as session, pytest.raises(ValueError, match="complete"):
        frozen_lake_universe(
            session,
            release=release,
            cache=LakeObjectCache(root=tmp_path / "cache", source=MasterOnlySource(store)),
            master_asof=ASOF,
        )


def test_unsupported_master_contract_is_rejected_before_any_parquet_get(tmp_path, monkeypatch):
    store, release, _ = build_master_lake(tmp_path, monkeypatch)
    name = "jquants.master_snapshots"
    current = release.dataset_manifest(name)
    release = replace(
        release,
        dataset_manifests={
            **release.dataset_manifests,
            name: current.model_copy(update={"contract_version": current.contract_version + 1}),
        },
    )
    with lake_session() as session, pytest.raises(LakeReadError, match="accepts only"):
        frozen_lake_universe(
            session,
            release=release,
            cache=LakeObjectCache(root=tmp_path / "cache", source=MasterOnlySource(store)),
            master_asof=ASOF,
        )
    assert store.get_keys == []


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


def other_dataset_digest(path):
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as connection:
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
    with closing(sqlite3.connect(database)) as connection, connection:
        insert_master_rows(
            connection,
            [
                (ASOF.isoformat(), "130A", "合成英字会社", "PRIME", "電気機器", "011"),
                (ASOF.isoformat(), "9401", "合成ETF", "PRIME", "その他", "014"),
                (ASOF.isoformat(), "9402", "合成PRO", "TOKYO PRO MARKET", "電気機器", "011"),
                (ASOF.isoformat(), "9403", "合成市場外", "RESIDUAL", "電気機器", "011"),
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
    with closing(sqlite3.connect(database)) as connection, connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SQLITE_SCHEMA_VERSION


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
    with closing(sqlite3.connect(database)) as connection, connection:
        if classification == "その他":
            connection.execute(
                "UPDATE jquants_master_snapshots SET record_json=json_set(record_json,'$.ProdCat','014') WHERE security_code <> '10000'"
            )
        else:
            connection.execute(
                "UPDATE jquants_master_snapshots SET record_json=json_set(record_json,'$.MktNm','TOKYO PRO MARKET') WHERE security_code <> '10000'"
            )
    with pytest.raises(ValueError, match=r"common.stock|common stock|production floor"):
        frozen_universe(database, ASOF)


def test_missing_product_category_cannot_be_inferred_from_sector(tmp_path):
    database = tmp_path / "market.sqlite"
    seed_database(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "UPDATE jquants_master_snapshots SET record_json=json_set(record_json,'$.ProdCat',NULL) WHERE security_code='10000'"
        )
    with pytest.raises(JQuantsProviderError, match="product category"):
        frozen_universe(database, ASOF)


@pytest.mark.parametrize("ticker", ["ABCD", "７２０３", "7203.T"])
def test_invalid_saved_ticker_cannot_enter_frozen_universe(tmp_path, ticker):
    database = tmp_path / "market.sqlite"
    seed_database(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "UPDATE jquants_master_snapshots SET record_json=json_set(record_json,'$.Code',?) WHERE security_code='10000'",
            (ticker,),
        )
    with pytest.raises(
        (ValueError, JQuantsProviderError), match=r"ticker|Ticker|security code|J-Quants code"
    ):
        frozen_universe(database, ASOF)


def test_frozen_binding_changes_with_selected_membership_and_exact_lake_origin(tmp_path):
    database = tmp_path / "market.sqlite"
    seed_database(database)
    initial_tickers, initial_binding = frozen_universe(database, ASOF)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "UPDATE jquants_master_snapshots SET security_code='130A0', record_json=json_set(record_json,'$.Code','130A0') WHERE security_code='10000'"
        )
    changed_tickers, changed_binding = frozen_universe(database, ASOF)
    assert changed_tickers != initial_tickers
    assert changed_binding != initial_binding
    with closing(sqlite3.connect(database)) as connection, connection:
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
    assert history.model_valuations[0].latest == missing_model
    assert history.model_valuations[0].previous == old_model
    assert history.analyst_ratings[0].latest == missing_rating
    assert history.analyst_ratings[0].previous == old_rating
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
    other_before = other_dataset_digest(database)
    empty = history_digests(database)
    assert set(empty) == {MODEL_VALUATION_TABLE, ANALYST_RATING_TABLE, QUANTS_SCORE_TABLE}
    assert set(empty.values()) == {hashlib.sha256(b"").hexdigest()}
    save_observations(database, [model(), rating()])
    initial = history_digests(database)
    save_observations(database, [model(RECEIVED + timedelta(hours=1))])
    later = history_digests(database)
    assert later[MODEL_VALUATION_TABLE] != initial[MODEL_VALUATION_TABLE]
    assert later[ANALYST_RATING_TABLE] == initial[ANALYST_RATING_TABLE]
    with closing(sqlite3.connect(database)) as connection, connection:
        updated = connection.execute(
            "UPDATE external_assessment_model_valuation_observations SET per_consensus_origin='changed-old-row' WHERE fetched_at_utc=?",
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
    assert other_dataset_digest(database) == other_before
    before_read = hashlib.sha256(database.read_bytes()).hexdigest()
    assert history_digests(database) == changed_rating
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before_read
