"""Native master feeds acquisition consumers without a legacy release or duplicate raw stage."""

import gzip
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from tests.helpers.jquants_receipts import body
from tests.helpers.lake_release import market_store
from tests.helpers.tradingview import _sdk, payload, retain_result
from tests.jobs.integration.test_master_native_models import attempt, records, setup_native

from baibai_engine.data_collection.providers.jquants.contract import JQUANTS_SOURCE_PAGES_DDL
from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.master_native import HEAD_KEY, MasterAttempt
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.storage.json import write_json_atomic
from baibai_jobs.acquisition.master_dependencies import (
    MasterDependencyUnavailable,
    prepare_tradingview_master,
    saved_master_dependency,
)
from baibai_jobs.integration.source_handoff import _replay
from baibai_jobs.integration.tradingview_handoff import export_handoff

DAY = date(2026, 11, 4)
AT = datetime(2026, 11, 4, 3, tzinfo=UTC)
NOW = datetime(2026, 11, 4, 7, tzinfo=UTC)


def context(tmp_path, *, attempts=None):
    store, owner, market = setup_native(tmp_path, attempts or [attempt("native", day=DAY, at=AT)])
    with sqlite3.connect(market) as conn:
        conn.execute("INSERT INTO jquants_market_calendar VALUES (?,1)", (DAY.isoformat(),))
    directory = tmp_path / "input"
    directory.mkdir()
    write_json_atomic(
        directory / "result.json",
        {"group": "tradingview", "created_at": AT.isoformat(), "canonical_generations": {}},
    )
    return store, owner, market, directory


def test_external_new_native_plan_requires_no_legacy_release_and_saved_resume_is_fixed(
    tmp_path, monkeypatch
):
    import baibai_jobs.acquisition.external_assessments.daily as daily

    monkeypatch.setenv("BAIBAI_MASTER_NATIVE", "true")
    population = records(DAY)
    for record in population:
        record["MktNm"] = "プライム"
    store, _owner, _market, directory = context(
        tmp_path, attempts=[attempt("native", day=DAY, at=AT, rows=population)]
    )
    monkeypatch.setattr(
        daily,
        "_serving_pointer",
        lambda *_: pytest.fail("explicit native master must not read legacy release"),
    )
    plan = daily.initialize(
        store,
        run_id="native-external",
        master_asof=DAY,
        mirror=directory / "mirror",
        source="monex",
    )
    binding = json.loads(plan.universe_binding)
    assert binding["master_asof"] == DAY.isoformat()
    assert binding["master_native_input"]["kind"] == "native_l1"
    assert sum(len(cohort.tickers) for cohort in plan.cohorts) == 2500
    del store.values[HEAD_KEY]
    assert (
        daily.initialize(store, run_id=plan.run_id, master_asof=DAY, mirror=None, source="monex")
        == plan
    )
    saved = InputSnapshot.model_validate(binding["master_native_input"])
    assert saved_master_dependency(store, saved, day=DAY)["request_id"] == "native"


@pytest.mark.parametrize("fault", ["empty", "partial", "held"])
def test_new_native_dependency_never_falls_back_after_empty_failure_or_held(
    tmp_path, monkeypatch, fault
):
    import baibai_jobs.acquisition.external_assessments.daily as daily

    monkeypatch.setenv("BAIBAI_MASTER_NATIVE", "true")
    store, owner, _market, directory = context(tmp_path)
    later = AT if fault == "held" else AT + timedelta(minutes=1)
    owner.commit(
        [
            attempt(
                "bad",
                day=DAY,
                at=later,
                rows=[] if fault == "empty" else records(DAY),
                partial=fault == "partial",
            )
        ]
    )
    monkeypatch.setattr(
        daily, "_serving_pointer", lambda *_: pytest.fail("no native fallback to old release")
    )
    with pytest.raises((MasterDependencyUnavailable, RuntimeError)):
        daily.initialize(
            store,
            run_id="bad-native-" + fault,
            master_asof=DAY,
            mirror=directory / "mirror",
            source="monex",
        )


