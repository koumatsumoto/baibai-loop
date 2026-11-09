"""Source-specific fulfillment stays independent from L2 and fixed historical rules."""

import json
import sqlite3
from datetime import date, timedelta

import pytest
from tests.helpers.jquants_receipts import body
from tests.helpers.margin_interest_native import AT, DAY, attempt, calendar, records
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.calendar_native import (
    HEAD_KEY as CALENDAR_HEAD,
)
from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsPage,
    JQuantsRateLimitError,
)
from baibai_engine.data_collection.providers.jquants.margin_interest_native import (
    HEAD_KEY,
    PREFIX,
    MarginInterestL1,
    balance_days,
    plan_margin_interest_days,
)
from baibai_jobs.acquisition.jquants_margin_interest_native import acquire_margin_interest_native
from baibai_jobs.integration.margin_interest_native import fixed_input, materialize_saved_input


class Client:
    def __init__(self, *, at=AT, rows=None, partial=False, callback=None):
        self.at, self.rows, self.partial, self.callback = at, rows, partial, callback
        self.calls = []

    def close(self):
        pass

    def pages(self, source, query):
        self.calls.append((source, query))
        if self.callback:
            self.callback()
        yield JQuantsPage(
            source,
            query,
            self.at,
            body(
                records(date.fromisoformat(query["date"])) if self.rows is None else self.rows,
                cursor="next" if self.partial else None,
            ),
        )
        if self.partial:
            raise JQuantsRateLimitError("synthetic stop")


def setup(tmp_path, *, start=DAY, end=DAY, omit=(), closed=()):
    store = MemoryR2Store()
    owner = MarginInterestL1(store, tmp_path / "raw")
    owner.initialize()
    cal = calendar(store, tmp_path / "calendar", start, end, omit=omit, closed=closed)
    return store, owner, cal


def test_normal_plan_preserves_transition_lags_and_all_daily_history(tmp_path):
    start, end = date(2026, 2, 13), date(2026, 10, 1)
    _, owner, cal = setup(tmp_path, start=start, end=end)
    plan = plan_margin_interest_days(owner.fixed(), cal, asof=end)
    assert date(2026, 9, 18) in plan.days
    assert not set(plan.days).intersection(date(2026, 9, d) for d in range(19, 25))
    assert [d for d in plan.days if d >= DAY] == [
        DAY,
        date(2026, 9, 28),
        date(2026, 9, 29),
        date(2026, 9, 30),
    ]
    # The weekly source uses a 230-calendar-day window, not a seven-day overlap.
    assert min(plan.days) == date(2026, 2, 13)
    assert not plan.unknown_calendar_days


def test_two_later_sessions_weekly_and_one_daily_including_holidays(tmp_path):
    start, end = date(2026, 9, 7), date(2026, 9, 29)
    _, owner, cal = setup(tmp_path, start=start, end=end, closed=(date(2026, 9, 14),))
    before = plan_margin_interest_days(
        owner.fixed(), cal, asof=date(2026, 9, 15), start=start, end=date(2026, 9, 11)
    )
    # Historical backfill can deliberately acquire an endpoint before it is due.
    assert date(2026, 9, 11) in before.days
    normal = plan_margin_interest_days(owner.fixed(), cal, asof=date(2026, 9, 15))
    assert date(2026, 9, 11) not in normal.days
    normal = plan_margin_interest_days(owner.fixed(), cal, asof=date(2026, 9, 16))
    assert date(2026, 9, 11) in normal.days
    assert DAY not in plan_margin_interest_days(owner.fixed(), cal, asof=DAY).days
    assert DAY in plan_margin_interest_days(owner.fixed(), cal, asof=date(2026, 9, 28)).days


def test_unknown_calendar_never_becomes_closed_or_silent_smaller_scope(tmp_path):
    missing = date(2026, 9, 28)
    _, owner, cal = setup(tmp_path, start=DAY, end=date(2026, 9, 29), omit=(missing,))
    plan = plan_margin_interest_days(
        owner.fixed(), cal, asof=date(2026, 9, 29), start=DAY, end=date(2026, 9, 29)
    )
    assert plan.unknown_calendar_days == (missing,)
    assert plan.days == (DAY, date(2026, 9, 29))


