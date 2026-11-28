"""Exercise the production US job entry point with fake authorized provider and R2."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx2 import MockTransport
from tests.jobs.integration.test_alpaca_research_scope import _asset
from tests.jobs.integration.test_listing_master_current import _JP, _refresh, _Store
from tests.jobs.integration.test_us_daily_handoff import _ENV, _PRIVATE_HEADER, _whole
from tests.jobs.integration.test_us_daily_prices import _payload

from baibai_engine.data_collection.providers.alpaca_assets import AlpacaAssetsReceipt
from baibai_engine.data_collection.providers.alpaca_sip_native import read_alpaca_sip_day
from baibai_engine.machine_analysis.observations.stocks.alpaca_listing_input import (
    prepare_alpaca_research_inventory,
)
from baibai_jobs.integration import us_market_daily
from baibai_jobs.integration.listing_master_current import (
    LISTING_MASTER_CURRENT_KEY,
    apply_listing_market_update,
    read_current_listing_master,
)

_FRI = date(2026, 11, 27)
_MON = date(2026, 11, 30)
_FRI_ORIGIN = datetime(2026, 11, 27, 13, 43, tzinfo=UTC)
_MON_ORIGIN = datetime(2026, 11, 30, 13, 43, tzinfo=UTC)
_SHORT_CLOSE = datetime(2026, 11, 27, 18, tzinfo=UTC)
_ROWS = [
    _asset(1, "AAPL", name="fixture-display"),
    _asset(2, "ETF", "ARCA", tradable=False),
    _asset(3, "ZZZZ", "OTC"),
]
_CALENDAR = {
    _FRI: {"date": _FRI.isoformat(), "open": "09:30", "close": "13:00"},
    _MON: {"date": _MON.isoformat(), "open": "09:30", "close": "16:00"},
}
_UNRELATED = "lake/pointers/market/current.json"


class _NativeStore(_Store):
    def close(self):
        pass


@pytest.fixture
def run(monkeypatch, capsys):
    for name, value in _ENV.items():
        monkeypatch.setenv(name, value)
    store = _NativeStore()
    _refresh(store, "JP", (_JP,))
    store.store_object(_UNRELATED, b"do not write Machine")
    original_jp = read_current_listing_master(store)["JP"]

    def invoke(
        stamp=_MON_ORIGIN,
        *,
        received_at=_FRI_ORIGIN,
        origin=None,
        event="schedule",
        calendar_status=200,
        bars_status=200,
        quoted=("AAPL", "ETF"),
        close_after_assets=None,
        pricing_before_assets=False,
    ):
        if received_at is not None:
            old = AlpacaAssetsReceipt(received_at, json.dumps(_ROWS).encode())
            apply_listing_market_update(
                store,
                "US",
                lambda previous: prepare_alpaca_research_inventory(old, previous=previous).current,
            )
        calls = []
        clock = [stamp]

        def provider(request):
            path = urlsplit(str(request.url)).path
            calls.append(path)
            assert request.method == "GET"
            assert request.headers["APCA-API-KEY-ID"] == _ENV["APCA_API_KEY_ID"]
            if path == "/v2/calendar":
                params = parse_qs(urlsplit(str(request.url)).query)
                assert len(params["end"]) == 1
                start, end = map(date.fromisoformat, (params["start"][0], params["end"][0]))
                rows = [row for day, row in _CALENDAR.items() if start <= day <= end]
                return _whole(request, rows, status=calendar_status)
            if path == "/v2/assets":
                if pricing_before_assets:
                    saved = read_alpaca_sip_day(store, session=_FRI)
                    assert saved is not None
                    assert not saved.day.unverified
                if close_after_assets is not None:
                    clock[:] = [close_after_assets]
                return _whole(request, _ROWS)
            assert path == "/v2/stocks/bars"
            query = parse_qs(urlsplit(str(request.url)).query)
            assert query["start"] == query["end"] == [_FRI.isoformat()]
            assert query["asof"] == ["-"]
            assert query["feed"] == ["sip"]
            assert query["adjustment"] == ["raw"]
            assert query["symbols"] == ["AAPL,ETF"]
            return _whole(request, _payload(quoted), status=bars_status)

        def transport(*, trust_env, retries):
            assert (trust_env, retries) == (False, 0)
            return MockTransport(provider)

        def bucket(*, bucket, verify_put_with_head):
            assert bucket == "baibai-stores"
            assert verify_put_with_head is False
            return store

        monkeypatch.delenv("R2_STORES_BUCKET", raising=False)
        monkeypatch.setattr(us_market_daily, "HTTPTransport", transport)
        monkeypatch.setattr(us_market_daily, "Boto3R2Store", bucket)
        monkeypatch.setattr(us_market_daily, "_now", lambda: clock[0])
        result = us_market_daily.main(
            ["--run-created-at", (origin or stamp).isoformat(), "--event", event]
        )
        output = capsys.readouterr()
        assert not output.err
        assert all(
            s not in output.out for s in (*_ENV.values(), _PRIVATE_HEADER, "fixture-display")
        )
        assert read_current_listing_master(store)["JP"] == original_jp
        assert store.values[_UNRELATED].body == b"do not write Machine"
        assert not store.delete_keys
        return result, json.loads(output.out), calls, store

    return invoke


def test_monday_saves_friday_short_close_before_refresh_and_retry_does_not_reacquire(run):
    result, report, calls, store = run(quoted=("AAPL",), pricing_before_assets=True)
    assert result == 0
    assert calls == ["/v2/calendar", "/v2/stocks/bars", "/v2/assets"]
    assert report == {
        "sip_prices": {
            "status": "saved",
            "session": _FRI.isoformat(),
            "expected": 2,
            "observed": 1,
            "missing": 1,
            "unverified": 0,
        },
        "listing_master": {"status": "updated", "session": _MON.isoformat(), "received_assets": 3},
    }
    saved = read_alpaca_sip_day(store, session=_FRI)
    assert saved is not None
    assert saved.day.closed_at == _SHORT_CLOSE
    assert saved.day.bars[0].symbol == "AAPL"
    assert read_current_listing_master(store)["US"].slice.session == _MON
    assert store.put_keys[-1] == LISTING_MASTER_CURRENT_KEY
    before = store.put_keys.copy()
    result, report, calls, _ = run(received_at=None)
    assert result == 0
    assert calls == ["/v2/calendar"]
    assert report["sip_prices"]["status"] == "already_saved"
    assert report["listing_master"]["status"] == "already_current"
    assert store.put_keys == before


def test_weekend_has_no_master_update_and_reuses_last_closed_friday(run):
    stamp = datetime(2026, 11, 28, 13, 43, tzinfo=UTC)
    result, report, calls, store = run(stamp)
    assert result == 0
    assert calls == ["/v2/calendar", "/v2/stocks/bars"]
    assert report["listing_master"] == {"status": "market_closed", "session": "2026-11-28"}
    assert read_current_listing_master(store)["US"].slice.session == _FRI


@pytest.mark.parametrize("prior", [None, _SHORT_CLOSE])
def test_first_or_late_master_never_invents_a_previous_session_price(run, prior):
    result, report, calls, store = run(received_at=prior)
    assert result == 1
    assert calls == ["/v2/calendar", "/v2/assets"]
    assert report["sip_prices"]["status"] == "preclose_master_unavailable"
    assert report["listing_master"]["status"] == "updated"
    assert read_alpaca_sip_day(store, session=_FRI) is None


def test_price_permission_denial_remains_visible_while_master_moves_forward(run):
    result, report, calls, store = run(bars_status=403)
    assert result == 1
    assert calls == ["/v2/calendar", "/v2/stocks/bars", "/v2/assets"]
    assert report["sip_prices"]["status"] == "prices_unavailable"
    assert report["sip_prices"]["http_errors"] == [403]
    assert report["listing_master"]["status"] == "updated"
    assert read_current_listing_master(store)["US"].slice.session == _MON
    assert read_alpaca_sip_day(store, session=_FRI) is None


def test_calendar_failure_does_not_acquire_prices_or_mutate_master(run):
    result, report, calls, store = run(calendar_status=403)
    assert result == 1
    assert calls == ["/v2/calendar"]
    assert report["status"] == "calendar_unavailable"
    assert report["http_status"] == 403
    assert read_current_listing_master(store)["US"].slice.session == _FRI


def test_delayed_retry_cannot_pick_a_newer_close_or_relabel_a_new_ny_date(run):
    stamp = datetime(2026, 11, 27, 18, 1, tzinfo=UTC)
    result, report, calls, store = run(stamp, origin=_FRI_ORIGIN)
    assert result == 1
    assert calls == ["/v2/calendar"]
    assert report["sip_prices"]["status"] == "run_late"
    assert report["listing_master"]["status"] == "already_current"
    assert read_alpaca_sip_day(store, session=_FRI) is None

    stamp = datetime(2026, 11, 28, 5, tzinfo=UTC)
    result, report, calls, _ = run(stamp, origin=_FRI_ORIGIN, received_at=None)
    assert result == 1
    assert not calls
    assert report["status"] == "failed"


def test_master_receipt_crossing_the_close_is_not_saved_for_that_day(run):
    result, report, calls, store = run(close_after_assets=datetime(2026, 11, 30, 21, tzinfo=UTC))
    assert result == 1
    assert calls == ["/v2/calendar", "/v2/stocks/bars", "/v2/assets"]
    assert report["sip_prices"]["status"] == "saved"
    assert report["listing_master"]["status"] == "refresh_window_closed"
    assert read_current_listing_master(store)["US"].slice.session == _FRI
