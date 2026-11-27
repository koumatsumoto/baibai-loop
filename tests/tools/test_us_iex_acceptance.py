"""Exercise the manual IEX command with a synthetic single-hop transport."""

from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx2 import ByteStream, ConnectError, MockTransport, Response, SyncByteStream
from tools.experiments import us_iex_acceptance as cli

_SYMBOLS = ("AAPL", "GOOG", "GOOGL", "NVDA")
_EXPECTED = "2026-11-27"
_BAR_TIME = "2026-11-27T05:00:00Z"
_BOUNDARY = datetime.fromisoformat("2026-11-28T05:00:00Z")
_BEFORE_BOUNDARY = datetime.fromisoformat("2026-11-28T04:59:59.999999Z")
_KEYS = {"APCA_API_KEY_ID": "test-only-key-id", "APCA_API_SECRET_KEY": "test-only-secret"}
_SENTINEL = "not-for-run-log"


def _page(symbols=_SYMBOLS, *, date_label=_BAR_TIME, token=None):
    return {
        "bars": {
            symbol: [{"t": date_label, "o": 101.125 + i, "h": 162, "l": 73, "c": 106, "v": 2000}]
            for i, symbol in enumerate(symbols)
        },
        "next_page_token": token,
    }


def _response(page, *, status=200, headers=None):
    body = json.dumps(page).encode()
    return Response(status, headers=headers, stream=ByteStream(body))


@pytest.fixture
def run_operator(monkeypatch, capsys):
    for name, value in _KEYS.items():
        monkeypatch.setenv(name, value)
    # Neither a local environment override nor a paper host may change the target.
    monkeypatch.setenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted.example.test")

    def run(*responses, expected=_EXPECTED, times=(_BOUNDARY,), called=None):
        calls = [] if called is None else called
        clock_ticks = iter(times)
        monkeypatch.setattr(cli, "_now", lambda: next(clock_ticks, times[-1]))
        replies = iter(responses)

        def respond(request):
            calls.append(request)
            response = next(replies)
            if isinstance(response, Exception):
                raise response
            return response

        def build_transport(*, trust_env, retries):
            assert trust_env is False
            assert retries == 0
            return MockTransport(respond)

        monkeypatch.setattr(cli, "HTTPTransport", build_transport)
        code = cli.main([f"--expected-date={expected}"])
        output, error = capsys.readouterr()
        assert error == ""
        assert _SENTINEL not in output
        assert all(value not in output for value in _KEYS.values())
        assert "101.125" not in output
        assert "https://" not in output
        return code, output, calls

    return run