def test_early_weekly_empty_uses_real_equal_confirmation_and_final_week_exception(tmp_path):
    first, last = date(2026, 9, 7), date(2026, 9, 30)
    store, owner, cal = setup(tmp_path, start=first, end=last)
    week = date(2026, 9, 11)
    early = AT.replace(month=9, day=15, hour=1)
    owner.commit([attempt("empty", day=week, at=early, rows=[])])
    assert (
        week in plan_margin_interest_days(owner.fixed(), cal, asof=last, start=first, end=week).days
    )
    late = AT.replace(month=9, day=15, hour=15)  # Sept 16 in JST; publication day was Sept 15.
    owner.commit([attempt("confirm", day=week, at=late, rows=[])])
    assert (
        week
        not in plan_margin_interest_days(owner.fixed(), cal, asof=last, start=first, end=week).days
    )
    assert next(owner.fixed().requests(store, days=[week])).request.received_at == early
    assert owner.fixed().index().receipts[0].received_at == late
    final = date(2026, 9, 18)
    owner.commit([attempt("final-empty", day=final, at=AT, rows=[])])
    assert (
        final
        in plan_margin_interest_days(owner.fixed(), cal, asof=last, start=final, end=final).days
    )


def test_finite_final_week_empty_remains_pending_with_endpoint_only_calendar(tmp_path):
    final = date(2026, 9, 18)
    _, owner, cal = setup(tmp_path, start=final, end=final)
    owner.commit([attempt("final-empty", day=final, rows=[])])
    plan = plan_margin_interest_days(owner.fixed(), cal, asof=AT.date(), start=final, end=final)
    assert plan.days == (final,)
    assert plan.unknown_calendar_days == ()  # Its final publication date is already known.


def test_finite_early_empty_does_not_hide_missing_publication_calendar(tmp_path):
    week = date(2026, 9, 11)
    _, owner, cal = setup(tmp_path, start=week, end=week)
    owner.commit([attempt("early-empty", day=week, at=AT.replace(day=14, month=9), rows=[])])
    plan = plan_margin_interest_days(owner.fixed(), cal, asof=AT.date(), start=week, end=week)
    assert plan.days == (week,)
    assert plan.unknown_calendar_days == (date(2026, 9, 12), date(2026, 9, 13))


def test_finite_empty_needs_only_two_proven_sessions_before_jst_confirmation(tmp_path):
    week, publication = date(2017, 5, 5), date(2017, 5, 9)
    _, owner, cal = setup(
        tmp_path,
        start=week,
        end=publication,
        omit=(date(2017, 5, 6),),
    )
    # Two later known sessions prove publication before this check even though
    # an intervening day and the years after this finite backfill are absent.
    owner.commit(
        [
            attempt(
                "skipped-week", day=week, at=AT.replace(year=2017, month=5, day=9, hour=15), rows=[]
            )
        ]
    )
    plan = plan_margin_interest_days(owner.fixed(), cal, asof=AT.date(), start=week, end=week)
    assert plan.days == plan.unknown_calendar_days == ()


def test_explicit_backfill_keeps_prepublication_empty_pending(tmp_path):
    week = date(2026, 9, 11)
    _, owner, cal = setup(tmp_path, start=week, end=date(2026, 9, 14))
    owner.commit([attempt("early-empty", day=week, at=AT.replace(day=14, month=9), rows=[])])
    plan = plan_margin_interest_days(
        owner.fixed(), cal, asof=date(2026, 9, 14), start=week, end=week
    )
    assert plan.days == (week,)
    assert plan.unknown_calendar_days == ()


