"""Native financial head is fixed once and reaches the ordinary model consumer."""

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.lake_release import market_store
from tests.helpers.r2_store import MemoryR2Store

import baibai_engine.machine_analysis.generation.inputs as generation
from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.financial_native import (
    FinancialAttempt,
    FinancialL1,
)
from baibai_engine.machine_analysis.generation.financial_inputs import read_financial_input
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
from baibai_engine.storage.snapshots.writer import sealed_sqlite_snapshot
from baibai_jobs.integration.financial_native import model_input

DAY = date(2026, 11, 2)
AT = datetime(2026, 11, 3, tzinfo=UTC)


def attempt(identity, *, at=AT, value=1):
    body = json.dumps(
        {"data": [{"Code": "13010", "DiscDate": DAY.isoformat(), "DiscNo": "1", "Sales": value}]}
    ).encode()
    return FinancialAttempt(
        identity,
        DAY,
        at,
        (JQuantsPage("financial_summaries", {"date": DAY.isoformat()}, at, body),),
    )


def test_native_financial_inputs_generate_all_models_without_old_financial_projection(
    tmp_path, monkeypatch
):
    store = MemoryR2Store()
    owner = FinancialL1(store, tmp_path / "native")
    owner.initialize()
    owner.commit([attempt("native")])
    financial = model_input(store, owner.fixed(), as_of=DAY, evaluated_at=AT)
    assert financial.documents[0].sales == 1
    market = market_store(
        tmp_path / "market.sqlite",
        daily_bars=(("1301", DAY.isoformat(), 100.0, 1.0),),
        short_sale_reports=(),
    )
    monkeypatch.setattr(
        generation,
        "read_financial_documents",
        lambda *a, **k: pytest.fail("old financial projection was read"),
    )
    with sealed_sqlite_snapshot(sqlite_path=market, mirror_root=tmp_path / "mirror") as snapshot:
        result = generation.read_and_generate_models(
            generation_id="native-financial",
            market_snapshot=snapshot,
            market_release=InputRelease(release_id="market-only", manifest_sha256="a" * 64),
            economic_path=None,
            economic_identity=None,
            as_of=DAY,
            evaluation_time=AT,
            ttm_rules=DEFAULT_TTM_RULES,
            tickers=("1301",),
            financial_input=financial,
        ).materialize()
    assert len({model.model_id for model in result.models}) == 7
    assert result.inputs["financial"] == financial.identity
    growth = result.result("company_growth", "1301").payload["input_release"]
    assert growth["collection_boundary"] == "fixed_source_head"
    assert growth["release_id"] == financial.identity.reference
    assert growth["manifest_sha256"] == financial.identity.sha256
    assert (
        result.result("social_trends", "market:global").payload["input_manifest"]["release_id"]
        == "market-only"
    )


def test_fixed_head_survives_new_acquisition_and_missing_days_are_explicit(tmp_path):
    store = MemoryR2Store()
    owner = FinancialL1(store, tmp_path / "native")
    owner.initialize()
    owner.commit([attempt("a")])
    old = owner.fixed()
    owner.commit([attempt("b", at=AT + timedelta(hours=1), value=2)])
    first = model_input(store, old, as_of=DAY + timedelta(days=1), evaluated_at=AT)
    second = model_input(store, owner.fixed(), as_of=DAY, evaluated_at=AT + timedelta(hours=1))
    assert first.documents[0].sales == 1
    assert second.documents[0].sales == 2
    selected = read_financial_input(
        store, first.identity.descriptor, days=(DAY, DAY + timedelta(days=1))
    )
    assert selected["scopes"][(DAY + timedelta(days=1)).isoformat()] == {}
    assert first.identity.descriptor["summary"]["unavailable_days"] == 1


def test_same_value_confirmation_after_evaluation_clock_is_not_used(tmp_path):
    store = MemoryR2Store()
    owner = FinancialL1(store, tmp_path / "native")
    owner.initialize()
    owner.commit([attempt("a")])
    owner.commit([attempt("equal", at=AT + timedelta(hours=2))])
    with pytest.raises(ValueError, match="confirmation state is later"):
        model_input(store, owner.fixed(), as_of=DAY, evaluated_at=AT + timedelta(hours=1))


