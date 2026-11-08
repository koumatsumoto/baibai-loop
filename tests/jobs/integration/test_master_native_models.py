"""Master generation rebuilds sparse history from native L1, never previous L2."""

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pyarrow.parquet as pq
import pytest
from tests.helpers.edinet_native import empty_edinet_input
from tests.helpers.financial_native import seed_empty_financial
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.lake_release import market_store
from tests.helpers.master import insert_master_rows
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.master_native import (
    HEAD_KEY,
    MasterAttempt,
    MasterL1,
)
from baibai_engine.machine_analysis.generation.master_inputs import read_master_input
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.machine_analysis.observations.stocks.reader import (
    read_eq_master_asof,
    read_eq_master_exact,
    read_master_snapshot,
)
from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.snapshots.reader import materialize_partitions
from baibai_engine.storage.snapshots.writer import sealed_sqlite_snapshot
from baibai_jobs.integration.master_native import (
    fixed_input,
    materialize_saved_input,
    model_input,
    rebuild,
    rebuild_saved_input,
)
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.l1_master_export import export_master_from_native_input

DAY = date(2026, 10, 1)
NEXT = date(2026, 11, 1)
AT = datetime(2026, 11, 3, tzinfo=UTC)


def records(day=DAY):
    rows = [
        {
            "Date": day.isoformat(),
            "Code": f"{code}0",
            "CoName": f"合成企業{code}",
            "MktNm": "プライム" if code == 1301 else "OTHER",
            "S33Nm": "卸売業",
            "ProdCat": "011",
        }
        for code in range(1000, 3500)
    ]
    for code, category in (("80000", "ETF"), ("80010", "REIT"), ("13011", "011")):
        rows.append({**rows[0], "Code": code, "ProdCat": category, "UnknownNative": {"x": None}})
    return rows


def attempt(identity, *, day=DAY, at=AT, rows=None, partial=False):
    payload = {"data": records(day) if rows is None else rows}
    if partial:
        payload["pagination_key"] = "next"
    return MasterAttempt(
        identity,
        day,
        at,
        (
            JQuantsPage(
                "master", {"date": day.strftime("%Y%m%d")}, at, json.dumps(payload).encode()
            ),
        ),
    )


def setup_native(tmp_path, attempts):
    store = MemoryR2Store()
    owner = MasterL1(store, tmp_path / "native")
    owner.initialize()
    owner.commit(attempts)
    market = market_store(tmp_path / "market.sqlite")
    with sqlite3.connect(market) as connection:
        insert_master_rows(
            connection, [(DAY.isoformat(), "9999", "stale999", "プライム", "情報・通信業", "011")]
        )
    return store, owner, market


def stored_rows(market):
    with sqlite3.connect(market) as connection:
        return connection.execute("SELECT * FROM jquants_master_snapshots ORDER BY 1,2").fetchall()


def test_sparse_history_all_products_and_unknown_fields_survive_cold_rebuild(tmp_path):
    store, owner, market = setup_native(tmp_path, [attempt("first"), attempt("next", day=NEXT)])
    store.get_keys.clear()
    fixed = owner.fixed()
    saved = model_input(
        store, fixed, as_of=NEXT + timedelta(days=1), evaluated_at=AT, market=market
    )
    pack_reads = [key for key in store.get_keys if "/master/v1/packs/" in key]
    assert len(pack_reads) == len(set(pack_reads)) == 1
    assert len(stored_rows(market)) == 5006
    assert not any(code == "99990" for _, code, _ in stored_rows(market))
    with sqlite3.connect(market) as connection:
        assert connection.execute(
            "SELECT coverage_start,status FROM source_coverage WHERE source='jquants_master_snapshots' ORDER BY 1"
        ).fetchall() == [(DAY.isoformat(), "ok"), (NEXT.isoformat(), "ok")]
    assert read_eq_master_exact(market, DAY) is not None
    assert read_eq_master_asof(market, NEXT + timedelta(days=1)).status == "prior_snapshot"
    assert read_master_snapshot(market, as_of=DAY - timedelta(days=1)) is None
    selected = read_master_input(store, saved.descriptor, days=(DAY, DAY + timedelta(days=1), NEXT))
    assert set(selected["scopes"]) == {DAY.isoformat(), NEXT.isoformat()}
    assert saved.descriptor["summary"]["snapshot_days"] == 2
    assert saved.descriptor["summary"]["snapshot_status"] == "prior_snapshot"
    assert "scopes" not in saved.descriptor
    original = stored_rows(market)
    del store.values[HEAD_KEY]
    market.unlink()
    store.get_keys.clear()
    result = rebuild_saved_input(store, saved, start=DAY, end=NEXT, output=tmp_path / "cold")
    assert HEAD_KEY not in store.get_keys
    assert result["snapshot_days"] == 2
    assert result["unavailable_days"] == 0
    assert result["rows"] == 5006
    cold = pq.read_table(tmp_path / "cold/master_snapshots.parquet").to_pylist()
    assert [tuple(row.values()) for row in cold] == original
    secondary = next(
        json.loads(row["record_json"]) for row in cold if row["security_code"] == "13011"
    )
    assert secondary["UnknownNative"] == {"x": None}
    assert len(pq.read_table(tmp_path / "cold/master_receipt_status.parquet")) == 2
    assert not (tmp_path / "cold/head.json").exists()
    with pytest.raises(ValueError, match="outside"):
        rebuild_saved_input(
            store, saved, start=DAY - timedelta(days=1), end=NEXT, output=tmp_path / "outside"
        )