def test_daily_empty_retries_and_nonempty_malformed_l2_is_not_refetched(tmp_path):
    store, owner, cal = setup(tmp_path)
    owner.commit([attempt("empty", rows=[])])
    assert plan_margin_interest_days(owner.fixed(), cal, asof=DAY, start=DAY, end=DAY).days == (
        DAY,
    )
    owner.commit([attempt("raw", at=AT + timedelta(hours=1), rows=[{"Date": DAY.isoformat()}])])
    assert plan_margin_interest_days(owner.fixed(), cal, asof=DAY, start=DAY, end=DAY).days == ()
    saved = fixed_input(
        store, owner.fixed(), cal, start=DAY, end=DAY, evaluated_at=AT + timedelta(hours=1)
    )
    result = materialize_saved_input(store, saved, output=tmp_path / "partial.sqlite")
    assert result["coverage_status"] == "partial"
    assert result["daily_rows"] == 0


def test_force_backfill_is_finite_and_includes_last_day(tmp_path):
    _, owner, cal = setup(tmp_path)
    owner.commit([attempt("one")])
    assert balance_days(cal, start=DAY, end=DAY)[0] == (DAY,)
    assert plan_margin_interest_days(owner.fixed(), cal, asof=DAY, start=DAY, end=DAY).days == ()
    assert plan_margin_interest_days(
        owner.fixed(), cal, asof=DAY, start=DAY, end=DAY, force=True
    ).days == (DAY,)
    for kwargs in ({"force": True}, {"start": DAY}, {"start": DAY, "end": DAY + timedelta(days=1)}):
        with pytest.raises(ValueError, match=r"backfill|interval"):
            plan_margin_interest_days(owner.fixed(), cal, asof=DAY, **kwargs)


def test_weekly_backfill_does_not_treat_midweek_range_end_as_week_end(tmp_path):
    monday, friday = date(2026, 9, 14), date(2026, 9, 18)
    _, _, cal = setup(tmp_path, start=monday, end=friday)
    assert balance_days(cal, start=monday, end=monday) == ((), (), ())
    assert balance_days(cal, start=monday, end=friday)[0] == (friday,)


def test_weekly_backfill_reports_missing_later_calendar_days(tmp_path):
    thursday, friday = date(2026, 9, 17), date(2026, 9, 18)
    _, _, cal = setup(tmp_path, start=thursday, end=thursday)
    expected, unknown, _ = balance_days(cal, start=thursday, end=thursday)
    assert expected == ()
    assert unknown == (friday,)


def test_weekly_backfill_keeps_thursday_when_friday_is_closed(tmp_path):
    thursday, friday = date(2026, 9, 17), date(2026, 9, 18)
    _, _, cal = setup(tmp_path, start=thursday, end=friday, closed=(friday,))
    assert balance_days(cal, start=thursday, end=thursday) == ((thursday,), (), ())


def test_acquire_uses_compact_dates_and_frozen_calendar_without_l2(tmp_path):
    store, owner, cal = setup(tmp_path)
    client = Client(callback=lambda: store.values.pop(CALENDAR_HEAD, None))
    report = acquire_margin_interest_native(
        owner, cal, asof=DAY, start=DAY, end=DAY, client=client, clock=lambda: AT
    )
    assert client.calls == [("margin_interest", {"date": "20260925"})]
    assert report["changed"] == 1
    assert report["incomplete"] == 0
    assert (owner.directory / "calendar-fixed.json").read_bytes() == cal.payload
    assert (
        json.loads((owner.directory / "plan.json").read_bytes())["calendar_sha256"]
        == cal.content_id
    )


def test_partial_stops_and_keeps_pending_dates_and_raw_pages(tmp_path):
    _, owner, cal = setup(tmp_path, end=date(2026, 9, 28))
    client = Client(partial=True)
    report = acquire_margin_interest_native(
        owner,
        cal,
        asof=date(2026, 9, 28),
        start=DAY,
        end=date(2026, 9, 28),
        client=client,
        clock=lambda: AT,
    )
    assert report["failed"] == report["halted"] == 1
    assert report["incomplete"] == 2
    assert len(client.calls) == 1
    assert list(owner.fixed().value()["scopes"]) == [DAY.isoformat()]
    assert next(owner.fixed().requests(owner.store, days=[DAY])).error is not None