def test_first_native_daily_publishes_models_and_upload_copy_without_financial_dataset(
    tmp_path, monkeypatch
):
    import shutil

    from tests.helpers.lake_policy import narrow_release_policy
    from tests.helpers.master import insert_master_rows

    import baibai_jobs.integration.models as job
    import baibai_jobs.publication.snapshot_publication as publication
    from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
    from baibai_engine.machine_analysis.generation.store import ModelGenerationStore
    from baibai_engine.machine_analysis.screening.policies.economic_paths import EconomicPathPolicy
    from baibai_engine.storage.snapshots.duck import lake_session
    from baibai_engine.storage.snapshots.hydrate import dehydrate_market_store, hydrate_market_store
    from baibai_engine.storage.snapshots.objects import LakeObjectCache
    from baibai_engine.storage.snapshots.reader import LakeReadError, resolve_current_release
    from baibai_jobs.publication.datasets import LAKE_DATASETS
    from baibai_jobs.publication.policy import release_policy_for_profile

    monkeypatch.setenv("BAIBAI_FINANCIAL_NATIVE", "true")
    names = ("jquants.daily_bars", "jquants.master_snapshots", "jquants.fin_summaries")
    narrow_release_policy(monkeypatch, datasets=names, carries_history=None)
    monkeypatch.setattr(publication, "LAKE_DATASETS", {key: LAKE_DATASETS[key] for key in names})
    monkeypatch.setattr(publication, "lake_verified_git_commit", lambda: "a" * 40)
    monkeypatch.setattr(job, "_load_handle", lambda _: object())
    monkeypatch.setattr(job, "verify_held", lambda *_: None)
    store = MemoryR2Store()
    owner = FinancialL1(store, tmp_path / "native")
    owner.initialize()
    owner.commit([attempt("native")])
    before = owner.fixed()
    market = market_store(
        tmp_path / "market.sqlite",
        daily_bars=(("1301", DAY.isoformat(), 100.0, 1.0),),
        short_sale_reports=(),
    )
    with sqlite3.connect(market) as connection:
        insert_master_rows(
            connection, [(DAY.isoformat(), "1301", "合成企業", "プライム", "卸売業", "011")]
        )
        connection.execute(
            "INSERT INTO jquants_fin_summaries VALUES (?, ?, ?, ?)",
            ("1301", DAY.isoformat(), "old", "{}"),
        )
        connection.execute(
            "INSERT INTO source_coverage VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "jquants_fin_summaries",
                "old",
                DAY.isoformat(),
                DAY.isoformat(),
                AT.isoformat(),
                1,
                "ok",
                None,
            ),
        )
    # The first native publication must be allowed to retire a previously served
    # financial projection, without weakening history guards for the legacy writer.
    from tests.helpers.lake_release import publish_market_fixture

    from baibai_engine.storage.snapshots.keys import (
        current_release_pointer_key,
        release_manifest_key,
    )
    from baibai_engine.storage.snapshots.models import canonical_lake_model_bytes
    from baibai_engine.storage.snapshots.release import LakeReleasePointer
    from baibai_engine.storage.sqlite.source_cache.lake_origin import read_lake_store_origin

    publish_market_fixture(market, "legacy-financial", created_at=AT)
    for path in (tmp_path / "mirror" / "lake").rglob("*"):
        if path.is_file():
            store.store_object(
                path.relative_to(tmp_path / "mirror").as_posix(),
                path.read_bytes(),
                content_type="application/vnd.apache.parquet"
                if path.suffix == ".parquet"
                else "application/json",
            )
    origin = read_lake_store_origin(market)
    store.store_object(
        current_release_pointer_key(),
        canonical_lake_model_bytes(
            LakeReleasePointer(
                release_id=origin.release_id,
                manifest_key=release_manifest_key(release_id=origin.release_id),
                manifest_sha256=origin.release_manifest_sha256,
            )
        ),
    )
    financial_policy = next(
        item
        for item in release_policy_for_profile("production").datasets
        if item.dataset == "jquants.fin_summaries"
    )
    assert financial_policy.carries_history is True
    economic = tmp_path / "economic.sqlite"
    initialize_database(economic).close()
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
    saved = ModelGenerationStore(tmp_path / "runs").read(result["generation_id"])
    assert saved.inputs["financial"].descriptor["head_sha256"] == before.content_id
    assert FinancialL1(store, tmp_path / "cold").fixed().payload == before.payload
    with sqlite3.connect(market) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jquants_fin_summaries").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM source_coverage WHERE source='jquants_fin_summaries'"
            ).fetchone()[0]
            == 0
        )
    # A reader in another workflow does not receive the rollout setting.
    monkeypatch.delenv("BAIBAI_FINANCIAL_NATIVE")
    from types import SimpleNamespace

    source = SimpleNamespace(read_bytes=store.get_bytes)
    release = resolve_current_release(
        source, catalog=LAKE_DATASETS, policy=release_policy_for_profile("production")
    )
    assert "jquants.fin_summaries" not in release.dataset_names()
    with pytest.raises(LakeReadError, match="does not contain dataset"):
        release.dataset_manifest("jquants.fin_summaries")
    upload = tmp_path / "upload.sqlite"
    shutil.copyfile(market, upload)
    dehydrate_market_store(upload, catalog=LAKE_DATASETS, release=release)
    with lake_session() as session:
        hydrate_market_store(
            session,
            catalog=LAKE_DATASETS,
            release=release,
            cache=LakeObjectCache(root=tmp_path / "mirror", source=source),
            store=upload,
            dataset_names=release.dataset_names(),
        )
    with sqlite3.connect(upload) as connection:
        assert connection.execute("SELECT close FROM jquants_daily_bars").fetchone()[0] == 100