def test_corrections_withdraw_codes_and_change_classification_without_old_l2(tmp_path):
    store, owner, market = setup_native(tmp_path, [attempt("first"), attempt("next", day=NEXT)])
    corrected = [row for row in records(NEXT) if row["Code"] != "80010"]
    for row in corrected:
        if row["Code"] == "13010":
            row["ProdCat"] = "ETF"
            row["S33Nm"] = "情報・通信業"
    corrected.append({**corrected[0], "Code": "90000"})  # Still 2500 common primary lines.
    owner.commit([attempt("corrected", day=NEXT, at=AT + timedelta(hours=1), rows=corrected)])
    model_input(
        store, owner.fixed(), as_of=NEXT, evaluated_at=AT + timedelta(hours=1), market=market
    )
    prior, current = (
        read_master_snapshot(market, as_of=DAY),
        read_master_snapshot(market, as_of=NEXT),
    )
    assert prior.securities["1301"].is_common_stock
    assert not current.securities["1301"].is_common_stock
    assert current.securities["1301"].sector_33 == "情報・通信業"
    assert "8001" in prior.securities
    assert "8001" not in current.securities
    assert len(stored_rows(market)) == 5006


@pytest.mark.parametrize(
    "invalid", ["empty", "truncated", "wrong-date", "duplicate", "missing-field"]
)
def test_invalid_adopted_master_never_replaces_previous_execution_rows(tmp_path, invalid):
    store, owner, market = setup_native(tmp_path, [attempt("first")])
    rows = records()
    if invalid == "empty":
        rows = []
    elif invalid == "truncated":
        rows = rows[:2499]
    elif invalid == "wrong-date":
        rows[-1]["Date"] = NEXT.isoformat()
    elif invalid == "duplicate":
        rows.append(dict(rows[0]))
    else:
        del rows[-1]["ProdCat"]
    owner.commit([attempt("invalid", at=AT + timedelta(hours=1), rows=rows)])
    old = stored_rows(market)
    with pytest.raises(JQuantsProviderError):
        model_input(
            store, owner.fixed(), as_of=DAY, evaluated_at=AT + timedelta(hours=1), market=market
        )
    assert stored_rows(market) == old
    assert owner.fixed().value()["scopes"][DAY.isoformat()]["adopted"] == "invalid"


@pytest.mark.parametrize("held", [False, True])
def test_newer_failed_or_held_keeps_native_adopted_with_explicit_partial(tmp_path, held):
    store, owner, market = setup_native(tmp_path, [attempt("first")])
    changed = records()
    changed[0]["CoName"] = "changed"
    owner.commit(
        [
            attempt(
                "unresolved",
                at=AT if held else AT + timedelta(hours=1),
                rows=changed,
                partial=not held,
            )
        ]
    )
    saved = model_input(
        store, owner.fixed(), as_of=DAY, evaluated_at=AT + timedelta(hours=1), market=market
    )
    assert json.loads(stored_rows(market)[0][2])["CoName"] != "changed"
    assert read_eq_master_exact(market, DAY) is None
    assert read_eq_master_asof(market, DAY).status == "exact_date"
    assert saved.descriptor["summary"]["coverage_status"] == "partial"
    assert saved.descriptor["summary"]["held_days" if held else "failed_days"] == 1
    result = rebuild_saved_input(store, saved, start=DAY, end=DAY, output=tmp_path / "cold")
    assert result["rows"] == 2503
    assert result["partial_days"] == 1


