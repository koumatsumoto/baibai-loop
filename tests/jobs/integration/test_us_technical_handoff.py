"""Exercise both private split SIP commands through a single injected read-only transport."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from urllib.parse import parse_qs, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from httpx2 import ByteStream, MockTransport, Response
from tests.helpers.us_research_admission import make_current_us
from tests.jobs.integration.test_alpaca_research_scope import _CLOSE, _asset, _receipt
from tests.jobs.integration.test_us_technical_input import _CUTOFF, _DAYS, _FRI, _MON, _listing
from tools.experiments.us_technical_check import check_packet
from tools.experiments.us_technical_check import main as check

from baibai_engine.data_collection.providers.alpaca_calendar import CALENDAR_WINDOW_MAX_DAYS
from baibai_engine.data_collection.providers.alpaca_http import BARS_BODY_MAX_BYTES
from baibai_engine.machine_analysis.observations.stocks.alpaca_listing_input import (
    prepare_alpaca_research_inventory,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    active_us_listing_sha256,
    decode_current_listing_master,
    encode_current_listing_master,
)
from baibai_jobs.integration import source_handoff, us_technical_handoff
from baibai_jobs.integration.us_technical_handoff import (
    TECHNICAL_CALENDAR_CHUNKS,
    TECHNICAL_PAGE_LIMIT,
    acquire_us_technical,
)

_NY = ZoneInfo("America/New_York")
_CASH_SCOPE_START = _DAYS[-19]
_AUTH = ("fixture-paper-id", "fixture-paper-secret")
_DAILY_OPEN = (*_DAYS, _MON)


def _mixed_listing(*, listing_day=_MON):
    current = decode_current_listing_master(_listing(day=listing_day, second=True))["US"]
    extra = make_current_us(
        venue="XNAS",
        ticker="EXT",
        day=listing_day,
        identity_since=_CASH_SCOPE_START,
        instrument_id="id-newer",
    )
    rows = (*current.slice.rows, *extra.slice.rows)
    ids = {row.key: row.instrument_id for row in rows if row.instrument_id is not None}
    assert current.rakuten is not None
    assert extra.rakuten is not None
    current = replace(
        current,
        slice=replace(current.slice, rows=rows),
        received_count=len(rows),
        rakuten=replace(
            current.rakuten,
            catalog_count=len(rows),
            active_listing_sha256=active_us_listing_sha256(ids),
            rows=(*current.rakuten.rows, *extra.rakuten.rows),
        ),
    )
    return encode_current_listing_master({"US": current})


def _body(request, data, *, status=200):
    raw = (json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    assert len(raw) <= BARS_BODY_MAX_BYTES
    return Response(
        status,
        request=request,
        headers={"Content-Type": "application/json", "Content-Length": str(len(raw))},
        stream=ByteStream(raw),
    )


def _provider(
    calendar_requests,
    bars_requests,
    data_pages,
    *,
    failure=None,
    no_calendar=False,
    after_bar=lambda: None,
):
    def get(request):
        parts = urlsplit(str(request.url))
        assert request.method == "GET"
        assert parts.scheme == "https"
        assert (request.headers["apca-api-key-id"], request.headers["apca-api-secret-key"]) == _AUTH
        assert request.headers["accept-encoding"] == "identity"
        opts = parse_qs(parts.query, strict_parsing=True)
        start, end = [date.fromisoformat(opts[name][0]) for name in ("start", "end")]
        if parts.path == "/v2/calendar":
            assert parts.netloc == "paper-api.alpaca.markets"
            assert opts["date_type"] == ["TRADING"]
            assert (end - start).days + 1 == CALENDAR_WINDOW_MAX_DAYS
            calendar_requests.append((start, end))
            if failure == "calendar_denied":
                return _body(request, {}, status=403)
            rows = [
                {
                    "date": day.isoformat(),
                    "open": "09:30",
                    "close": "13:00" if day == _FRI else "16:00",
                }
                for day in _DAILY_OPEN
                if start <= day <= end and not no_calendar
            ]
            return _body(request, rows)
        assert (parts.netloc, parts.path) == ("data.alpaca.markets", "/v2/stocks/bars")
        assert opts["feed"] == ["sip"]
        assert opts["adjustment"] == ["split"]
        assert opts["currency"] == ["USD"]
        assert opts["timeframe"] == ["1Day"]
        assert opts["asof"] == ["-"]
        assert opts["limit"] == [str(TECHNICAL_PAGE_LIMIT)]
        symbols = tuple(opts["symbols"][0].split(","))
        first = "page_token" not in opts
        if not first:
            assert opts["page_token"] == ["more"]
        bars_requests.append((symbols, start, end, first))
        if failure == "bars_denied":
            return _body(request, {}, status=403)
        if failure == "later_group_rate_limited" and symbols == ("EXT",):
            return _body(request, {}, status=429)
        expected = [
            (symbol, day)
            for symbol in symbols
            for day in _DAILY_OPEN
            if start <= day <= end and not (failure == "missing_last" and day == end)
        ]
        count = TECHNICAL_PAGE_LIMIT + int(failure == "oversized_page")
        values = expected[:count] if first else expected[TECHNICAL_PAGE_LIMIT:]
        by_symbol = {}
        for symbol, day in values:
            record = {
                "t": datetime.combine(day, time.min, tzinfo=_NY).astimezone(UTC).isoformat(),
                "o": 140,
                "h": 151,
                "l": 135,
                "c": 141 + day.day / 10,
                "v": 210,
            }
            by_symbol.setdefault(symbol, []).append(record)
        payload = {"bars": by_symbol, "currency": "USD"}
        if failure != "missing_terminal":
            payload["next_page_token"] = (
                "more" if first and len(expected) > TECHNICAL_PAGE_LIMIT else None
            )
        assert parts.query
        data_pages.append((symbols, len(values)))
        after_bar()
        return _body(request, payload)

    return MockTransport(get)


@pytest.fixture
def run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("APCA_API_KEY_ID", _AUTH[0])
    monkeypatch.setenv("APCA_API_SECRET_KEY", _AUTH[1])
    monkeypatch.setenv("APCA_API_BASE_URL", "https://not-a-service.invalid")
    monkeypatch.setenv("HTTPS_PROXY", "https://not-a-proxy.invalid")
    listing = tmp_path / "pin.json"
    listing.write_bytes(_mixed_listing())
    directory = tmp_path / "receipt"

    def invoke(
        *, expected=_FRI, command="acquire", pinned=True, pin_bytes=None, cutoff=_CUTOFF, **options
    ):
        if pin_bytes is not None:
            listing.write_bytes(pin_bytes)
        monkeypatch.setattr(us_technical_handoff, "_now", lambda: cutoff)
        calendars, bars, parts = [], [], []

        def transport(*, trust_env, retries):
            assert (trust_env, retries) == (False, 0)
            return _provider(calendars, bars, parts, **options)

        monkeypatch.setattr(us_technical_handoff, "HTTPTransport", transport)
        args = [
            command,
            "--source",
            "alpaca-us-technical",
            "--directory",
            str(directory),
            "--asof",
            expected.isoformat(),
        ]
        if pinned:
            args += ["--current-listing-master", str(listing)]
        if command in {"stage", "download"}:
            args += ["--stage-id", "us-technical-private-test"]
        if command == "export":
            args += ["--since", _CUTOFF.isoformat()]
        status = source_handoff.main(args)
        output = capsys.readouterr()
        assert all(value not in output.out + output.err for value in _AUTH)
        assert "https://" not in (output.out + output.err)
        return status, output, directory, listing, calendars, bars, parts

    return invoke


def test_real_private_acquire_then_offline_check_with_calendar_chunks_and_exact_split_ranges(
    run, capsys
):
    exit_code, output, directory, listing, calendars, bars, parts = run()
    assert exit_code == 0
    assert len(calendars) < TECHNICAL_CALENDAR_CHUNKS
    assert len(calendars) > 1
    assert calendars[0][1] == _MON
    assert all(
        older_end + timedelta(days=1) == newer_start
        for (newer_start, _), (_, older_end) in pairwise(calendars)
    )
    assert bars == [
        (("BRK.B", "GOOGL"), _DAYS[0], _FRI, True),
        (("BRK.B", "GOOGL"), _DAYS[0], _FRI, False),
        (("EXT",), _CASH_SCOPE_START, _FRI, True),
    ]
    assert [rows for _, rows in parts] == [TECHNICAL_PAGE_LIMIT, 6, 19]
    assert json.loads(output.out) == {
        "asof": _FRI.isoformat(),
        "batches": 2,
        "source": "alpaca-us-technical",
        "status": "complete",
        "targets": 3,
    }
    assert directory.stat().st_mode & 0o077 == 0
    assert not list(directory.rglob("*.sqlite*"))
    assert not list(directory.parent.glob(".us-tech-*"))
    assert all(
        path.stat().st_size <= BARS_BODY_MAX_BYTES for path in directory.rglob("page-*.json")
    )
    assert check(["--packet", str(directory), "--current-listing-master", str(listing)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "listing_session": _MON.isoformat(),
        "models": 3,
        "price_session": _FRI.isoformat(),
        "status": "admitted",
    }


def _alpaca_research_pin(*, with_old_history=True):
    late = _CLOSE + timedelta(minutes=10)
    latest = (_asset(1, "AAPL"), _asset(3, "REUSE", "NYSE", tradable=False))
    previous = None
    if with_old_history:
        previous = prepare_alpaca_research_inventory(
            _receipt(
                _asset(1, "AAPL"),
                _asset(2, "REUSE", "NYSE"),
                received_at=datetime.combine(_DAYS[0], time(11), tzinfo=_NY),
            )
        ).current
    changed = prepare_alpaca_research_inventory(
        _receipt(*latest, received_at=late), previous=previous
    ).current
    monday = prepare_alpaca_research_inventory(
        _receipt(*latest, received_at=_CUTOFF - timedelta(minutes=1)), previous=changed
    ).current
    assert monday.rakuten is monday.nasdaq_directories is None
    return encode_current_listing_master({"US": monday})


@pytest.mark.parametrize("after_next_close", [False, True])
def test_current_alpaca_pin_reaches_existing_split_and_offline_l2(run, capsys, after_next_close):
    price_session = _MON if after_next_close else _FRI
    cutoff = datetime(2026, 11, 30, 21, 30, tzinfo=UTC) if after_next_close else _CUTOFF
    exit_code, output, packet, listing, _, bars, _ = run(
        pin_bytes=_alpaca_research_pin(), expected=price_session, cutoff=cutoff
    )
    assert exit_code == 0
    assert json.loads(output.out)["status"] == "complete"
    expected_start = _DAYS[1] if after_next_close else _DAYS[0]
    expected_queries = [(("AAPL",), expected_start, price_session, True)]
    if after_next_close:
        expected_queries.append((("REUSE",), _MON, _MON, True))
    assert bars == expected_queries
    assert check(["--packet", str(packet), "--current-listing-master", str(listing)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "listing_session": _MON.isoformat(),
        "models": 2 if after_next_close else 1,
        "price_session": price_session.isoformat(),
        "status": "admitted",
    }
    observed = check_packet(packet, listing)
    assert observed is not None
    stable = observed.models[0]
    assert stable.subject == f"us:alpaca:{UUID(int=1)}"
    assert stable.payload["history_scope_start"] == expected_start.isoformat()
    assert stable.payload["price_source"] == "alpaca_sip_daily_split"
    if after_next_close:
        new = observed.models[1]
        assert new.subject == f"us:alpaca:{UUID(int=3)}"
        assert new.payload["ticker"] == "REUSE"
        assert new.payload["primary_mic"] == "XNYS"
        assert new.payload["bar_count"] == 1
        assert new.payload["history_scope_start"] == _MON.isoformat()
        assert new.payload["moving_averages"]["sma_20bar"]["status"] == "insufficient_history"


def test_new_scope_after_the_verified_short_close_never_fetches_older_prices(run):
    exit_code, output, packet, _, _, bars, _ = run(
        pin_bytes=_alpaca_research_pin(with_old_history=False),
    )
    assert exit_code == 1
    assert json.loads(output.out)["status"] == "scope_unavailable"
    assert not bars
    assert not packet.exists()


@pytest.mark.parametrize(
    "failure",
    ["calendar_denied", "bars_denied", "missing_latest", "missing_terminal", "oversized_page"],
)
def test_no_packet_when_an_authoritative_chain_is_not_complete(run, failure):
    exit_code, output, directory, _, calendars, bars, _ = run(
        failure="missing_last" if failure == "missing_latest" else failure
    )
    assert exit_code == 1
    assert not directory.exists()
    assert not list(directory.parent.glob(".us-tech-*"))
    if failure == "calendar_denied":
        assert len(calendars) == 1
        assert not bars
        assert json.loads(output.out)["status"] == "calendar_unavailable"
    else:
        assert len(bars) == (2 if failure == "missing_latest" else 1)
        assert json.loads(output.out)["status"] == "incomplete"


def test_later_group_rate_limit_does_not_publish_earlier_completed_group(run):
    status, output, directory, listing, _, bars, _ = run(failure="later_group_rate_limited")
    assert [(symbols, first_page) for symbols, _, _, first_page in bars] == [
        (("BRK.B", "GOOGL"), True),
        (("BRK.B", "GOOGL"), False),
        (("EXT",), True),
    ]
    assert status == 1
    assert json.loads(output.out)["status"] == "incomplete"
    assert set(directory.parent.iterdir()) == {listing}


def test_mismatched_session_and_known_insufficient_calendar_never_request_prices(run):
    exit_code, output, directory, _, _, bars, _ = run(expected=_MON)
    assert exit_code == 1
    assert json.loads(output.out)["status"] == "session_unavailable"
    assert not directory.exists()
    assert not bars
    exit_code, output, directory, _, calendars, bars, _ = run(no_calendar=True)
    assert exit_code == 1
    assert json.loads(output.out)["status"] == "calendar_unavailable"
    assert len(calendars) == TECHNICAL_CALENDAR_CHUNKS
    assert not bars
    assert not directory.exists()


@pytest.mark.parametrize(
    ("command", "pinned"),
    [
        ("stage", True),
        ("export", True),
        ("download", True),
        ("replay-copy", True),
        ("acquire", False),
    ],
)
def test_split_packet_is_only_reachable_by_pinned_acquire(
    run, command, pinned, tmp_path, monkeypatch
):
    def unexpected_io(*args, **kwargs):
        pytest.fail("private technical acquisition must not touch a handoff store or database")

    monkeypatch.setattr(source_handoff, "Boto3R2Store", unexpected_io)
    monkeypatch.setattr(source_handoff, "_replay", unexpected_io)
    monkeypatch.setattr(source_handoff, "_export", unexpected_io)
    actions_out = tmp_path / "actions-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(actions_out))
    exit_code, output, directory, _, calendars, bars, _ = run(command=command, pinned=pinned)
    assert exit_code == 1
    assert output.err == "source handoff failed: ValueError\n"
    assert not directory.exists()
    assert not actions_out.exists()
    assert not calendars
    assert not bars


def test_acquisition_crossing_new_york_close_cannot_relabel_an_older_series(tmp_path):
    listing, directory = tmp_path / "pin.json", tmp_path / "receipt"
    listing.write_bytes(_mixed_listing())
    now = datetime.combine(_MON, time(20, 59), tzinfo=UTC)
    calendars, bars, parts = [], [], []

    def after_bar():
        nonlocal now
        now += timedelta(minutes=1)

    summary = acquire_us_technical(
        listing_master=listing,
        directory=directory,
        expected_session=_FRI,
        credentials=us_technical_handoff.AlpacaCredentials(*_AUTH),
        transport=_provider(calendars, bars, parts, after_bar=after_bar),
        clock=lambda: now,
    )
    assert summary["status"] == "session_changed"
    assert all(end == _FRI for _, _, end, _ in bars)
    assert not directory.exists()
    assert not list(directory.parent.glob(".us-tech-*"))


def test_weekend_receives_previous_confirmed_short_session_without_a_weekend_bar(tmp_path):
    listing, directory = tmp_path / "pin.json", tmp_path / "receipt"
    listing.write_bytes(_mixed_listing(listing_day=_FRI))
    saturday = _FRI + timedelta(days=1)
    now = datetime.combine(saturday, time(14), tzinfo=UTC)
    calendars, bars, parts = [], [], []
    summary = acquire_us_technical(
        listing_master=listing,
        directory=directory,
        expected_session=_FRI,
        credentials=us_technical_handoff.AlpacaCredentials(*_AUTH),
        transport=_provider(calendars, bars, parts),
        clock=lambda: now,
    )
    assert summary["status"] == "complete"
    assert calendars[0][1] == saturday
    assert all(end == _FRI for _, _, end, _ in bars)
    assert check(["--packet", str(directory), "--current-listing-master", str(listing)]) == 0
