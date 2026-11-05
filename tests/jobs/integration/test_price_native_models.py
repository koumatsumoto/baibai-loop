"""Native prices reach normal generation and remain independently rebuildable."""

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pyarrow.parquet as pq
import pytest
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.lake_release import market_store
from tests.helpers.master import insert_master_rows
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.price_native import (
    HEAD_KEY,
    PriceAttempt,
    PriceL1,
)
from baibai_engine.machine_analysis.generation.price_inputs import read_price_input
from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.snapshots.reader import materialize_partitions
from baibai_jobs.integration.price_native import model_input, rebuild_saved_input
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.l1_price_export import export_prices_from_native_input

DAY = date(2026, 10, 31)
AT = datetime(2026, 11, 3, tzinfo=UTC)


def attempt(identity, *, day=DAY, at=AT, value=100, partial=False):
    body = json.dumps(
        {
            "data": []
            if value is None
            else [{"Code": "13010", "Date": day.isoformat(), "C": value}],
            **({"pagination_key": "next"} if partial else {}),
        }
    ).encode()
    return PriceAttempt(
        identity, day, at, (JQuantsPage("daily_bars", {"date": day.isoformat()}, at, body),)
    )


def test_saved_price_input_retains_failures_missing_and_empty_across_months(tmp_path):
    store = MemoryR2Store()
    owner = PriceL1(store, tmp_path / "native")
    owner.initialize()
    owner.commit([attempt("first"), attempt("empty", day=DAY + timedelta(days=1), value=None)])
    owner.commit([attempt("partial", at=AT + timedelta(hours=1), partial=True)])
    market = market_store(
        tmp_path / "market.sqlite", daily_bars=(("9999", DAY.isoformat(), 999, 1),)
    )
    fixed = owner.fixed()
    end = DAY + timedelta(days=2)
    saved = model_input(
        store, fixed, as_of=end, evaluated_at=AT + timedelta(hours=1), market=market
    )
    with sqlite3.connect(market) as connection:
        assert connection.execute("SELECT ticker,close FROM jquants_daily_bars").fetchall() == [
            ("1301", 100)
        ]
        assert dict(
            connection.execute(
                "SELECT coverage_start,status FROM source_coverage WHERE source='jquants_daily_bars'"
            )
        ) == {
            DAY.isoformat(): "partial",
            (DAY + timedelta(days=1)).isoformat(): "ok",
            end.isoformat(): "unavailable",
        }
    summary = saved.descriptor["summary"]
    assert summary["failed_days"] == 1
    assert summary["normal_empty_days"] == 1
    assert summary["unavailable_days"] == 1
    assert summary["coverage_status"] == "partial"
    owner.commit([attempt("withdrawn", at=AT + timedelta(hours=2), value=None)])
    del store.values[HEAD_KEY]
    market.unlink()
    store.get_keys.clear()
    counts = rebuild_saved_input(store, saved, start=DAY, end=end, output=tmp_path / "rebuilt")
    assert HEAD_KEY not in store.get_keys
    assert counts["rows"] == 1
    assert counts["failed"] == counts["unavailable"] == 1
    assert pq.read_table(tmp_path / "rebuilt/daily_bars.parquet").to_pylist()[0]["close"] == 100
    selected = read_price_input(store, saved.descriptor, days=(DAY,))
    assert selected["scopes"][DAY.isoformat()]["failure"]
    with pytest.raises(ValueError, match="outside"):
        rebuild_saved_input(
            store, saved, start=DAY - timedelta(days=1), end=end, output=tmp_path / "outside"
        )


def test_new_empty_response_withdraws_all_old_rows(tmp_path):
    store = MemoryR2Store()
    owner = PriceL1(store, tmp_path / "native")
    owner.initialize()
    owner.commit([attempt("first")])
    owner.commit([attempt("withdrawn", at=AT + timedelta(hours=1), value=None)])
    market = market_store(
        tmp_path / "market.sqlite", daily_bars=(("1301", DAY.isoformat(), 100, 1),)
    )
    saved = model_input(
        store, owner.fixed(), as_of=DAY, evaluated_at=AT + timedelta(hours=1), market=market
    )
    with sqlite3.connect(market) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jquants_daily_bars").fetchone()[0] == 0
    assert saved.descriptor["summary"]["normal_empty_days"] == 1
    assert saved.descriptor["summary"]["coverage_status"] == "complete"
    assert (
        rebuild_saved_input(store, saved, start=DAY, end=DAY, output=tmp_path / "rebuilt")["rows"]
        == 0
    )