def test_cold_rebuild_keeps_weekly_daily_fields_without_newest_heads_or_prices(tmp_path):
    week = date(2026, 9, 18)
    store, owner, cal = setup(tmp_path, start=week, end=DAY)
    owner.commit(
        [attempt("weekly", day=week), attempt("daily", rows=records(publication=date(2026, 9, 28)))]
    )
    saved = fixed_input(store, owner.fixed(), cal, start=week, end=DAY, evaluated_at=AT)
    store.values.pop(HEAD_KEY)
    store.values.pop(CALENDAR_HEAD)
    output = tmp_path / "cold.sqlite"
    result = materialize_saved_input(store, saved, output=output)
    assert result["weekly_rows"] == result["daily_rows"] == 1
    assert result["coverage_status"] == "complete"
    with sqlite3.connect(output) as con:
        assert (
            con.execute("SELECT publication_date FROM jquants_all_issues_daily_margin").fetchone()[
                0
            ]
            == "2026-09-28"
        )
        assert con.execute("SELECT COUNT(*) FROM jquants_daily_bars").fetchone()[0] == 0
    with pytest.raises(FileExistsError):
        materialize_saved_input(store, saved, output=output)


def test_cold_final_week_empty_keeps_native_fulfillment_separate_from_l2_coverage(tmp_path):
    week = date(2026, 9, 18)
    store, owner, cal = setup(tmp_path, start=week, end=AT.date())
    owner.commit([attempt("empty", day=week, rows=[])])
    saved = fixed_input(store, owner.fixed(), cal, start=week, end=week, evaluated_at=AT)
    store.values.pop(HEAD_KEY)
    store.values.pop(CALENDAR_HEAD)
    output = tmp_path / "cold-empty.sqlite"
    result = materialize_saved_input(store, saved, output=output)
    assert result["coverage_status"] == "partial"
    assert result["unresolved_days"] == [week.isoformat()]
    with sqlite3.connect(output) as conn:
        assert conn.execute(
            "SELECT status,record_count FROM source_coverage WHERE source='jquants_weekly_margin'"
        ).fetchone() == ("ok", 0)


def test_cold_subset_does_not_inherit_another_weeks_unresolved_empty(tmp_path):
    final = date(2026, 9, 18)
    store, owner, cal = setup(tmp_path, start=final, end=AT.date())
    owner.commit([attempt("final-empty", day=final, rows=[]), attempt("daily")])
    saved = fixed_input(store, owner.fixed(), cal, start=final, end=DAY, evaluated_at=AT)
    result = materialize_saved_input(
        store, saved, output=tmp_path / "subset.sqlite", start=DAY, end=DAY
    )
    assert result["coverage_status"] == "complete"
    assert result["unresolved_days"] == []


def test_saved_input_future_time_and_corrupt_pack_fail_closed(tmp_path):
    store, owner, cal = setup(tmp_path)
    owner.commit([attempt("one")])
    with pytest.raises(ValueError, match="evaluation clock"):
        fixed_input(
            store, owner.fixed(), cal, start=DAY, end=DAY, evaluated_at=AT - timedelta(seconds=1)
        )
    saved = fixed_input(store, owner.fixed(), cal, start=DAY, end=DAY, evaluated_at=AT)
    for key in list(store.values):
        if key.startswith(PREFIX + "packs/"):
            store.values[key].body = b"corrupt"
    from baibai_engine.storage.objects.r2 import LakePublishError

    with pytest.raises(LakePublishError, match="integrity"):
        materialize_saved_input(store, saved, output=tmp_path / "bad.sqlite")
    assert not (tmp_path / "bad.sqlite").exists()


@pytest.mark.parametrize("day", [DAY, date(2026, 9, 18)])
def test_commit_work_never_calls_empty_balance_success_and_uses_saved_calendar(
    tmp_path, monkeypatch, capsys, day
):
    from baibai_jobs.acquisition import jquants_margin_interest_native as module

    class Store(MemoryR2Store):
        def close(self):
            pass

    store = Store()
    owner = MarginInterestL1(store, tmp_path / "work")
    owner.initialize()
    cal = calendar(store, tmp_path / "cal", day, day)
    report = acquire_margin_interest_native(
        owner, cal, asof=AT.date(), start=day, end=day, client=Client(rows=[]), clock=lambda: AT
    )
    assert report["incomplete"] == 1
    store.values.pop(CALENDAR_HEAD)
    monkeypatch.setattr(module, "Boto3R2Store", lambda **_: store)
    assert module.main(["commit-work", "--directory", str(owner.directory)]) == 1
    assert json.loads(capsys.readouterr().out)["remaining"]["days"] == [day.isoformat()]
    owner.commit([attempt("recovered", day=day, at=AT + timedelta(hours=1))])
    assert module.main(["commit-work", "--directory", str(owner.directory)]) == 0