def test_future_confirmation_rejected_before_any_projection_change(tmp_path):
    store, owner, market = setup_native(tmp_path, [attempt("first")])
    owner.commit([attempt("confirmed", at=AT + timedelta(hours=1))])
    previous = stored_rows(market)
    with pytest.raises(ValueError, match="confirmation state is later"):
        model_input(store, owner.fixed(), as_of=DAY, evaluated_at=AT, market=market)
    assert stored_rows(market) == previous


def test_preflight_materializes_only_explicit_exact_date_without_head(tmp_path):
    store, owner, market = setup_native(tmp_path, [attempt("first"), attempt("next", day=NEXT)])
    saved = fixed_input(store, owner.fixed(), days=(NEXT,), as_of=NEXT, evaluated_at=AT)
    del store.values[HEAD_KEY]
    store.get_keys.clear()
    materialize_saved_input(store, saved, market=market, days=(NEXT,))
    assert HEAD_KEY not in store.get_keys
    assert read_eq_master_exact(market, NEXT) is not None
    assert any(code == "99990" for _, code, _ in stored_rows(market))
    with pytest.raises(ValueError, match="outside"):
        materialize_saved_input(store, saved, market=market, days=(DAY,))


def test_cold_sparse_gap_has_no_manufactured_daily_status(tmp_path):
    store, owner, _market = setup_native(tmp_path, [attempt("first"), attempt("next", day=NEXT)])
    result = rebuild(
        store,
        owner.fixed(),
        start=DAY + timedelta(days=1),
        end=NEXT - timedelta(days=1),
        output=tmp_path / "gap",
    )
    assert result["snapshot_days"] == result["rows"] == result["unavailable_days"] == 0
    assert len(pq.read_table(tmp_path / "gap/master_receipt_status.parquet")) == 0


def test_publication_uses_same_immutable_seal_without_second_raw_decode(tmp_path):
    store, owner, market = setup_native(tmp_path, [attempt("first")])
    saved = model_input(store, owner.fixed(), as_of=DAY, evaluated_at=AT, market=market)
    with sealed_sqlite_snapshot(sqlite_path=market, mirror_root=tmp_path / "seal") as snapshot:
        del store.values[HEAD_KEY]
        with sqlite3.connect(market) as connection:
            connection.execute("DELETE FROM jquants_master_snapshots")
        store.get_keys.clear()
        result = export_master_from_native_input(
            source_snapshot=snapshot,
            saved=saved,
            mirror_root=tmp_path / "export",
            producer_git_commit="a" * 40,
            created_at=AT,
        )
        assert not store.get_keys
    source = result.manifest.partitions[0].sources[0]
    assert source.kind == "native_l1"
    assert source.namespace == "jquants-master"
    assert source.sha256 == saved.descriptor["selection_sha256"]
    assert source.key in store.values
    paths = materialize_partitions(
        LakeObjectCache(root=tmp_path / "export", source=LocalMirrorSource(tmp_path / "export")),
        dataset=LAKE_DATASETS["jquants.master_snapshots"],
        partitions=result.manifest.partitions,
    )
    assert sum(len(pq.read_table(path)) for path in paths) == 2503


