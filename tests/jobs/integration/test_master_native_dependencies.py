"""Native master feeds acquisition consumers without a legacy release or duplicate raw stage."""

import json
import sqlite3
from collections import Counter
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.lake_release import market_store
from tests.helpers.tradingview import _sdk, payload, retain_result, stored
from tests.jobs.integration.test_master_native_models import attempt, records, setup_native

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.master_native import HEAD_KEY
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.master_inputs import (
    INPUT_PREFIX as MASTER_INPUT_PREFIX,
)
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_jobs.acquisition.master_dependencies import (
    MasterDependencyUnavailable,
    prepare_tradingview_master,
    saved_master_dependency,
)
from baibai_jobs.integration.master_native import fixed_input, materialize_saved_input
from baibai_jobs.integration.source_handoff import _replay
from baibai_jobs.integration.tradingview_handoff import export_handoff

DAY = date(2026, 11, 4)
AT = datetime(2026, 11, 4, 3, tzinfo=UTC)
NOW = datetime(2026, 11, 4, 7, tzinfo=UTC)


def context(tmp_path, *, attempts=None):
    store, owner, market = setup_native(tmp_path, attempts or [attempt("native", day=DAY, at=AT)])
    with sqlite3.connect(market) as conn:
        conn.execute("INSERT INTO jquants_market_calendar VALUES (?,1)", (DAY.isoformat(),))
    from baibai_engine.data_collection.providers.jquants.calendar_native import (
        CalendarAttempt,
        CalendarL1,
    )

    calendar = CalendarL1(store, tmp_path / "calendar-native-fixture")
    calendar.initialize()
    calendar.commit(
        [
            CalendarAttempt(
                "calendar",
                DAY,
                DAY,
                None,
                (
                    JQuantsPage(
                        "market_calendar",
                        {"from": DAY.isoformat(), "to": DAY.isoformat()},
                        AT,
                        json.dumps({"data": [{"Date": DAY.isoformat(), "HolDiv": "1"}]}).encode(),
                    ),
                ),
            )
        ]
    )
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

    population = records(DAY)
    for record in population:
        record["MktNm"] = "プライム"
    store, _owner, _market, directory = context(
        tmp_path, attempts=[attempt("native", day=DAY, at=AT, rows=population)]
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
    request = saved_master_dependency(store, saved, day=DAY)
    assert request.source == "master"
    assert request.params == {"date": DAY.strftime("%Y%m%d")}
    assert request.records() == population


@pytest.mark.parametrize("fault", ["empty", "partial", "held"])
def test_new_native_dependency_never_falls_back_after_empty_failure_or_held(
    tmp_path, monkeypatch, fault
):
    import baibai_jobs.acquisition.external_assessments.daily as daily

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
        calendar_input=InputSnapshot.model_validate(
            json.loads((directory / "result.json").read_bytes())["calendar_native_input"]
        ),
    )
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
    store.get_keys.clear()
    store.head_keys.clear()
    store.put_keys.clear()
    result = _replay(
        "tradingview",
        target,
        packet,
        store=store,
        master_input=saved,
        calendar_input=InputSnapshot.model_validate(
            json.loads((directory / "result.json").read_bytes())["calendar_native_input"]
        ),
    )
    assert result.complete
    assert result.report["rows_added"] == 1
    master_reads = Counter(key for key in store.get_keys if key.startswith(MASTER_INPUT_PREFIX))
    assert len(master_reads) == 2  # One selection root plus its only selected month.
    assert set(master_reads.values()) == {2}  # One projection read and one separate packet check.
    assert not store.put_keys
    assert HEAD_KEY not in store.get_keys + store.head_keys
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


