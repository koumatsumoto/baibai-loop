"""Calendar keeps source range planning, partial originals and explicit recovery."""

import json
from datetime import timedelta

from tests.engine.data_collection.providers.jquants.test_calendar_native import AT, DAY, attempt
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.calendar_native import CalendarL1, interval
from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsPage,
    JQuantsRateLimitError,
)
from baibai_jobs.acquisition.jquants_calendar_native import acquire_calendar_native, calendar_window


class Client:
    def __init__(self, *, at=AT, omit=(), error=None):
        self.at, self.omit, self.error, self.calls = at, omit, error, []

    def pages(self, source, query):
        from datetime import date

        self.calls.append((source, query))
        rows = [
            {"Date": day.isoformat(), "HolDiv": "1"}
            for day in interval(date.fromisoformat(query["from"]), date.fromisoformat(query["to"]))
            if day not in self.omit
        ]
        envelope = {"data": rows}
        if self.error:
            envelope["pagination_key"] = "next"
        yield JQuantsPage(source, query, self.at, json.dumps(envelope).encode())
        if self.error:
            raise self.error


def test_initial_window_and_retained_ancient_gap_keep_53_day_scope(tmp_path):
    store = MemoryR2Store()
    owner = CalendarL1(store, tmp_path)
    owner.initialize()
    first, last = calendar_window(owner.fixed(), DAY)
    assert first == DAY - timedelta(days=7)
    assert last == DAY + timedelta(days=45)
    assert len(interval(first, last)) == 53
    client = Client()
    result = acquire_calendar_native(owner, start=first, end=last, client=client, clock=lambda: AT)
    assert result["changed"] == 1
    assert result["incomplete"] == 0
    assert client.calls == [
        ("market_calendar", {"from": first.strftime("%Y%m%d"), "to": last.strftime("%Y%m%d")})
    ]
    next_owner = CalendarL1(store, tmp_path / "next")
    second = Client(at=AT + timedelta(days=1))
    first2, last2 = calendar_window(next_owner.fixed(), DAY + timedelta(days=1))
    assert first2 == first
    assert last2 == last + timedelta(days=1)
    acquire_calendar_native(next_owner, start=first2, end=last2, client=second)
    assert second.calls == [
        ("market_calendar", {"from": last2.strftime("%Y%m%d"), "to": last2.strftime("%Y%m%d")})
    ]
    ancient = DAY - timedelta(days=400)
    next_owner.commit([attempt("ancient-empty", ancient, hour=30, rows=[])])
    assert calendar_window(next_owner.fixed(), DAY)[0] == ancient
    assert ancient in next_owner.fixed().plan(start=ancient, end=DAY)


def test_missing_dates_become_bounded_ranges_not_refetch_of_fulfilled_overlap(tmp_path):
    store = MemoryR2Store()
    owner = CalendarL1(store, tmp_path)
    owner.initialize()
    end = DAY + timedelta(days=4)
    gaps = (DAY + timedelta(days=1), DAY + timedelta(days=3))
    first = Client(omit=gaps)
    assert acquire_calendar_native(owner, start=DAY, end=end, client=first)["incomplete"] == 2
    second = Client(at=AT + timedelta(hours=1))
    result = acquire_calendar_native(
        CalendarL1(store, tmp_path / "retry"), start=DAY, end=end, client=second
    )
    assert result["incomplete"] == 0
    assert [query for _, query in second.calls] == [
        {"from": day.strftime("%Y%m%d"), "to": day.strftime("%Y%m%d")} for day in gaps
    ]


def test_partial_429_is_durable_and_stops_all_later_ranges(tmp_path):
    store = MemoryR2Store()
    owner = CalendarL1(store, tmp_path)
    owner.initialize()
    owner.commit([attempt("middle", DAY + timedelta(days=1))])
    client = Client(at=AT + timedelta(hours=1), error=JQuantsRateLimitError("stop"))
    result = acquire_calendar_native(owner, start=DAY, end=DAY + timedelta(days=2), client=client)
    assert result["halted"] == result["failed"] == 1
    assert len(client.calls) == 1
    assert {item.request_id for item in owner.fixed().requests(store)} >= {"middle"}
    assert result["incomplete"] == 2


def test_commit_work_replays_without_fetch_and_does_not_fulfill_empty(
    tmp_path, monkeypatch, capsys
):
    import baibai_jobs.acquisition.jquants_calendar_native as cli

    class Store(MemoryR2Store):
        def close(self):
            pass

    store = Store()
    owner = CalendarL1(store, tmp_path)
    owner.initialize()
    empty = attempt("empty", rows=[])
    (tmp_path / "attempt-empty.json").write_text(json.dumps(empty.value()))
    owner.commit([empty])
    monkeypatch.setattr(cli, "Boto3R2Store", lambda **_: store)
    assert cli.main(["commit-work", "--directory", str(tmp_path)]) == 1
    assert json.loads(capsys.readouterr().out)["unresolved"] == [DAY.isoformat()]
    owner.commit([attempt("recover", hour=1)])
    assert cli.main(["commit-work", "--directory", str(tmp_path)]) == 0