@pytest.mark.parametrize("historical_invalid", [False, True])
def test_normal_models_and_publication_share_native_master_and_price(
    tmp_path, monkeypatch, historical_invalid
):
    import baibai_jobs.integration.models as job
    import baibai_jobs.publication.snapshot_publication as publication
    from baibai_engine.data_collection.providers.jquants.price_native import PriceAttempt, PriceL1
    from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
    from baibai_engine.machine_analysis.generation.store import ModelGenerationStore
    from baibai_engine.machine_analysis.screening.policies.economic_paths import EconomicPathPolicy
    from baibai_engine.storage.snapshots.reader import resolve_current_release
    from baibai_jobs.publication.policy import release_policy_for_profile

    monkeypatch.setenv("BAIBAI_MASTER_NATIVE", "true")
    names = ("jquants.daily_bars", "jquants.master_snapshots")
    narrow_release_policy(monkeypatch, datasets=names)
    monkeypatch.setattr(publication, "LAKE_DATASETS", {name: LAKE_DATASETS[name] for name in names})
    monkeypatch.setattr(publication, "lake_verified_git_commit", lambda: "a" * 40)
    monkeypatch.setattr(job, "_load_handle", lambda _path: None)
    monkeypatch.setattr(job, "verify_held", lambda *_args: None)
    attempts = [attempt("native-master")]
    if historical_invalid:
        attempts.insert(0, attempt("old-empty", day=DAY - timedelta(days=1), rows=[]))
    store, owner, market = setup_native(tmp_path, attempts)
    price = PriceL1(store, tmp_path / "price")
    price.initialize()
    body = json.dumps({"data": [{"Code": "13010", "Date": DAY.isoformat(), "C": 100}]}).encode()
    price.commit(
        [
            PriceAttempt(
                "native-price",
                DAY,
                AT,
                (JQuantsPage("daily_bars", {"date": DAY.isoformat()}, AT, body),),
            )
        ]
    )
    fixed = owner.fixed()
    economic = tmp_path / "economic.sqlite"
    initialize_database(economic).close()
    store.get_keys.clear()
    seed_empty_financial(store, tmp_path / "financial-native", DAY)
    result = job.integrate_models(
        store=store,
        edinet_input=empty_edinet_input(store, tmp_path),
        market=market,
        economic=economic,
        runs=tmp_path / "runs",
        mirror=tmp_path / "mirror",
        as_of=DAY,
        lease_handle=tmp_path / "lease",
        growth_start=None,
        policy=EconomicPathPolicy(candidate_limit=36),
    )
    assert result["company_count"] == 1  # ETF/REIT/preferred and other markets not candidates.
    assert store.get_keys.count(HEAD_KEY) == 1
    packs = [key for key in store.get_keys if "/master/v1/packs/" in key]
    assert len(packs) == len(set(packs)) == 1
    saved = ModelGenerationStore(tmp_path / "runs").read(result["generation_id"])
    assert saved.inputs["master"].descriptor["head_sha256"] == fixed.content_id
    assert "price" in saved.inputs
    assert read_eq_master_exact(market, DAY) is not None
    release = resolve_current_release(
        SimpleNamespace(read_bytes=store.get_bytes),
        catalog=LAKE_DATASETS,
        policy=release_policy_for_profile("production"),
    )
    master = release.dataset_manifest("jquants.master_snapshots")
    assert master.partitions[0].sources[0].namespace == "jquants-master"
    assert master.totals.rows == 2503
    assert master.coverage_status == ("partial" if historical_invalid else "complete")
    assert saved.inputs["master"].descriptor["summary"]["invalid_days"] == int(historical_invalid)
    assert saved.inputs["master"].descriptor["summary"]["coverage_status"] == master.coverage_status


def test_unavailable_requested_snapshot_does_not_reuse_old_projection(tmp_path):
    store, owner, market = setup_native(tmp_path, [attempt("failed", partial=True)])
    saved = model_input(store, owner.fixed(), as_of=DAY, evaluated_at=AT, market=market)
    assert stored_rows(market) == []
    assert saved.descriptor["summary"]["unavailable_days"] == 1
    assert saved.descriptor["summary"]["snapshot_status"] == "unavailable"
    with sqlite3.connect(market) as connection:
        assert connection.execute(
            "SELECT status,record_count FROM source_coverage WHERE source='jquants_master_snapshots'"
        ).fetchall() == [("unavailable", 0)]
    assert read_eq_master_exact(market, DAY) is None


def test_saved_metadata_missing_fails_without_latest_fallback(tmp_path):
    from baibai_engine.storage.objects.r2 import LakePublishError

    store, owner, market = setup_native(tmp_path, [attempt("first")])
    saved = model_input(store, owner.fixed(), as_of=DAY, evaluated_at=AT, market=market)
    root = (
        "fixed-inputs/jquants-master/v1/selections/"
        + saved.descriptor["selection_sha256"]
        + ".json"
    )
    del store.values[root]
    store.get_keys.clear()
    with pytest.raises(LakePublishError, match="unavailable"):
        rebuild_saved_input(store, saved, start=DAY, end=DAY, output=tmp_path / "missing")
    assert HEAD_KEY not in store.get_keys