def test_tradingview_native_stage_uses_saved_reference_and_cold_replays_without_head(tmp_path):
    store, _owner, source, directory = context(tmp_path)
    assert prepare_tradingview_master(
        store, directory=directory, market=source, observation_date=DAY
    )
    saved = InputSnapshot.model_validate(
        json.loads((directory / "result.json").read_bytes())["master_native_input"]
    )
    retain_result(
        source,
        _sdk(payload(["TSE:1301"])),
        day=DAY,
        master=DAY,
        received=NOW,
        symbols=("TSE:1301",),
    )
    packet = tmp_path / "packet"
    packet.mkdir()
    handoff = export_handoff(
        source,
        packet / "receipts.sqlite",
        day=DAY,
        since=NOW - timedelta(seconds=1),
        master_input=saved,
    )
    assert handoff.master_request_ids == ()
    write_json_atomic(
        packet / "result.json",
        {"source": "tradingview", "asof": DAY.isoformat(), **handoff.to_json()},
    )
    with sqlite3.connect(packet / "receipts.sqlite") as raw:
        assert raw.execute("SELECT COUNT(*) FROM jquants_source_pages").fetchone()[0] == 0
    target = market_store(tmp_path / "target.sqlite")
    with sqlite3.connect(target) as conn:
        conn.execute("INSERT INTO jquants_market_calendar VALUES (?,1)", (DAY.isoformat(),))
    del store.values[HEAD_KEY]
    result = _replay("tradingview", target, packet, store=store, master_input=saved)
    assert result.complete
    assert result.report["rows_added"] == 1
    with sqlite3.connect(target) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM jquants_source_pages WHERE source='master'"
            ).fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT ticker FROM tradingview_forecast_snapshots").fetchall() == [
            ("1301",)
        ]


def test_unresolved_native_master_clears_only_exact_stale_preflight_projection(tmp_path):
    store, owner, market, directory = context(tmp_path)
    assert prepare_tradingview_master(
        store, directory=directory, market=market, observation_date=DAY
    )
    owner.commit([attempt("partial", day=DAY, at=AT + timedelta(minutes=1), partial=True)])
    assert not prepare_tradingview_master(
        store, directory=directory, market=market, observation_date=DAY
    )
    assert "master_native_input" not in json.loads((directory / "result.json").read_bytes())
    with sqlite3.connect(market) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM jquants_master_snapshots WHERE snapshot_date=?",
                (DAY.isoformat(),),
            ).fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM jquants_market_calendar").fetchone()[0] == 1


def test_old_mixed_stage_master_is_verified_but_does_not_block_other_source_projection(tmp_path):
    from tests.helpers.r2_store import MemoryR2Store

    from baibai_engine.data_collection.providers.jquants.master_native import MasterL1

    store = MemoryR2Store()
    page = JQuantsPage("master", {"date": DAY.isoformat()}, AT, body([]))
    old = MasterAttempt("old-empty", DAY, None, (page,))
    owner = MasterL1(store, tmp_path / "native")
    owner.initialize()
    owner.commit([old])
    packet = tmp_path / "packet"
    packet.mkdir()
    with sqlite3.connect(packet / "receipts.sqlite") as conn:
        conn.executescript(JQUANTS_SOURCE_PAGES_DDL)
        for identity, source, query, data in (
            ("old-empty", "master", {"date": DAY.isoformat()}, page.body),
            (
                "calendar",
                "market_calendar",
                {"from": DAY.isoformat(), "to": DAY.isoformat()},
                body([{"Date": DAY.isoformat(), "HolDiv": "1"}]),
            ),
        ):
            conn.execute(
                "INSERT INTO jquants_source_pages VALUES (?,?,?,?,?,?,?)",
                (
                    identity,
                    0,
                    source,
                    json.dumps(query),
                    AT.isoformat(),
                    DAY.isoformat(),
                    gzip.compress(data, mtime=0),
                ),
            )
    write_json_atomic(
        packet / "result.json",
        {"source": "jquants-market", "request_ids": ["old-empty", "calendar"]},
    )
    fingerprints = {
        identity: receipt["fingerprint"]
        for identity, receipt in owner.fixed().value()["receipts"].items()
    }
    target = tmp_path / "target.sqlite"
    result = _replay("jquants-market", target, packet, master_fingerprints=fingerprints)
    assert result.complete
    assert result.report["migrated_master_requests"] == ["old-empty"]
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT day FROM jquants_market_calendar").fetchall() == [
            (DAY.isoformat(),)
        ]
        assert conn.execute("SELECT DISTINCT source FROM jquants_source_pages").fetchall() == [
            ("market_calendar",)
        ]
    with pytest.raises(ValueError, match="differs from the native cutover"):
        _replay("jquants-market", tmp_path / "bad.sqlite", packet, master_fingerprints={})


