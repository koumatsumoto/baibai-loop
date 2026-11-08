"""Native calendar publication shares the seal and reuses unchanged monthly objects."""

import json
import sqlite3
from datetime import date, timedelta
from types import SimpleNamespace

import pyarrow.parquet as pq
import pytest
from tests.engine.data_collection.providers.jquants.test_calendar_native import (
    AT,
    DAY,
    attempt,
    owner,
)
from tests.helpers.edinet_native import empty_edinet_input
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.lake_release import market_store
from tests.jobs.integration.test_master_native_models import attempt as master_attempt
from tests.jobs.integration.test_master_native_models import records

from baibai_engine.data_collection.providers.jquants.calendar_native import HEAD_KEY
from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.financial_native import (
    FinancialAttempt,
    FinancialL1,
)
from baibai_engine.data_collection.providers.jquants.master_native import MasterL1
from baibai_engine.data_collection.providers.jquants.price_native import PriceAttempt, PriceL1
from baibai_engine.machine_analysis.generation.query import read_generation
from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.snapshots.reader import materialize_partitions, resolve_current_release
from baibai_engine.storage.snapshots.writer import sealed_sqlite_snapshot
from baibai_jobs.integration.calendar_native import model_input
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.l1_calendar_export import export_calendar_from_native_input
from baibai_jobs.publication.policy import release_policy_for_profile


def native_dependencies(store, tmp_path, *, day=DAY, holiday=None):
    master = MasterL1(store, tmp_path / "master")
    master.initialize()
    master.commit([master_attempt("master", day=day, at=AT)])
    if holiday is not None:
        holiday_rows = records(holiday)
        for row in holiday_rows:
            row["S33Nm"] = "銀行業"
        master.commit([master_attempt("holiday", day=holiday, at=AT, rows=holiday_rows)])
    price = PriceL1(store, tmp_path / "price")
    price.initialize()
    body = json.dumps({"data": [{"Code": "13010", "Date": day.isoformat(), "C": 100}]}).encode()
    price.commit(
        [
            PriceAttempt(
                "price", day, AT, (JQuantsPage("daily_bars", {"date": day.isoformat()}, AT, body),)
            )
        ]
    )
    if holiday is not None:
        price.commit(
            [
                PriceAttempt(
                    "closed-" + closed.isoformat(),
                    closed,
                    AT,
                    (JQuantsPage("daily_bars", {"date": closed.isoformat()}, AT, b'{"data":[]}'),),
                )
                for closed in (day + timedelta(days=1), holiday)
            ]
        )
    financial = FinancialL1(store, tmp_path / "financial")
    financial.initialize()
    financial.commit(
        [
            FinancialAttempt(
                "financial",
                day,
                AT,
                (
                    JQuantsPage(
                        "financial_summaries", {"date": day.isoformat()}, AT, b'{"data":[]}'
                    ),
                ),
            )
        ]
    )


def configure(monkeypatch):
    import baibai_jobs.publication.snapshot_publication as publication

    names = ("jquants.daily_bars", "jquants.master_snapshots", "jquants.market_calendar")
    narrow_release_policy(monkeypatch, datasets=names)
    monkeypatch.setattr(publication, "LAKE_DATASETS", {name: LAKE_DATASETS[name] for name in names})
    monkeypatch.setattr(publication, "lake_verified_git_commit", lambda: "a" * 40)
    return publication


def test_month_objects_reuse_equal_and_correct_only_changed_month_from_same_seal(tmp_path):
    store, source = owner(tmp_path / "native")
    earlier = date(2025, 10, 1)
    prior = date(2026, 10, 1)
    source.commit([attempt("earlier", earlier), attempt("prior", prior), attempt("current")])
    market = market_store(tmp_path / "market.sqlite")
    mirror = tmp_path / "mirror"
    changes, manifests = [], []
    for received in (
        None,
        attempt("equal", hour=1),
        attempt("new", DAY + timedelta(days=1), hour=2),
        attempt("correction", prior, hour=3, rows=[{"Date": prior.isoformat(), "HolDiv": "0"}]),
    ):
        if received:
            source.commit([received])
        saved = model_input(
            store, source.fixed(), as_of=DAY, evaluated_at=AT + timedelta(hours=3), market=market
        )
        with sealed_sqlite_snapshot(sqlite_path=market, mirror_root=mirror) as seal:
            store.get_keys.clear()
            with sqlite3.connect(market) as connection:
                connection.execute("DELETE FROM jquants_market_calendar")
            report = export_calendar_from_native_input(
                source_snapshot=seal, saved=saved, mirror_root=mirror, producer_git_commit="a" * 40
            )
            assert store.get_keys == []
        changes.append(report.new_objects)
        manifests.append(report.manifest)
    assert changes == [3, 0, 1, 1]
    first = {
        obj.min_key[0][:7]: obj.key for part in manifests[0].partitions for obj in part.objects
    }
    final = {
        obj.min_key[0][:7]: obj.key for part in manifests[-1].partitions for obj in part.objects
    }
    assert final["2025-10"] == first["2025-10"]
    assert final["2026-10"] != first["2026-10"]
    assert manifests[-1].contract_version == 2
    assert manifests[-1].partition_by == ("year",)
    source_ref = manifests[-1].partitions[0].sources[0]
    assert source_ref.namespace == "jquants-market-calendar"
    assert source_ref.sha256 == saved.descriptor["selection_sha256"]
    assert source_ref.key in store.values
    paths = materialize_partitions(
        LakeObjectCache(root=tmp_path / "cold", source=LocalMirrorSource(mirror)),
        dataset=LAKE_DATASETS["jquants.market_calendar"],
        partitions=manifests[-1].partitions,
    )
    assert sum(len(pq.read_table(path)) for path in paths) == 4