@pytest.mark.parametrize("state", ["unadopted", "failed", "held"])
def test_master_replay_rejects_unresolved_fixed_scope_before_writing(tmp_path, state):
    attempts = [] if state == "unadopted" else [attempt("clean", day=DAY, at=AT)]
    changed = records(DAY)
    changed[0]["CoName"] = "未採用の訂正"
    attempts.append(
        attempt(
            "unresolved",
            day=DAY,
            at=AT if state == "held" else AT + timedelta(minutes=1),
            rows=changed,
            partial=state != "held",
        )
    )
    store, owner, market = setup_native(tmp_path, attempts)
    head = owner.fixed()
    scope = head.value()["scopes"][DAY.isoformat()]
    assert bool(scope.get("adopted")) == (state != "unadopted")
    assert bool(scope.get("held")) == (state == "held")
    assert scope.get("failure") or scope.get("held")
    saved = fixed_input(store, head, as_of=DAY, evaluated_at=NOW, days=[DAY])
    with sqlite3.connect(market) as conn:
        before = list(conn.iterdump())
    store.get_keys.clear()
    with pytest.raises(ValueError, match="native master replay acquisition is unresolved"):
        materialize_saved_input(store, saved, market=market, days=[DAY], require_resolved=True)
    assert len(store.get_keys) == 2
    assert all(key.startswith(MASTER_INPUT_PREFIX) for key in store.get_keys)
    with sqlite3.connect(market) as conn:
        assert list(conn.iterdump()) == before


def test_unresolved_native_master_clears_only_exact_stale_preflight_projection(tmp_path):
    store, owner, market, directory = context(tmp_path)
    assert prepare_tradingview_master(
        store, directory=directory, market=market, observation_date=DAY
    )
    # A new source input carries old TradingView rows, not a prior cycle's fixed master.
    report = json.loads((directory / "result.json").read_bytes())
    report.pop("master_native_input")
    write_json_atomic(directory / "result.json", report)
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


@pytest.mark.parametrize("change", ["correction", "partial", "missing-head"])
@pytest.mark.parametrize("acquire", [False, True])
def test_prepared_tradingview_master_keeps_saved_universe_after_head_changes(
    tmp_path, change, acquire
):
    store, owner, market, directory = context(tmp_path)
    assert prepare_tradingview_master(
        store, directory=directory, market=market, observation_date=DAY
    )
    report = json.loads((directory / "result.json").read_bytes())
    with sqlite3.connect(market) as connection:
        before = connection.execute(
            "SELECT * FROM jquants_master_snapshots ORDER BY snapshot_date,security_code"
        ).fetchall()
    if change == "missing-head":
        del store.values[HEAD_KEY]
    else:
        changed = records(DAY)
        changed[0]["CoName"] = "後日の訂正"
        owner.commit(
            [
                attempt(
                    change,
                    day=DAY,
                    at=AT + timedelta(minutes=1),
                    rows=changed,
                    partial=change == "partial",
                )
            ]
        )
    store.get_keys.clear()
    store.head_keys.clear()
    with sqlite3.connect(market) as connection:
        connection.execute(
            "DELETE FROM jquants_master_snapshots WHERE snapshot_date=?", (DAY.isoformat(),)
        )
    assert prepare_tradingview_master(
        store, directory=directory, market=market, observation_date=DAY, acquire=acquire
    )
    after = json.loads((directory / "result.json").read_bytes())
    assert after["master_native_input"] == report["master_native_input"]
    assert after["calendar_native_input"] == report["calendar_native_input"]
    assert HEAD_KEY not in store.get_keys
    assert HEAD_KEY not in store.head_keys
    with sqlite3.connect(market) as connection:
        assert (
            connection.execute(
                "SELECT * FROM jquants_master_snapshots ORDER BY snapshot_date,security_code"
            ).fetchall()
            == before
        )