def test_complete_cli_checks_all_four_classes_and_keeps_no_raw_files(
    run_operator, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    cursor = "x&feed=sip&&page=other"
    code, output, calls = run_operator(
        _response(_page(_SYMBOLS[:2], token=cursor)),
        _response(_page(_SYMBOLS[2:])),
    )
    assert code == 0
    assert output == "iex_session=2026-11-27 status=accepted symbols_ok=4/4\n"
    assert not list(tmp_path.iterdir())
    assert len(calls) == 2
    for i, request in enumerate(calls):
        url = urlsplit(str(request.url))
        assert (request.method, url.scheme, url.netloc, url.path) == (
            "GET",
            "https",
            "data.alpaca.markets",
            "/v2/stocks/bars",
        )
        query = parse_qs(url.query)
        assert query.pop("page_token", []) == ([cursor] if i else [])
        assert query == {
            "symbols": ["AAPL,GOOG,GOOGL,NVDA"],
            "timeframe": ["1Day"],
            "start": ["2026-11-27"],
            "end": ["2026-11-28"],
            "limit": ["10000"],
            "adjustment": ["split"],
            "asof": ["-"],
            "feed": ["iex"],
            "currency": ["USD"],
            "sort": ["asc"],
        }
        assert request.headers["APCA-API-KEY-ID"] == _KEYS["APCA_API_KEY_ID"]
        assert request.headers["APCA-API-SECRET-KEY"] == _KEYS["APCA_API_SECRET_KEY"]
        assert request.headers["Accept-Encoding"] == "identity"


@pytest.mark.parametrize("now", [datetime.fromisoformat("2026-11-27T18:00:00Z"), _BEFORE_BOUNDARY])
def test_same_new_york_date_never_calls_alpaca_even_after_short_day_close(run_operator, now):
    assert run_operator(times=(now,)) == (
        2,
        "iex_session=2026-11-27 status=awaiting_ny_date symbols_ok=0/4\n",
        [],
    )


@pytest.mark.parametrize("bad", ["", "20261127", "2026-02-30", _SENTINEL])
def test_invalid_date_is_redacted_and_never_calls_alpaca(run_operator, bad):
    assert run_operator(expected=bad) == (
        2,
        "iex_session=- status=invalid_date symbols_ok=0/4\n",
        [],
    )


@pytest.mark.parametrize("bad", [None, "", "two words", "line\nbreak"])
def test_missing_or_malformed_auth_never_calls_alpaca(run_operator, monkeypatch, bad):
    if bad is None:
        monkeypatch.delenv("APCA_API_SECRET_KEY")
    else:
        monkeypatch.setenv("APCA_API_SECRET_KEY", bad)
    assert run_operator() == (
        2,
        "iex_session=2026-11-27 status=missing_credentials symbols_ok=0/4\n",
        [],
    )


def test_final_later_page_cannot_make_same_ny_day_observations_eligible(run_operator):
    code, output, calls = run_operator(
        _response(_page(_SYMBOLS[:2], token="page-two")),
        _response(_page(_SYMBOLS[2:])),
        times=(_BOUNDARY, _BEFORE_BOUNDARY, _BOUNDARY),
    )
    assert len(calls) == 2
    assert (code, output) == (1, "iex_session=2026-11-27 status=missing_symbols symbols_ok=2/4\n")


def test_closed_day_empty_is_not_acceptance(run_operator):
    code, output, _ = run_operator(_response(_page(())), expected="2026-11-26")
    assert (code, output) == (1, "iex_session=2026-11-26 status=missing_symbols symbols_ok=0/4\n")


def test_adjacent_day_cannot_replace_missing_target_or_share_class(run_operator):
    page = _page(("AAPL", "GOOG", "NVDA"))
    page["bars"].update(_page(("GOOGL",), date_label="2026-11-28T05:00:00Z")["bars"])
    code, output, _ = run_operator(_response(page))
    assert (code, output) == (1, "iex_session=2026-11-27 status=missing_symbols symbols_ok=3/4\n")


@pytest.mark.parametrize("status", [206, 302, 401, 403, 429, 503])
def test_non_200_surfaces_only_status_never_follows_or_retries(run_operator, status):
    code, output, calls = run_operator(
        _response(
            {"message": _SENTINEL}, status=status, headers={"Location": "https://elsewhere.test"}
        ),
    )
    assert code == 1
    assert (
        output == f"iex_session=2026-11-27 status=http_status symbols_ok=0/4 http_status={status}\n"
    )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "headers",
    [{"Content-Range": "bytes 0-100/1000"}, {"Content-Encoding": "gzip"}, {"Content-Length": "1"}],
)
def test_unverified_wire_never_enables_observations(run_operator, headers):
    code, output, _ = run_operator(_response(_page(), headers=headers))
    assert (code, output) == (1, "iex_session=2026-11-27 status=transport_error symbols_ok=0/4\n")


def test_partial_and_oversize_bodies_cannot_be_used(run_operator):
    class Interrupted(SyncByteStream):
        def __iter__(self):
            yield json.dumps(_page()).encode()
            raise ConnectError(_SENTINEL)

    for stream in (Interrupted(), ByteStream(b"X" * (128 * 1024 + 1))):
        code, output, calls = run_operator(Response(200, stream=stream))
        assert len(calls) == 1
        assert (code, output) == (
            1,
            "iex_session=2026-11-27 status=transport_error symbols_ok=0/4\n",
        )


@pytest.mark.parametrize(
    ("make", "status"),
    [
        (lambda: {"bars": _page()["bars"]}, "invalid_cursor"),
        (lambda: _page(("AAPL", "GOOG", "GOOGL", "7203")), "scope_mismatch"),
        (lambda: _page(date_label="2026-11-26T05:00:00Z"), "invalid_observations"),
    ],
)
def test_bad_page_scope_and_nonterminal_or_out_of_window_data_never_accept(
    run_operator, make, status
):
    code, output, calls = run_operator(_response(make()))
    assert len(calls) == 1
    assert (code, output) == (1, f"iex_session=2026-11-27 status={status} symbols_ok=0/4\n")
