"""Acquisition calendars remain fixed independently of legacy releases and later heads."""

import json
import sqlite3
from dataclasses import replace
from datetime import date, timedelta

import pytest
from tests.helpers.edinet_native import empty_edinet_input
from tests.jobs.integration.test_master_native_dependencies import AT, DAY, context
from tests.jobs.integration.test_master_native_models import attempt, records

from baibai_engine.data_collection.providers.jquants import calendar_native
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
    saved_calendar_context,
)


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
    population = records(DAY)
    for record in population:
        record["MktNm"] = "プライム"
    store, master, market, directory = context(
        tmp_path, attempts=[attempt("master", day=DAY, at=AT, rows=population)]
    )
    calendar = CalendarL1(store, tmp_path / "calendar")
    if days is not None:
        calendar.commit([calendar_attempt("calendar-range", days, minute=1)])
    return store, master, calendar, market, directory


@pytest.mark.parametrize("opening", ["1", "2"])
def test_external_scheduled_holiday_fixes_native_calendar_without_release_or_ready(
    tmp_path, monkeypatch, opening
):
    import baibai_jobs.acquisition.external_assessments.daily as daily

    observed = DAY + timedelta(days=1)
    store, _master, calendar, _market, directory = native_context(
        tmp_path, monkeypatch, days=[(DAY, opening), (observed, "3")]
    )
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
    with sqlite3.connect(market) as conn:
        assert conn.execute(
            "SELECT day, holiday_division FROM jquants_market_calendar"
        ).fetchall() == [(DAY.isoformat(), "1")]


@pytest.mark.parametrize("problem", ["legacy-date", "different-division"])
def test_native_calendar_dependency_will_not_use_a_disputed_latest_day(
    tmp_path, monkeypatch, problem
):
    store, _, calendar, market, directory = native_context(tmp_path, monkeypatch)
    observed = DAY + timedelta(days=1)
    calendar.commit([calendar_attempt("good", [(DAY, "1")], minute=1)])
    latest = calendar_attempt(
        "latest", [(observed, "0" if problem == "legacy-date" else "1")], minute=2
    )
    if problem == "legacy-date":
        body = {"data": [{"Date": observed.isoformat() + " suffix", "HolDiv": "0"}]}
        page = replace(latest.pages[0], body=json.dumps(body).encode())
        with monkeypatch.context() as old:
            old.setattr(calendar_native, "_response_day", lambda raw: date.fromisoformat(raw[:10]))
            calendar.commit([replace(latest, pages=(page,))])
    else:
        calendar.commit([latest])
        head = calendar.fixed().value()
        head["days"][observed.isoformat()]["division"] = "0"
        store.store_object(CALENDAR_HEAD, json.dumps(head).encode())
    with sqlite3.connect(market) as conn:
        conn.execute("DELETE FROM jquants_market_calendar")
        conn.execute("INSERT INTO jquants_market_calendar VALUES (?, ?)", (str(observed), "0"))
    store.get_keys.clear()
    with pytest.raises(CalendarDependencyUnavailable):
        fix_calendar_dependency(store, directory=directory / "fix", observation_date=observed)
    # Acquisition uses native evidence and does not clobber an unrelated working copy.
    with sqlite3.connect(market) as conn:
        assert conn.execute("SELECT holiday_division FROM jquants_market_calendar").fetchone() == (
            "0",
        )
    packs = [key for key in store.get_keys if key.startswith(calendar_native.PREFIX + "packs/")]
    assert len(packs) == len(set(packs)) == 2  # One previous open and one disputed range.


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


@pytest.mark.parametrize("existing", [False, True])
def test_common_integration_defers_full_calendar_projection_to_model_publication(
    tmp_path, monkeypatch, existing
):
    from tests.helpers.lake_policy import narrow_release_policy
    from tests.helpers.price_native import seed_prices

    import baibai_jobs.integration.sources as sources
    import baibai_jobs.publication.snapshot_publication as publication

    store, _master, _calendar, market, directory = native_context(tmp_path, monkeypatch)
    monkeypatch.setattr(sources, "extract_saved_edinet", lambda *a, **k: {})
    write_json_atomic(
        directory / "plan.json",
        {
            "edinet_input": empty_edinet_input(store, tmp_path).model_dump(mode="json"),
            "handoffs": [],
            "external": [],
        },
    )
    with sqlite3.connect(market) as conn:
        if existing:
            conn.execute("UPDATE jquants_market_calendar SET holiday_division='future'")
        else:
            conn.execute("DELETE FROM jquants_market_calendar")
    store.get_keys.clear()
    for _ in range(2):
        report = sources.apply(store, directory, market=market, asof=DAY)
        with sqlite3.connect(market) as conn:
            assert conn.execute("SELECT * FROM jquants_market_calendar").fetchall() == (
                [(DAY.isoformat(), "future")] if existing else []
            )
    assert not any("/packs/" in key for key in store.get_keys)

    narrow_release_policy(monkeypatch, datasets=("jquants.market_calendar",))
    monkeypatch.setattr(
        publication,
        "LAKE_DATASETS",
        {"jquants.market_calendar": publication.LAKE_DATASETS["jquants.market_calendar"]},
    )
    monkeypatch.setattr(publication, "lake_verified_git_commit", lambda: "a" * 40)
    seed_prices(store, market, tmp_path / "price", as_of=DAY)
    with publication.prepare_market_lake(
        sqlite_path=market,
        mirror_root=tmp_path / "publication",
        store=store,
        as_of=DAY,
        edinet_input=InputSnapshot.model_validate(report["edinet_input"]),
    ) as prepared:
        with sqlite3.connect(prepared.source_snapshot.path) as conn:
            assert conn.execute("SELECT * FROM jquants_market_calendar").fetchall() == [
                (DAY.isoformat(), "1")
            ]
        assert prepared.calendar_identity.descriptor["summary"]["coverage_status"] == "complete"
        assert prepared.export.datasets["jquants.market_calendar"].manifest.totals.rows == 1
    with sqlite3.connect(market) as conn:
        assert conn.execute("SELECT * FROM jquants_market_calendar").fetchall() == [
            (DAY.isoformat(), "1")
        ]
