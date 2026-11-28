"""Native master feeds acquisition consumers without a legacy release or duplicate raw stage."""

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.lake_release import market_store
from tests.helpers.r2_store import MemoryR2Store
from tests.jobs.integration.test_master_native_models import attempt, records

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.master_native import HEAD_KEY, MasterL1
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.master_inputs import (
    INPUT_PREFIX as MASTER_INPUT_PREFIX,
)
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_jobs.acquisition.master_dependencies import (
    MasterDependencyUnavailable,
    fix_master_dependency,
    saved_master_dependency,
)
from baibai_jobs.integration.master_native import fixed_input, materialize_saved_input

DAY = date(2026, 11, 4)
AT = datetime(2026, 11, 4, 3, tzinfo=UTC)
NOW = datetime(2026, 11, 4, 7, tzinfo=UTC)


def context(tmp_path, *, attempts=None):
    store = MemoryR2Store()
    owner = MasterL1(store, tmp_path / "native")
    owner.initialize()
    owner.commit(attempts or [attempt("native", day=DAY, at=AT)])
    market = market_store(tmp_path / "market.sqlite", short_sale_reports=())
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
    store, owner, market, _ = context(tmp_path, attempts=attempts)
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


@pytest.mark.parametrize("fault", ["missing-metadata", "missing-body", "changed-body"])
def test_saved_master_dependency_does_not_hide_missing_or_changed_originals(tmp_path, fault):
    store, _, _, directory = context(tmp_path)
    saved = fix_master_dependency(store, directory=directory / "fixed", day=DAY)
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
        saved_master_dependency(store, saved, day=DAY)
    assert HEAD_KEY not in store.get_keys
    assert HEAD_KEY not in store.head_keys


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