def test_native_backfill_alias_does_not_refetch_fulfilled_cohort_dates(tmp_path, monkeypatch):
    import baibai_jobs.maintenance.master_native_backfill as backfill

    store, _owner, _market, _directory = context(tmp_path)
    store.close = lambda: None
    monkeypatch.setattr(backfill, "Boto3R2Store", lambda **_: store)

    class Client:
        def close(self):
            pass

        def pages(self, *_):
            pytest.fail("fulfilled master must not be requested again by maintenance alias")

    monkeypatch.setattr(backfill, "JQuantsHTTP", lambda *_: Client())
    assert (
        backfill.backfill_native_master(
            SimpleNamespace(asofs=[DAY.isoformat()], month_end_from=None, month_end_to=None),
            sqlite_path=tmp_path / "no-l2.sqlite",
        )
        == 0
    )


def test_legacy_tradingview_master_is_verified_against_finite_native_migration(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BAIBAI_MASTER_NATIVE", "true")
    page = JQuantsPage(
        "master", {"date": DAY.strftime("%Y%m%d")}, AT, json.dumps({"data": records(DAY)}).encode()
    )
    old = MasterAttempt("legacy", DAY, None, (page,))
    store, owner, source, directory = context(tmp_path, attempts=[old])
    assert prepare_tradingview_master(
        store, directory=directory, market=source, observation_date=DAY
    )
    saved = InputSnapshot.model_validate(
        json.loads((directory / "result.json").read_bytes())["master_native_input"]
    )
    with sqlite3.connect(source) as conn:
        conn.execute(
            "INSERT INTO jquants_source_pages VALUES (?,?,?,?,?,?,?)",
            (
                "legacy",
                0,
                "master",
                json.dumps(dict(page.query)),
                AT.isoformat(),
                DAY.isoformat(),
                gzip.compress(page.body, mtime=0),
            ),
        )
    retain_result(
        source,
        _sdk(payload(["TSE:1301"])),
        day=DAY,
        master=DAY,
        received=NOW,
        symbols=("TSE:1301",),
    )
    packet = tmp_path / "legacy-packet"
    packet.mkdir()
    handoff = export_handoff(
        source, packet / "receipts.sqlite", day=DAY, since=AT - timedelta(seconds=1)
    )
    assert handoff.master_request_ids == ("legacy",)
    write_json_atomic(
        packet / "result.json",
        {"source": "tradingview", "asof": DAY.isoformat(), **handoff.to_json()},
    )
    target = market_store(tmp_path / "target.sqlite")
    with sqlite3.connect(target) as conn:
        conn.execute("INSERT INTO jquants_market_calendar VALUES (?,1)", (DAY.isoformat(),))
    fingerprints = {
        identity: receipt["fingerprint"]
        for identity, receipt in owner.fixed().value()["receipts"].items()
    }
    result = _replay(
        "tradingview",
        target,
        packet,
        store=store,
        master_input=saved,
        master_fingerprints=fingerprints,
    )
    assert result.complete
    with sqlite3.connect(target) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM jquants_source_pages WHERE source='master'"
            ).fetchone()[0]
            == 0
        )
    with pytest.raises(ValueError, match="differs from the native cutover"):
        _replay(
            "tradingview", target, packet, store=store, master_input=saved, master_fingerprints={}
        )


def test_tradingview_scoped_input_omits_disposable_master_copy_in_native_mode(
    tmp_path, monkeypatch
):
    from baibai_jobs.acquisition.inputs import export_source_input

    store, _owner, market, directory = context(tmp_path)
    assert prepare_tradingview_master(
        store, directory=directory, market=market, observation_date=DAY
    )
    monkeypatch.setenv("BAIBAI_MASTER_NATIVE", "true")
    output = tmp_path / "scoped.sqlite"
    report = export_source_input(market, output, group="tradingview", asof=DAY)
    assert "jquants_master_snapshots" not in report["rows"]
    with sqlite3.connect(output) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jquants_master_snapshots").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM source_coverage WHERE source='jquants_master_snapshots'"
            ).fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM jquants_market_calendar").fetchone()[0] == 1