def test_native_calendar_does_not_replace_saved_price_days_in_history(tmp_path):
    from baibai_engine.machine_analysis.observations.trading_activity.margin_reader import (
        published_margin_balance_dates,
    )

    store, owner, cal = setup(tmp_path, end=date(2026, 9, 30))
    owner.commit([attempt("one")])
    saved = fixed_input(store, owner.fixed(), cal, start=DAY, end=DAY, evaluated_at=AT)
    db = tmp_path / "history.sqlite"
    materialize_saved_input(store, saved, output=db)
    assert published_margin_balance_dates(db, date(2026, 9, 30)) == []
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO jquants_daily_bars(ticker,traded_at,close) VALUES ('1301','2026-09-29',1)"
        )
    # The calendar knew Sept 28, but this frozen historical view did not have its bar.
    assert published_margin_balance_dates(db, date(2026, 9, 29)) == []
    assert published_margin_balance_dates(db, date(2026, 9, 30)) == [(DAY, "daily")]
    owner.commit(
        [
            attempt(
                "publication",
                at=AT + timedelta(hours=1),
                rows=records(publication=date(2026, 9, 28)),
            )
        ]
    )
    saved = fixed_input(
        store, owner.fixed(), cal, start=DAY, end=DAY, evaluated_at=AT + timedelta(hours=1)
    )
    explicit = tmp_path / "explicit.sqlite"
    materialize_saved_input(store, saved, output=explicit)
    assert published_margin_balance_dates(explicit, date(2026, 9, 28)) == []
    assert published_margin_balance_dates(explicit, date(2026, 9, 29)) == [(DAY, "daily")]


def test_cli_fixed_calendar_and_saved_input_work_without_current_heads(tmp_path, monkeypatch):
    from baibai_jobs import cli
    from baibai_jobs.acquisition import jquants_margin_interest_native as acquisition
    from baibai_jobs.integration import margin_interest_native as integration

    class Store(MemoryR2Store):
        def close(self):
            pass

    store = Store()
    calendar(store, tmp_path / "calendar", DAY, DAY)
    monkeypatch.setattr(acquisition, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(integration, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(acquisition, "JQuantsHTTP", lambda _: Client())
    directory = tmp_path / "work"
    assert (
        cli.main(["jquants-margin-interest-native", "initialize", "--directory", str(directory)])
        == 0
    )
    args = [
        "--directory",
        str(directory),
        "--asof",
        DAY.isoformat(),
        "--start",
        DAY.isoformat(),
        "--end",
        DAY.isoformat(),
    ]
    assert cli.main(["jquants-margin-interest-native", "plan", *args]) == 0
    assert cli.main(["jquants-margin-interest-native", "acquire", *args]) == 0
    assert (
        cli.main(["jquants-margin-interest-native", "commit-work", "--directory", str(directory)])
        == 0
    )
    output = tmp_path / "out"
    args = [
        "--work-directory",
        str(directory),
        "--start",
        DAY.isoformat(),
        "--end",
        DAY.isoformat(),
    ]
    assert (
        cli.main(
            [
                "rebuild-margin-interest-native",
                *args,
                "--output",
                str(output),
                "--calendar-head",
                str(directory / "calendar-fixed.json"),
            ]
        )
        == 0
    )
    store.values.pop(HEAD_KEY)
    store.values.pop(CALENDAR_HEAD)
    assert (
        cli.main(
            [
                "rebuild-margin-interest-native",
                *args,
                "--output",
                str(tmp_path / "cold"),
                "--saved-input",
                str(output / "input.json"),
            ]
        )
        == 0
    )