def test_future_business_snapshot_is_retained_but_never_generation_asof(tmp_path):
    store, owner, market = setup_native(tmp_path, [attempt("past"), attempt("future", day=NEXT)])
    saved = model_input(store, owner.fixed(), as_of=DAY, evaluated_at=AT, market=market)
    assert saved.descriptor["summary"]["snapshot_date"] == DAY.isoformat()
    assert read_master_snapshot(market, as_of=DAY).as_of == DAY
    assert len(stored_rows(market)) == 5006


def test_base_export_still_rejects_empty_without_native_composition(tmp_path):
    from baibai_engine.storage.snapshots.writer import LakeBuildError, export_market_snapshot

    market = market_store(tmp_path / "market.sqlite")
    with (
        sealed_sqlite_snapshot(sqlite_path=market, mirror_root=tmp_path / "seal") as snapshot,
        pytest.raises(LakeBuildError, match="no lake dataset holds a row"),
    ):
        export_market_snapshot(
            catalog={},
            source_snapshot=snapshot,
            mirror_root=tmp_path / "export",
            producer_git_commit="a" * 40,
            expected_store_origin=None,
        )


@pytest.mark.parametrize("invalid_kind", ["empty", "truncated", "wrong-date"])
def test_historical_invalid_is_unavailable_boundary_but_newer_valid_recovers(
    tmp_path, invalid_kind
):
    prior = DAY - timedelta(days=30)
    invalid = (
        []
        if invalid_kind == "empty"
        else records()[:2499]
        if invalid_kind == "truncated"
        else records(NEXT)
    )
    store, owner, market = setup_native(
        tmp_path,
        [
            attempt("prior", day=prior),
            attempt("invalid", rows=invalid),
            attempt("current", day=NEXT),
        ],
    )
    saved = model_input(store, owner.fixed(), as_of=NEXT, evaluated_at=AT, market=market)
    summary = saved.descriptor["summary"]
    assert summary["coverage_status"] == "partial"
    assert summary["invalid_days"] == summary["unavailable_days"] == 1
    assert summary["invalid_snapshot_dates"] == [DAY.isoformat()]
    assert summary["snapshot_date"] == NEXT.isoformat()
    assert read_eq_master_asof(market, DAY - timedelta(days=1)).status == "prior_snapshot"
    assert read_eq_master_asof(market, prior - timedelta(days=1)).status == "future_snapshot"
    for day in (DAY, DAY + timedelta(days=1), NEXT - timedelta(days=1)):
        assert read_master_snapshot(market, as_of=day) is None
        assert read_eq_master_asof(market, day).status == "unavailable"
        assert read_eq_master_exact(market, day) is None
    assert read_master_snapshot(market, as_of=NEXT).as_of == NEXT
    assert read_eq_master_exact(market, NEXT) is not None
    old = stored_rows(market)
    with pytest.raises(JQuantsProviderError):
        model_input(store, owner.fixed(), as_of=DAY, evaluated_at=AT, market=market)
    assert stored_rows(market) == old
    del store.values[HEAD_KEY]
    market.unlink()
    store.get_keys.clear()
    cold = rebuild_saved_input(store, saved, start=prior, end=NEXT, output=tmp_path / "cold")
    assert cold["invalid_days"] == 1
    assert cold["coverage_status"] == "partial"
    statuses = pq.read_table(tmp_path / "cold/master_receipt_status.parquet").to_pylist()
    invalid_status = next(row for row in statuses if row["day"] == DAY.isoformat())
    assert invalid_status["status"] == "unavailable"
    assert invalid_status["error"].startswith("invalid master snapshot: ")
    assert invalid_status["row_count"] == 0
    assert HEAD_KEY not in store.get_keys
    # A historical slice reproduces its known invalid state, not a fabricated prior universe.
    subset = rebuild_saved_input(store, saved, start=DAY, end=DAY, output=tmp_path / "subset")
    assert subset["rows"] == 0
    assert subset["invalid_days"] == 1
    assert subset["coverage_status"] == "partial"