def test_native_master_stage_common_apply_then_normal_models_use_same_source(tmp_path, monkeypatch):
    from tests.helpers.lake_policy import narrow_release_policy

    import baibai_jobs.integration.models as models
    import baibai_jobs.integration.sources as sources
    import baibai_jobs.publication.snapshot_publication as publication
    from baibai_engine.data_collection.providers.jquants.price_native import PriceAttempt, PriceL1
    from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
    from baibai_engine.machine_analysis.generation.store import ModelGenerationStore
    from baibai_engine.machine_analysis.screening.policies.economic_paths import EconomicPathPolicy
    from baibai_jobs.acquisition.economic_staging import save_handoff
    from baibai_jobs.acquisition.receipts import available_handoffs
    from baibai_jobs.publication.datasets import LAKE_DATASETS

    monkeypatch.setenv("BAIBAI_MASTER_NATIVE", "true")
    monkeypatch.setenv("BAIBAI_PRICE_NATIVE", "true")
    monkeypatch.setenv("BAIBAI_FINANCIAL_NATIVE", "false")
    narrow_release_policy(monkeypatch, datasets=("jquants.master_snapshots", "jquants.daily_bars"))
    monkeypatch.setattr(
        publication,
        "LAKE_DATASETS",
        {name: LAKE_DATASETS[name] for name in ("jquants.master_snapshots", "jquants.daily_bars")},
    )
    monkeypatch.setattr(publication, "lake_verified_git_commit", lambda: "a" * 40)
    monkeypatch.setattr(sources, "extract_saved_edinet", lambda *_, **__: {})
    monkeypatch.setattr(models, "_load_handle", lambda *_: None)
    monkeypatch.setattr(models, "verify_held", lambda *_: None)
    store, owner, source, input_directory = context(tmp_path)
    assert prepare_tradingview_master(
        store, directory=input_directory, market=source, observation_date=DAY
    )
    saved = InputSnapshot.model_validate(
        json.loads((input_directory / "result.json").read_bytes())["master_native_input"]
    )
    retain_result(
        source,
        _sdk(payload(["TSE:1301"])),
        day=DAY,
        master=DAY,
        received=NOW,
        symbols=("TSE:1301",),
    )
    packet = tmp_path / "packet"
    packet.mkdir()
    selected = export_handoff(
        source,
        packet / "receipts.sqlite",
        day=DAY,
        since=NOW - timedelta(seconds=1),
        master_input=saved,
    )
    write_json_atomic(
        packet / "result.json",
        {
            "source": "tradingview",
            "asof": DAY.isoformat(),
            "started_at": NOW.isoformat(),
            **selected.to_json(),
        },
    )
    save_handoff(store, "tradingview-native-e2e", packet, market=True)
    work = tmp_path / "integration"
    work.mkdir()
    write_json_atomic(
        work / "plan.json",
        {
            "master_native": True,
            "price_native": True,
            "financial_native": False,
            "handoffs": available_handoffs(store, source="tradingview"),
            "external": [],
        },
    )
    target = market_store(tmp_path / "target.sqlite")
    with sqlite3.connect(target) as conn:
        conn.execute("INSERT INTO jquants_market_calendar VALUES (?,1)", (DAY.isoformat(),))
    applied = sources.apply(store, work, market=target, asof=DAY)
    assert applied["handoffs"][0]["applied"] is True
    assert applied["handoffs"][0]["failed"] is False
    price = PriceL1(store, tmp_path / "price")
    price.initialize()
    price.commit(
        [
            PriceAttempt(
                "price",
                DAY,
                AT,
                (
                    JQuantsPage(
                        "daily_bars",
                        {"date": DAY.isoformat()},
                        AT,
                        body([{"Code": "13010", "Date": DAY.isoformat(), "C": 100}]),
                    ),
                ),
            )
        ]
    )
    economic = tmp_path / "economic.sqlite"
    initialize_database(economic).close()
    generated = models.integrate_models(
        store=store,
        market=target,
        economic=economic,
        runs=tmp_path / "runs",
        mirror=tmp_path / "mirror",
        as_of=DAY,
        lease_handle=tmp_path / "lease",
        growth_start=None,
        policy=EconomicPathPolicy(candidate_limit=36),
    )
    assert generated["company_count"] == 1
    metadata = ModelGenerationStore(tmp_path / "runs").read(generated["generation_id"])
    assert metadata.inputs["master"].descriptor["head_sha256"] == owner.fixed().content_id
    with sqlite3.connect(target) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM tradingview_forecast_snapshots").fetchone()[0] == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM jquants_source_pages WHERE source='master'"
            ).fetchone()[0]
            == 0
        )