@pytest.mark.parametrize("fault", ["missing-metadata", "missing-body", "changed-body"])
@pytest.mark.parametrize("acquire", [False, True])
def test_saved_master_preparation_does_not_hide_missing_or_changed_originals(
    tmp_path, fault, acquire
):
    store, _owner, market, directory = context(tmp_path)
    assert prepare_tradingview_master(
        store, directory=directory, market=market, observation_date=DAY
    )
    report = (directory / "result.json").read_bytes()
    if fault == "missing-metadata":
        key = next(key for key in store.values if key.startswith("fixed-inputs/jquants-master/"))
        del store.values[key]
    else:
        key = next(key for key in store.values if "/master/v1/packs/" in key)
        if fault == "missing-body":
            del store.values[key]
        else:
            store.values[key].body += b"changed"
    store.get_keys.clear()
    store.head_keys.clear()
    with pytest.raises(LakePublishError, match=r"unavailable|changed|integrity"):
        prepare_tradingview_master(
            store, directory=directory, market=market, observation_date=DAY, acquire=acquire
        )
    assert HEAD_KEY not in store.get_keys
    assert HEAD_KEY not in store.head_keys
    assert (directory / "result.json").read_bytes() == report


def test_native_master_recovery_keeps_existing_tradingview_receipts(tmp_path):
    from tests.jobs.acquisition.test_jquants_master_native import Client

    from baibai_jobs.maintenance.tradingview_rebuild import rebuild_from_receipts

    store, owner, market, directory = context(tmp_path)
    assert prepare_tradingview_master(
        store, directory=directory, market=market, observation_date=DAY
    )
    receipt = retain_result(
        market,
        _sdk(payload(["TSE:1301"])),
        day=DAY,
        master=DAY,
        received=NOW,
        symbols=("TSE:1301",),
    )
    rebuild_from_receipts(market, DAY, [receipt])
    before = stored(market)
    assert len(before) == 1
    # Clearing the failed exact-date master must not touch retained TV data.
    report = json.loads((directory / "result.json").read_bytes())
    report.pop("master_native_input")
    write_json_atomic(directory / "result.json", report)
    owner.commit([attempt("failure", day=DAY, at=NOW + timedelta(minutes=1), partial=True)])
    assert not prepare_tradingview_master(
        store, directory=directory, market=market, observation_date=DAY
    )
    assert stored(market) == before
    with sqlite3.connect(market) as conn:
        assert conn.execute("SELECT receipt_id FROM tradingview_source_receipts").fetchall() == [
            (receipt,)
        ]
    client = Client(records(DAY), AT + timedelta(hours=6))
    assert prepare_tradingview_master(
        store, directory=directory, market=market, observation_date=DAY, acquire=True, client=client
    )
    assert stored(market) == before
    with sqlite3.connect(market) as conn:
        assert conn.execute("SELECT receipt_id FROM tradingview_source_receipts").fetchall() == [
            (receipt,)
        ]


def test_native_explicit_dates_do_not_refetch_fulfilled_cohort_dates(tmp_path, monkeypatch):
    from baibai_jobs.acquisition import jquants_master_native

    store, _owner, _market, _directory = context(tmp_path)
    store.close = lambda: None
    monkeypatch.setattr(jquants_master_native, "Boto3R2Store", lambda **_: store)
    monkeypatch.chdir(tmp_path)

    class Client:
        def close(self):
            pass

        def pages(self, *_):
            pytest.fail("fulfilled master must not be requested again")

    monkeypatch.setattr(jquants_master_native, "JQuantsHTTP", lambda *_: Client())
    assert jquants_master_native.main(["acquire", "--asof", DAY.isoformat()]) == 0
    assert not (tmp_path / "stores/market/market.sqlite").exists()


def test_tradingview_scoped_input_omits_disposable_master_copy_in_native_mode(
    tmp_path, monkeypatch
):
    from baibai_jobs.acquisition.inputs import export_source_input

    store, _owner, market, directory = context(tmp_path)
    assert prepare_tradingview_master(
        store, directory=directory, market=market, observation_date=DAY
    )
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
        assert conn.execute("SELECT COUNT(*) FROM jquants_market_calendar").fetchone()[0] == 0
