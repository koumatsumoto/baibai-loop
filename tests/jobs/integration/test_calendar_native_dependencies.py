"""Acquisition calendars remain fixed independently of legacy releases and later heads."""

import json
import sqlite3
from datetime import timedelta

import pytest
from tests.helpers.edinet_native import empty_edinet_input
from tests.helpers.lake_release import market_store
from tests.helpers.tradingview import _sdk, payload, retain_result
from tests.jobs.integration.test_master_native_dependencies import AT, DAY, NOW, context
from tests.jobs.integration.test_master_native_models import attempt, records

from baibai_engine.data_collection.providers.jquants.calendar_native import (
    HEAD_KEY as CALENDAR_HEAD,
)
from baibai_engine.data_collection.providers.jquants.calendar_native import (
    CalendarAttempt,
    CalendarL1,
)
from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.master_native import HEAD_KEY as MASTER_HEAD
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.storage.json import write_json_atomic
from baibai_jobs.acquisition.calendar_dependencies import (
    CalendarDependencyUnavailable,
    fix_calendar_dependency,
    prepare_tradingview_calendar,
    saved_calendar_context,
)
from baibai_jobs.acquisition.master_dependencies import prepare_tradingview_master
from baibai_jobs.integration.source_handoff import _export, _replay, _replay_context


def calendar_attempt(identity, days, *, minute=0, partial=False):
    envelope = {"data": [{"Date": day.isoformat(), "HolDiv": value} for day, value in days]}
    if partial:
        envelope["pagination_key"] = "next"
    first, last = min(day for day, _ in days), max(day for day, _ in days)
    return CalendarAttempt(
        identity,
        first,
        last,
        None,
        (
            JQuantsPage(
                "market_calendar",
                {"from": first.isoformat(), "to": last.isoformat()},
                AT + timedelta(minutes=minute),
                json.dumps(envelope).encode(),
            ),
        ),
    )


def native_context(tmp_path, monkeypatch, *, days=None):
    monkeypatch.setenv("BAIBAI_MASTER_NATIVE", "true")
    monkeypatch.setenv("BAIBAI_CALENDAR_NATIVE", "true")
    population = records(DAY)
    for record in population:
        record["MktNm"] = "プライム"
    store, master, market, directory = context(
        tmp_path, attempts=[attempt("master", day=DAY, at=AT, rows=population)]
    )
    calendar = CalendarL1(store, tmp_path / "calendar")
    calendar.initialize()
    calendar.commit([calendar_attempt("calendar", days or [(DAY, "1")])])
    return store, master, calendar, market, directory


def test_external_scheduled_holiday_fixes_native_calendar_without_release_or_ready(
    tmp_path, monkeypatch
):
    import baibai_jobs.acquisition.external_assessments.daily as daily

    observed = DAY + timedelta(days=1)
    store, _master, calendar, _market, directory = native_context(
        tmp_path, monkeypatch, days=[(DAY, "1"), (observed, "3")]
    )
    for name in ("_serving_pointer", "available_dependencies"):
        monkeypatch.setattr(daily, name, lambda *_a, **_k: pytest.fail("legacy dependency read"))
    plan = daily.initialize(
        store,
        run_id="calendar-external",
        master_asof=None,
        observation_date=observed,
        mirror=directory / "external",
        source="monex",
    )
    binding = json.loads(plan.universe_binding)
    assert binding["master_asof"] == DAY.isoformat()
    saved = InputSnapshot.model_validate(binding["calendar_native_input"])
    assert saved_calendar_context(store, saved, observation_date=observed) == (DAY, False)
    assert "base_release_id" not in binding
    assert "calendar_receipt" not in binding
    calendar.commit([calendar_attempt("later", [(observed, "1")], minute=1)])
    assert saved_calendar_context(store, saved, observation_date=observed) == (DAY, False)
    del store.values[CALENDAR_HEAD]
    del store.values[MASTER_HEAD]
    assert (
        daily.initialize(
            store,
            run_id=plan.run_id,
            master_asof=None,
            observation_date=observed,
            mirror=None,
            source="monex",
        )
        == plan
    )