def test_native_publication_is_derived_from_saved_selection_not_latest_or_source_pages(tmp_path):
    import duckdb

    store = MemoryR2Store()
    owner = PriceL1(store, tmp_path / "native")
    owner.initialize()
    owner.commit([attempt("first"), attempt("next-month", day=DAY + timedelta(days=1), value=110)])
    market = market_store(tmp_path / "market.sqlite")
    saved = model_input(
        store, owner.fixed(), as_of=DAY + timedelta(days=1), evaluated_at=AT, market=market
    )
    del store.values[HEAD_KEY]
    market.unlink()
    exported = export_prices_from_native_input(
        store=store,
        saved=saved,
        mirror_root=tmp_path / "mirror",
        producer_git_commit="a" * 40,
        known_at=AT,
        created_at=AT,
    )
    assert exported.build.manifest.coverage_status == "complete"
    assert len(exported.build.manifest.partitions) == 2
    for partition in exported.build.manifest.partitions:
        assert len(partition.sources) == 1
        source = partition.sources[0]
        assert source.kind == "native_l1"
        assert source.sha256 == saved.descriptor["selection_sha256"]
        assert source.key in store.values
    paths = materialize_partitions(
        LakeObjectCache(root=tmp_path / "mirror", source=LocalMirrorSource(tmp_path / "mirror")),
        dataset=LAKE_DATASETS["jquants.daily_bars"],
        partitions=exported.build.manifest.partitions,
    )
    with duckdb.connect(":memory:") as connection:
        assert connection.execute(
            "SELECT close FROM read_parquet(?) ORDER BY traded_at", [[str(p) for p in paths]]
        ).fetchall() == [(100,), (110,)]


def test_future_confirmation_rejected_before_old_execution_rows_change(tmp_path):
    store = MemoryR2Store()
    owner = PriceL1(store, tmp_path / "native")
    owner.initialize()
    owner.commit([attempt("first")])
    owner.commit([attempt("confirmed", at=AT + timedelta(hours=1))])
    market = market_store(
        tmp_path / "market.sqlite", daily_bars=(("1301", DAY.isoformat(), 999, 1),)
    )
    with pytest.raises(ValueError, match="confirmation state is later"):
        model_input(store, owner.fixed(), as_of=DAY, evaluated_at=AT, market=market)
    with sqlite3.connect(market) as connection:
        assert connection.execute("SELECT close FROM jquants_daily_bars").fetchone()[0] == 999


def test_ordinary_integration_fixes_head_once_and_models_use_native_prices(tmp_path, monkeypatch):
    import baibai_jobs.integration.models as job
    import baibai_jobs.publication.snapshot_publication as publication
    from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
    from baibai_engine.machine_analysis.generation.store import ModelGenerationStore
    from baibai_engine.machine_analysis.screening.policies.economic_paths import EconomicPathPolicy
    from baibai_engine.storage.snapshots.reader import resolve_current_release
    from baibai_jobs.publication.policy import release_policy_for_profile

    monkeypatch.setenv("BAIBAI_PRICE_NATIVE", "true")
    names = ("jquants.daily_bars", "jquants.master_snapshots")
    narrow_release_policy(monkeypatch, datasets=names)
    monkeypatch.setattr(publication, "LAKE_DATASETS", {name: LAKE_DATASETS[name] for name in names})
    monkeypatch.setattr(job, "_load_handle", lambda path: None)
    monkeypatch.setattr(job, "verify_held", lambda *args: None)
    monkeypatch.setattr(publication, "lake_verified_git_commit", lambda: "a" * 40)
    store = MemoryR2Store()
    owner = PriceL1(store, tmp_path / "native")
    owner.initialize()
    owner.commit([attempt("native")])
    fixed = owner.fixed()
    market = market_store(
        tmp_path / "market.sqlite",
        daily_bars=(("1301", DAY.isoformat(), 999, 1),),
        short_sale_reports=(),
    )
    with sqlite3.connect(market) as connection:
        insert_master_rows(
            connection, [(DAY.isoformat(), "1301", "合成企業", "プライム", "卸売業", "011")]
        )
    economic = tmp_path / "economic.sqlite"
    initialize_database(economic).close()
    import baibai_engine.machine_analysis.generation.inputs as generation

    original_reader = generation.read_price_bars
    consumed_prices = []

    def read_prices(*args, **kwargs):
        prices = original_reader(*args, **kwargs)
        consumed_prices.extend(bar.price for bar in prices["1301"])
        return prices

    monkeypatch.setattr(generation, "read_price_bars", read_prices)
    store.get_keys.clear()
    result = job.integrate_models(
        store=store,
        market=market,
        economic=economic,
        runs=tmp_path / "runs",
        mirror=tmp_path / "mirror",
        as_of=DAY,
        lease_handle=tmp_path / "lease",
        growth_start=None,
        policy=EconomicPathPolicy(candidate_limit=36),
    )
    assert consumed_prices == [100]
    assert store.get_keys.count(HEAD_KEY) == 1
    saved = ModelGenerationStore(tmp_path / "runs").read(result["generation_id"])
    assert saved.inputs["price"].descriptor["head_sha256"] == fixed.content_id
    with sqlite3.connect(market) as connection:
        assert connection.execute("SELECT close FROM jquants_daily_bars").fetchone()[0] == 100
    release = resolve_current_release(
        SimpleNamespace(read_bytes=store.get_bytes),
        catalog=LAKE_DATASETS,
        policy=release_policy_for_profile("production"),
    )
    manifest = release.dataset_manifest("jquants.daily_bars")
    assert manifest.partitions[0].sources[0].kind == "native_l1"