def test_generation_materializes_calendar_before_master_and_preserves_identity(
    tmp_path, monkeypatch
):
    import baibai_jobs.integration.models as job
    from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
    from baibai_engine.machine_analysis.screening.policies.economic_paths import EconomicPathPolicy

    publication = configure(monkeypatch)
    monkeypatch.setattr(job, "_load_handle", lambda _path: None)
    monkeypatch.setattr(job, "verify_held", lambda *_args: None)
    friday, saturday, sunday = date(2026, 10, 30), date(2026, 10, 31), date(2026, 11, 1)
    store, source = owner(tmp_path / "native")
    source.commit(
        [
            attempt(
                "calendar",
                friday,
                sunday,
                rows=[
                    {"Date": friday.isoformat(), "HolDiv": "1"},
                    {"Date": saturday.isoformat(), "HolDiv": "0"},
                    {"Date": sunday.isoformat(), "HolDiv": "0"},
                ],
            )
        ]
    )
    native_dependencies(store, tmp_path, day=friday, holiday=sunday)
    market = market_store(tmp_path / "market.sqlite")
    with sqlite3.connect(market) as connection:
        connection.execute(
            "INSERT INTO jquants_market_calendar VALUES (?,?)", (sunday.isoformat(), "1")
        )
    economic = tmp_path / "economic.sqlite"
    initialize_database(economic).close()
    fixed = source.fixed()
    store.get_keys.clear()
    result = job.integrate_models(
        store=store,
        edinet_input=empty_edinet_input(store, tmp_path),
        market=market,
        economic=economic,
        runs=tmp_path / "runs",
        mirror=tmp_path / "mirror",
        as_of=sunday,
        lease_handle=tmp_path / "lease",
        growth_start=None,
        policy=EconomicPathPolicy(candidate_limit=36),
    )
    saved = read_generation(result["generation_id"], path=tmp_path / "runs")
    assert saved.inputs["calendar"].descriptor["head_sha256"] == fixed.content_id
    assert saved.inputs["master"].descriptor["summary"]["master_basis_date"] == friday.isoformat()
    assert saved.inputs["master"].descriptor["summary"]["snapshot_date"] == friday.isoformat()
    assert store.get_keys.count(HEAD_KEY) == 1
    release = resolve_current_release(
        SimpleNamespace(read_bytes=store.get_bytes),
        catalog=publication.LAKE_DATASETS,
        policy=release_policy_for_profile("production"),
    )
    calendar = release.dataset_manifest("jquants.market_calendar")
    assert calendar.totals.rows == 3
    assert (
        calendar.partitions[0].sources[0].sha256
        == saved.inputs["calendar"].descriptor["selection_sha256"]
    )