@pytest.mark.parametrize("fault", ["unknown", "invalid", "partial", "held", "gap"])
def test_unresolved_calendar_never_falls_back_to_saved_l2(tmp_path, monkeypatch, fault):
    store, _master, calendar, market, directory = native_context(tmp_path, monkeypatch)
    observed = DAY
    if fault == "gap":
        observed = DAY + timedelta(days=2)
        calendar.commit([calendar_attempt("gap", [(observed, "0")], minute=1)])
    else:
        calendar.commit(
            [
                calendar_attempt(
                    "bad",
                    [(DAY, "future" if fault == "unknown" else "" if fault == "invalid" else "0")],
                    minute=0 if fault == "held" else 1,
                    partial=fault == "partial",
                )
            ]
        )
    with pytest.raises(CalendarDependencyUnavailable):
        fix_calendar_dependency(store, directory=directory / "fix", observation_date=observed)
    assert not prepare_tradingview_calendar(
        store, directory=directory, market=market, observation_date=observed
    )
    assert "calendar_native_input" not in json.loads((directory / "result.json").read_bytes())
    with sqlite3.connect(market) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jquants_market_calendar").fetchone() == (0,)


def test_native_master_hydrates_calendar_first_and_keeps_fixed_basis_during_recovery(
    tmp_path, monkeypatch
):
    observed = DAY + timedelta(days=1)
    store, _master, calendar, market, directory = native_context(
        tmp_path, monkeypatch, days=[(DAY, "1"), (observed, "0")]
    )
    # The scoped L2 is absent or wrong. It cannot select the dependency date.
    with sqlite3.connect(market) as conn:
        conn.execute("DELETE FROM jquants_market_calendar")
        conn.execute("INSERT INTO jquants_market_calendar VALUES (?,1)", (observed.isoformat(),))
    assert prepare_tradingview_master(
        store, directory=directory, market=market, observation_date=observed
    )
    before = json.loads((directory / "result.json").read_bytes())
    assert before["master_native_input"]["descriptor"]["as_of"] == DAY.isoformat()
    calendar.commit([calendar_attempt("changed", [(observed, "1")], minute=1)])
    del store.values[CALENDAR_HEAD]
    assert prepare_tradingview_master(
        store, directory=directory, market=market, observation_date=observed
    )
    after = json.loads((directory / "result.json").read_bytes())
    assert before["calendar_native_input"] == after["calendar_native_input"]


def test_native_tradingview_packet_cold_replays_fixed_references_without_raw_calendar(
    tmp_path, monkeypatch
):
    store, _master, _calendar, source, directory = native_context(tmp_path, monkeypatch)
    assert prepare_tradingview_master(
        store, directory=directory, market=source, observation_date=DAY
    )
    saved = json.loads((directory / "result.json").read_bytes())
    master = InputSnapshot.model_validate(saved["master_native_input"])
    calendar = InputSnapshot.model_validate(saved["calendar_native_input"])
    retain_result(
        source,
        _sdk(payload(["TSE:1301"])),
        day=DAY,
        master=DAY,
        received=NOW,
        symbols=("TSE:1301",),
    )
    packet = tmp_path / "packet"
    _export(
        "tradingview",
        DAY,
        NOW - timedelta(seconds=1),
        source,
        packet,
        master_input=master,
        calendar_input=calendar,
    )
    assert not list(packet.glob("dependency-*.json"))
    with sqlite3.connect(packet / "receipts.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM jquants_source_pages").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM jquants_market_calendar").fetchone() == (0,)
    del store.values[CALENDAR_HEAD]
    del store.values[MASTER_HEAD]
    report = json.loads((packet / "result.json").read_bytes())
    selected = _replay_context(store, "tradingview", directory, report)
    assert selected == {"master_input": master, "calendar_input": calendar}
    target = market_store(tmp_path / "target.sqlite")
    result = _replay("tradingview", target, packet, store=store, **selected)
    assert result.complete
    assert result.report["rows_added"] == 1
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jquants_source_pages").fetchone() == (0,)


def test_native_scoped_input_never_requires_legacy_calendar_or_copies_its_coverage(
    tmp_path, monkeypatch
):
    from baibai_jobs.acquisition.inputs import export_source_input

    _store, _master, _calendar, source, _directory = native_context(tmp_path, monkeypatch)
    with sqlite3.connect(source) as conn:
        conn.execute("DELETE FROM jquants_market_calendar")
    output = tmp_path / "input.sqlite"
    report = export_source_input(source, output, group="tradingview", asof=DAY)
    assert "jquants_market_calendar" not in report["rows"]
    assert "jquants_master_snapshots" not in report["rows"]
    with sqlite3.connect(output) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jquants_market_calendar").fetchone() == (0,)
        assert conn.execute(
            "SELECT COUNT(*) FROM source_coverage WHERE source='jquants_market_calendar'"
        ).fetchone() == (0,)