def test_held_is_not_complete(tmp_path):
    store = MemoryR2Store()
    owner = PriceL1(store, tmp_path / "native")
    owner.initialize()
    owner.commit([attempt("adopted")])
    owner.commit([attempt("held", value=200)])
    market = market_store(tmp_path / "market.sqlite")
    saved = model_input(store, owner.fixed(), as_of=DAY, evaluated_at=AT, market=market)
    assert saved.descriptor["summary"]["held_days"] == 1
    assert saved.descriptor["summary"]["coverage_status"] == "partial"
    result = rebuild_saved_input(store, saved, start=DAY, end=DAY, output=tmp_path / "held")
    assert result["held"] == 1
    assert result["coverage_status"] == "partial"
    assert pq.read_table(tmp_path / "held/daily_bars.parquet").to_pylist()[0]["close"] == 100


def test_native_refresh_removes_only_daily_price_legacy_originals(tmp_path):
    import gzip

    store = MemoryR2Store()
    owner = PriceL1(store, tmp_path / "native")
    owner.initialize()
    owner.commit([attempt("native")])
    market = market_store(tmp_path / "market.sqlite")
    with sqlite3.connect(market) as connection:
        for source in ("daily_bars", "market_calendar"):
            connection.execute(
                "INSERT INTO jquants_source_pages VALUES (?,?,?,?,?,?,?)",
                (
                    source,
                    0,
                    source,
                    "{}",
                    AT.isoformat(),
                    DAY.isoformat(),
                    gzip.compress(b'{"data":[]}'),
                ),
            )
    model_input(store, owner.fixed(), as_of=DAY, evaluated_at=AT, market=market)
    with sqlite3.connect(market) as connection:
        assert connection.execute("SELECT source FROM jquants_source_pages").fetchall() == [
            ("market_calendar",)
        ]


def test_partial_normalization_never_claims_complete_coverage(tmp_path):
    store = MemoryR2Store()
    owner = PriceL1(store, tmp_path / "native")
    owner.initialize()
    body = json.dumps(
        {
            "data": [
                {"Code": "13010", "Date": DAY.isoformat(), "C": 100},
                {"Date": DAY.isoformat(), "C": 200},
            ]
        }
    ).encode()
    owner.commit(
        [
            PriceAttempt(
                "mixed", DAY, AT, (JQuantsPage("daily_bars", {"date": DAY.isoformat()}, AT, body),)
            )
        ]
    )
    market = market_store(tmp_path / "market.sqlite")
    saved = model_input(store, owner.fixed(), as_of=DAY, evaluated_at=AT, market=market)
    assert saved.descriptor["summary"]["partial_days"] == 1
    assert saved.descriptor["summary"]["coverage_status"] == "partial"
    result = export_prices_from_native_input(
        store=store,
        saved=saved,
        mirror_root=tmp_path / "mirror",
        producer_git_commit="a" * 40,
        known_at=AT,
        created_at=AT,
    )
    assert result.build.manifest.coverage_status == "partial"
    assert result.build.manifest.totals.rows == 1