@pytest.mark.parametrize("nearby_bad", [False, True])
def test_partial_native_calendar_uses_exact_safe_days_for_fixed_financial_basis(
    tmp_path, monkeypatch, nearby_bad
):
    from baibai_engine.machine_analysis.observations.financials.fixed_financial_reader import (
        read_fixed_financial_documents,
    )
    from baibai_engine.machine_analysis.observations.jquants_writer import (
        store_jquants_fin_summaries,
    )
    from baibai_engine.storage.snapshots.release import create_release
    from baibai_engine.storage.snapshots.sources import sha256_file
    from baibai_engine.storage.snapshots.writer import export_dataset_snapshot
    from baibai_engine.storage.sqlite.source_cache.lake_origin import LakeStoreOrigin
    from baibai_jobs.integration.master_native import model_input as master_model_input
    from baibai_jobs.publication.l1_master_export import export_master_from_native_input

    friday, saturday, sunday = date(2026, 10, 30), date(2026, 10, 31), date(2026, 11, 1)
    store, source = owner(tmp_path / "native")
    source.commit(
        [
            attempt("distant-invalid", date(2025, 1, 1), rows=[]),
            attempt(
                "current",
                friday,
                sunday,
                rows=[
                    {"Date": friday.isoformat(), "HolDiv": "1"},
                    {"Date": saturday.isoformat(), "HolDiv": "0"},
                    {"Date": sunday.isoformat(), "HolDiv": "0"},
                ],
            ),
        ]
    )
    if nearby_bad:
        source.commit(
            [
                attempt(
                    "nearby-invalid",
                    sunday,
                    hour=1,
                    rows=[{"Date": sunday.isoformat(), "HolDiv": "FUTURE"}],
                )
            ]
        )
    native_dependencies(store, tmp_path, day=friday, holiday=sunday)
    market = market_store(tmp_path / "market.sqlite")
    calendar = model_input(
        store, source.fixed(), as_of=sunday, evaluated_at=AT + timedelta(hours=1), market=market
    )
    master = master_model_input(
        store,
        MasterL1(store, tmp_path / "master").fixed(),
        as_of=sunday,
        evaluated_at=AT + timedelta(hours=1),
        market=market,
    )
    names = ("jquants.fin_summaries", "jquants.master_snapshots", "jquants.market_calendar")
    narrow_release_policy(monkeypatch, datasets=names)
    store_jquants_fin_summaries(
        market,
        [{"Code": "13010", "DiscDate": friday.isoformat(), "DiscNo": "fixed"}],
        requested_start=friday,
        requested_end=friday,
    )
    mirror = tmp_path / "mirror"
    with sealed_sqlite_snapshot(sqlite_path=market, mirror_root=mirror) as snapshot:
        reports = [
            export_dataset_snapshot(
                dataset=LAKE_DATASETS["jquants.fin_summaries"],
                source_snapshot=snapshot,
                mirror_root=mirror,
                producer_git_commit="a" * 40,
            ),
            export_master_from_native_input(
                source_snapshot=snapshot,
                saved=master,
                mirror_root=mirror,
                producer_git_commit="a" * 40,
            ),
            export_calendar_from_native_input(
                source_snapshot=snapshot,
                saved=calendar,
                mirror_root=mirror,
                producer_git_commit="a" * 40,
            ),
        ]
        assert reports[-1].manifest.coverage_status == "partial"
    path, release = create_release(
        dataset_manifest_paths=[report.manifest_path for report in reports],
        mirror_root=mirror,
        release_id="fixed",
        catalog=LAKE_DATASETS,
        policy=release_policy_for_profile("production"),
    )
    origin = LakeStoreOrigin(
        release_id=release.release_id, release_manifest_sha256=sha256_file(path)
    )
    market.unlink()
    store.values.clear()
    result = read_fixed_financial_documents(mirror, origin=origin, ticker="1301", as_of=sunday)
    assert result.sector == ("銀行業" if nearby_bad else "卸売業")


def test_partial_calendar_policy_does_not_relax_legacy_release(tmp_path, monkeypatch):
    from baibai_engine.storage.snapshots.release import create_release
    from baibai_engine.storage.snapshots.writer import export_dataset_snapshot

    store, source = owner(tmp_path / "native")
    source.commit(
        [
            attempt("valid"),
            attempt("invalid", DAY + timedelta(days=1), rows=[]),
            attempt("later", DAY + timedelta(days=2)),
        ]
    )
    market = market_store(tmp_path / "market.sqlite")
    saved = model_input(store, source.fixed(), as_of=DAY, evaluated_at=AT, market=market)
    narrow_release_policy(monkeypatch, datasets=("jquants.market_calendar",))
    mirror = tmp_path / "mirror"
    with sealed_sqlite_snapshot(sqlite_path=market, mirror_root=mirror) as seal:
        native = export_calendar_from_native_input(
            source_snapshot=seal, saved=saved, mirror_root=mirror, producer_git_commit="a" * 40
        )
        legacy = export_dataset_snapshot(
            dataset=LAKE_DATASETS["jquants.market_calendar"],
            source_snapshot=seal,
            mirror_root=mirror,
            producer_git_commit="a" * 40,
        )
    assert native.manifest.coverage_status == legacy.manifest.coverage_status == "partial"
    create_release(
        dataset_manifest_paths=[native.manifest_path],
        mirror_root=mirror,
        release_id="native",
        catalog=LAKE_DATASETS,
        policy=release_policy_for_profile("production"),
    )
    with pytest.raises(ValueError, match="requires complete"):
        create_release(
            dataset_manifest_paths=[legacy.manifest_path],
            mirror_root=mirror,
            release_id="legacy",
            catalog=LAKE_DATASETS,
            policy=release_policy_for_profile("production"),
        )