def test_common_integration_hydrates_native_calendar_even_without_source_handoffs(
    tmp_path, monkeypatch
):
    import baibai_jobs.integration.sources as sources

    store, _master, _calendar, market, directory = native_context(tmp_path, monkeypatch)
    monkeypatch.setattr(sources, "extract_saved_edinet", lambda *a, **k: {})
    write_json_atomic(
        directory / "plan.json",
        {
            "edinet_input": empty_edinet_input(store, tmp_path).model_dump(mode="json"),
            "master_native": True,
            "calendar_native": True,
            "financial_native": False,
            "price_native": False,
            "handoffs": [],
            "external": [],
        },
    )
    with sqlite3.connect(market) as conn:
        conn.execute("UPDATE jquants_market_calendar SET holiday_division='future'")
    sources.apply(store, directory, market=market, asof=DAY)
    with sqlite3.connect(market) as conn:
        assert conn.execute("SELECT * FROM jquants_market_calendar").fetchall() == [
            (DAY.isoformat(), "1")
        ]


def test_common_apply_verifies_old_calendar_dependency_before_accepting_tradingview(
    tmp_path, monkeypatch
):
    import baibai_jobs.integration.sources as sources
    from baibai_engine.data_collection.providers.jquants.contracts import complete_request
    from baibai_engine.data_collection.providers.jquants.dependencies import dependency_from_request
    from baibai_jobs.acquisition.calendar_dependencies import verify_stage_calendar_dependencies
    from baibai_jobs.acquisition.economic_staging import save_handoff
    from baibai_jobs.acquisition.receipts import available_handoffs

    store, _master, calendar, source, directory = native_context(tmp_path, monkeypatch)
    assert prepare_tradingview_master(
        store, directory=directory, market=source, observation_date=DAY
    )
    saved = json.loads((directory / "result.json").read_bytes())
    retain_result(
        source,
        _sdk(payload(["TSE:1301"])),
        day=DAY,
        master=DAY,
        received=NOW,
        symbols=("TSE:1301",),
    )
    packet = tmp_path / "legacy"
    _export(
        "tradingview",
        DAY,
        NOW - timedelta(seconds=1),
        source,
        packet,
        master_input=InputSnapshot.model_validate(saved["master_native_input"]),
    )
    original = calendar_attempt("calendar", [(DAY, "1")])
    write_json_atomic(
        packet / "dependency-calendar.json",
        dependency_from_request("calendar", complete_request(original.pages)),
    )
    save_handoff(store, "tradingview-legacy-calendar", packet, market=True)
    entries = available_handoffs(store, source="tradingview")
    fingerprints = {
        identity: receipt["fingerprint"]
        for identity, receipt in calendar.fixed().value()["receipts"].items()
    }
    verify_stage_calendar_dependencies(store, entries[0], fingerprints)
    with pytest.raises(ValueError, match="finite cutover"):
        verify_stage_calendar_dependencies(store, entries[0], {})
    with pytest.raises(ValueError, match="manifest changed"):
        verify_stage_calendar_dependencies(
            store, {**entries[0], "manifest_sha256": "0" * 64}, fingerprints
        )
    monkeypatch.setattr(sources, "extract_saved_edinet", lambda *a, **k: {})
    write_json_atomic(
        directory / "plan.json",
        {
            "edinet_input": empty_edinet_input(store, tmp_path).model_dump(mode="json"),
            "master_native": True,
            "calendar_native": True,
            "financial_native": False,
            "price_native": False,
            "handoffs": entries,
            "external": [],
        },
    )
    target = market_store(tmp_path / "target.sqlite")
    report = sources.apply(store, directory, market=target, asof=DAY)
    assert report["handoffs"][0]["applied"] is True
    assert report["handoffs"][0]["failed"] is False
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jquants_source_pages").fetchone() == (0,)
        assert conn.execute("SELECT ticker FROM tradingview_forecast_snapshots").fetchall() == [
            ("1301",)
        ]