def test_retained_later_correction_preserves_growth_retrospective_semantics(tmp_path):
    from baibai_engine.machine_analysis.models.company_growth.generation import (
        generate_company_growth,
    )
    from baibai_engine.machine_analysis.observations.financials.financial_documents import (
        normalize_financial_summary,
    )

    evaluation = datetime(2026, 10, 1, tzinfo=UTC)
    as_of = date(2026, 6, 30)
    store = MemoryR2Store()
    owner = FinancialL1(store, tmp_path / "native")
    owner.initialize()
    originals = []
    attempts = []
    for identity, disclosed, sales in (
        ("original", "2026-05-15", 100),
        ("correction", "2026-09-15", 200),
    ):
        record = {
            "Code": "13010",
            "DiscDate": disclosed,
            "DiscNo": identity,
            "DiscTime": "15:00:00",
            "DocType": "FYFinancialStatements_Consolidated_JP",
            "CurPerType": "FY",
            "CurPerSt": "2025-04-01",
            "CurPerEn": "2026-03-31",
            "CurFYSt": "2025-04-01",
            "CurFYEn": "2026-03-31",
            "Sales": sales,
        }
        originals.append(normalize_financial_summary(record))
        page = JQuantsPage(
            "financial_summaries",
            {"date": disclosed},
            evaluation,
            json.dumps({"data": [record]}).encode(),
        )
        attempts.append(
            FinancialAttempt(identity, date.fromisoformat(disclosed), evaluation, (page,))
        )
    owner.commit(attempts)
    native = model_input(store, owner.fixed(), as_of=as_of, evaluated_at=evaluation)

    def growth(documents):
        return generate_company_growth(
            documents,
            ticker="1301",
            sector=None,
            as_of=as_of,
            input_release=InputRelease(release_id="fixed", manifest_sha256="a" * 64),
        )

    legacy = growth(originals)
    result = growth(native.documents)
    assert result.periods == legacy.periods
    assert result.periods[0].observations[0].metrics["sales"].value == 200
    assert result.calculation_method == "company_growth_retrospective_v7"
    assert result.read_semantics == "retained_latest_in_fixed_release_not_point_in_time"


def test_native_input_summary_bounds_unresolved_ranges_and_separates_confirmed_changed(tmp_path):
    store = MemoryR2Store()
    owner = FinancialL1(store, tmp_path / "native")
    owner.initialize()
    owner.commit([attempt("a")])
    owner.commit([attempt("equal", at=AT + timedelta(hours=1))])
    observed = model_input(
        store, owner.fixed(), as_of=DAY + timedelta(days=30), evaluated_at=AT + timedelta(hours=1)
    )
    summary = observed.identity.descriptor["summary"]
    assert summary["adopted_requests"] == 1
    assert summary["unavailable_days"] == 30
    assert summary["unresolved_ranges"] == [
        {
            "start": (DAY + timedelta(days=1)).isoformat(),
            "end": (DAY + timedelta(days=30)).isoformat(),
        }
    ]
    assert summary["latest_changed_at"] == AT.isoformat()
    assert summary["latest_confirmed_at"] == (AT + timedelta(hours=1)).isoformat()
    assert len(json.dumps(summary)) < 1500


def test_native_input_summary_limits_disjoint_gaps_without_hiding_their_count(tmp_path):
    store = MemoryR2Store()
    owner = FinancialL1(store, tmp_path / "native")
    owner.initialize()
    start = DAY - timedelta(days=50)
    days = tuple(start + timedelta(days=offset) for offset in range(0, 51, 2))
    owner.commit(
        [
            FinancialAttempt(
                f"empty-{day}",
                day,
                AT,
                (
                    JQuantsPage(
                        "financial_summaries", {"date": day.isoformat()}, AT, b'{"data":[]}'
                    ),
                ),
            )
            for day in days
        ]
    )
    observed = model_input(store, owner.fixed(), as_of=DAY, evaluated_at=AT)
    summary = observed.identity.descriptor["summary"]
    assert summary["normal_empty_days"] == 26
    assert summary["unavailable_days"] == 25
    assert len(summary["unresolved_ranges"]) == 12
    assert summary["additional_unresolved_ranges"] == 13
    assert summary["unresolved_ranges"][0] == {
        "start": (start + timedelta(days=1)).isoformat(),
        "end": (start + timedelta(days=1)).isoformat(),
    }
    assert len(json.dumps(summary)) < 1800
