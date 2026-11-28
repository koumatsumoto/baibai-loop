"""Run the real JP-master and market-calendar native owners through source handoff."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.r2_store import MemoryR2Store
from tests.helpers.screening_sqlite import make_master_records

from baibai_engine.data_collection.providers.jquants import http
from baibai_engine.data_collection.providers.jquants.calendar_native import CalendarL1
from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsPage,
    JQuantsRateLimitError,
)
from baibai_engine.data_collection.providers.jquants.master_native import PREFIX, MasterL1
from baibai_engine.storage.snapshots import objects
from baibai_jobs.acquisition import (
    jquants_calendar_native,
    jquants_margin_alerts_native,
    jquants_margin_interest_native,
    jquants_master_native,
    jquants_price_native,
    jquants_short_sale_reports_native,
)
from baibai_jobs.integration import jp_listing_refresh, source_handoff
from baibai_jobs.integration.listing_master_current import (
    LISTING_MASTER_CURRENT_KEY,
    read_current_listing_master,
)

_DAY = date(2026, 11, 27)
_EVALUATED = datetime(2026, 11, 27, 9, tzinfo=UTC)
_RECEIVED = _EVALUATED - timedelta(minutes=1)


class _Store(MemoryR2Store):
    def get_checked_object(self, key, *, max_bytes):
        item = self.get_bytes_with_metadata(key)
        assert item is None or len(item[0]) <= max_bytes
        return item


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return _EVALUATED.astimezone(tz)


class _StartClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return (_RECEIVED - timedelta(seconds=2)).astimezone(tz)


class _Client:
    def __init__(self, session, case):
        self.session, self.case, self.calls = session, case, []

    def close(self):
        pass

    def pages(self, source, params):
        self.calls.append(source)
        session = self.session.isoformat()
        query = f"{self.session:%Y%m%d}"
        if source == "market_calendar":
            assert params == {"from": query, "to": query}
            division = {"closed": "0", "half": "2", "ambiguous-calendar": "unused"}.get(
                self.case, "1"
            )
            yield JQuantsPage(
                source,
                params,
                _RECEIVED,
                json.dumps({"data": [{"Date": session, "HolDiv": division}]}).encode(),
            )
            return
        assert source == "master"
        assert params == {"date": query}
        if self.case == "quota":
            raise JQuantsRateLimitError("private test body")
        if self.case == "closed":
            yield JQuantsPage(source, params, _RECEIVED, b'{"data":[]}')
            return
        day = self.session + timedelta(days=1) if self.case == "wrong-day" else self.session
        rows = make_master_records(day)
        yield JQuantsPage(
            source,
            params,
            _RECEIVED - timedelta(seconds=1),
            json.dumps({"data": rows[:2000], "pagination_key": "next"}).encode(),
        )
        if self.case != "partial":
            yield JQuantsPage(
                source,
                {**params, "pagination_key": "next"},
                _RECEIVED,
                json.dumps({"data": rows[2000:]}).encode(),
            )


@pytest.mark.parametrize(
    ("case", "status", "code"),
    [
        ("open", "updated", 0),
        ("half", "updated", 0),
        ("closed", "closed", 0),
        ("wrong-day", "unavailable", 1),
        ("partial", "unavailable", 1),
        ("ambiguous-calendar", "unavailable", 1),
        ("quota", "provider_halted", 1),
        ("historical", "not_current_session", 0),
    ],
)
def test_native_current_write_reuses_exact_capture_not_a_second_master_replay(
    tmp_path,
    monkeypatch,
    capsys,
    case,
    status,
    code,
):
    session = _DAY - timedelta(days=1) if case == "historical" else _DAY
    store = _Store()
    CalendarL1(store, tmp_path / "init-calendar").initialize()
    MasterL1(store, tmp_path / "init-master").initialize()
    store.store_object("lake/pointers/market/current.json", b"untouched")
    client = _Client(session, case)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setattr(source_handoff, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(http, "JQuantsHTTP", lambda *_: client)
    monkeypatch.setattr(source_handoff, "datetime", _Clock)
    for module in (jquants_master_native, jquants_calendar_native):
        monkeypatch.setattr(module, "datetime", _StartClock)
    monkeypatch.setattr(jquants_calendar_native, "calendar_window", lambda *_: (session, session))
    monkeypatch.setattr(objects, "open_lake", lambda **_: pytest.fail("attempted Lake restore"))
    observed = []
    for module, name in (
        (jquants_price_native, "acquire_price_native"),
        (jquants_margin_alerts_native, "acquire_margin_alerts_native"),
        (jquants_short_sale_reports_native, "acquire_short_sale_reports_native"),
        (jquants_margin_interest_native, "acquire_margin_interest_native"),
    ):

        def done(*_args, name=name, **_kwargs):
            observed.append(name)
            return {
                "failed": 0,
                "held": 0,
                "incomplete": 0,
                "remaining": {"unknown_calendar_days": [], "unknown_incomplete_requests": []},
            }

        monkeypatch.setattr(module, name, done)
    refresh = jp_listing_refresh.refresh_current_jp_listings
    examined = []

    def check_no_replay(*args, **kwargs):
        before = len(store.get_keys)
        try:
            return refresh(*args, **kwargs)
        finally:
            assert not any(key.startswith(PREFIX) for key in store.get_keys[before:])
            examined.append(True)

    monkeypatch.setattr(jp_listing_refresh, "refresh_current_jp_listings", check_no_replay)

    def run(directory):
        return source_handoff.main(
            [
                "acquire",
                "--source",
                "jquants-market",
                "--asof",
                session.isoformat(),
                "--directory",
                str(directory),
            ]
        )

    directory = tmp_path / "route"
    assert run(directory) == code
    report = json.loads((directory / "result.json").read_bytes())
    assert report["listing_master_current"]["status"] == status
    assert client.calls == ["market_calendar", "master"]
    assert len(examined) == int(case not in {"quota", "historical"})
    assert store.get_bytes("lake/pointers/market/current.json") == b"untouched"
    assert not store.delete_keys
    assert "private test body" not in capsys.readouterr().err
    assert len(observed) == (1 if case == "quota" else 4)
    if status == "updated":
        result = read_current_listing_master(store)["JP"]
        assert result.slice.session == _DAY
        assert result.received_at == _RECEIVED
        assert result.received_count == 2500
        assert store.put_keys.count(LISTING_MASTER_CURRENT_KEY) == 1
        second = tmp_path / "repeat"
        assert run(second) == 0
        assert json.loads((second / "result.json").read_bytes())["listing_master_current"] == {
            "status": "unchanged"
        }
        assert client.calls == ["market_calendar", "master"]
        assert store.put_keys.count(LISTING_MASTER_CURRENT_KEY) == 1
    else:
        assert LISTING_MASTER_CURRENT_KEY not in store.values
