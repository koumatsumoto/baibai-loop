from __future__ import annotations

import gzip
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx2 import ByteStream, MockTransport, Request, Response
from tests.helpers.us_research_admission import make_current_us, observe_alpaca_us
from tests.jobs.integration.test_us_daily_plan import (
    _CUTOFF,
    _ESTABLISHED,
    _FRI,
    _MON,
    _attach,
    _store,
)
from tests.jobs.integration.test_us_daily_prices import _CALENDAR_ROWS, _payload
from tests.tools.test_us_research_report import _draft, _write
from tools.experiments.us_research_mvp import _adjacent_loader, _fixed_alpaca_capture
from tools.experiments.us_research_report import _calendar_proof
from tools.experiments.us_research_report import main as research_cli

from baibai_engine.investment_judgment.us_proposal import sip_daily_close_fields
from baibai_engine.machine_analysis.observations.prices.alpaca_normalization import (
    normalize_alpaca_daily_bars,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    encode_current_listing_master,
    read_local_listing_master,
)
from baibai_engine.storage.paths import APPLICATION_DB_PATH
from baibai_engine.storage.sqlite.application.write import initialize_database
from baibai_jobs.integration import source_handoff, us_daily_handoff
from baibai_web.materialize import export_read_models

_PAPER_AUTH = ("fixture-paper-id", "fixture-paper-secret")
_ENV = {
    "APCA_API_KEY_ID": _PAPER_AUTH[0],
    "APCA_API_SECRET_KEY": _PAPER_AUTH[1],
}
_PRIVATE_HEADER = "test-only-unshareable-header"
_SATURDAY_SLOT = datetime(2026, 11, 28, 7, 43, tzinfo=UTC)  # 16:43 JST, 02:43 NY


def _whole(request, value, *, status=200, fields=None, reported_request=None):
    body = json.dumps(value, separators=(",", ":")).encode()
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Content-Length": str(len(body)),
        "Set-Cookie": _PRIVATE_HEADER,
    } | (fields or {})
    return Response(
        status, request=reported_request or request, headers=headers, stream=ByteStream(body)
    )


@pytest.fixture
def invoke(tmp_path, monkeypatch, capsys):
    for name, value in _ENV.items():
        monkeypatch.setenv(name, value)
    for name in ("APCA_PAPER_API_KEY_ID", "APCA_PAPER_API_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APCA_API_BASE_URL", "https://not-the-owner.invalid")
    monkeypatch.setenv("HTTPS_PROXY", "https://untrusted-proxy.invalid")
    pinned = tmp_path / "current.json"
    pinned.write_bytes(encode_current_listing_master({"US": _attach(_store())}))
    directory = tmp_path / "acquire-output"
    monkeypatch.setattr(us_daily_handoff, "_now", lambda: _CUTOFF)

    def run(handler, *, expected=_FRI, command="acquire", current=None):
        if current is not None:
            pinned.write_bytes(encode_current_listing_master({"US": current}))
        calls = []

        def checked(request):
            parts = urlsplit(str(request.url))
            assert request.method == "GET"
            assert request.headers["accept-encoding"] == "identity"
            assert (parts.scheme, parts.netloc, parts.path) in (
                ("https", "paper-api.alpaca.markets", "/v2/calendar"),
                ("https", "data.alpaca.markets", "/v2/stocks/bars"),
            )
            assert (
                request.headers["APCA-API-KEY-ID"],
                request.headers["APCA-API-SECRET-KEY"],
            ) == _PAPER_AUTH
            calls.append(request)
            return handler(request)

        def transport(*, trust_env, retries) -> MockTransport:
            assert (trust_env, retries) == (False, 0)
            return MockTransport(checked)

        monkeypatch.setattr(us_daily_handoff, "HTTPTransport", transport)
        result = source_handoff.main(
            [
                command,
                "--source",
                "alpaca-us-daily",
                "--asof",
                expected.isoformat(),
                "--current-listing-master",
                str(pinned),
                "--directory",
                str(directory),
            ]
        )
        output = capsys.readouterr()
        assert all(t not in output.out + output.err for t in (*_ENV.values(), _PRIVATE_HEADER))
        assert "https://" not in (output.out + output.err)
        return result, output, calls, directory, pinned

    return run


def _calendar_only(request):
    assert urlsplit(str(request.url)).path == "/v2/calendar"
    return _whole(request, _CALENDAR_ROWS)


def test_existing_handoff_entrypoint_replays_exact_one_date_terminal_native_packet(
    invoke, tmp_path, capsys
):
    first_bar = True

    def get(request):
        nonlocal first_bar
        parts = urlsplit(str(request.url))
        query = parse_qs(parts.query)
        if parts.path == "/v2/calendar":
            assert query == {
                "start": [(_MON - timedelta(days=7)).isoformat()],
                "end": [_MON.isoformat()],
                "date_type": ["TRADING"],
            }
            return _calendar_only(request)
        assert query["start"] == query["end"] == [_FRI.isoformat()]
        assert (query["feed"], query["adjustment"], query["asof"], query["currency"]) == (
            ["sip"],
            ["raw"],
            ["-"],
            ["USD"],
        )
        symbols = query["symbols"][0].split(",")
        if first_bar:
            first_bar = False
            return _whole(request, _payload(symbols[:7], next_page="exact_cursor"))
        if "page_token" in query:
            assert query["page_token"] == ["exact_cursor"]
            return _whole(request, _payload(symbols[7:]))
        return _whole(request, _payload(symbols))

    assets = tuple(
        (("XNYS" if symbol == "BRK.B" else "XNAS", symbol), number)
        for number, symbol in enumerate(_ESTABLISHED, start=1)
    )
    prior = observe_alpaca_us(assets, at=_CUTOFF - timedelta(days=3))
    current = observe_alpaca_us(assets, at=_CUTOFF - timedelta(minutes=1), previous=prior)
    code, output, calls, directory, pinned = invoke(get, current=current)
    assert code == 0
    assert output.err == ""
    assert len(calls) == 5
    summary = json.loads(output.out)
    assert summary["status"] == "complete"
    assert summary["received"] == summary["expected"] == 42
    assert summary["listing_master_sha256"] == sha256(pinned.read_bytes()).hexdigest()
    assert not list(directory.rglob("*.sqlite*"))
    assert directory.stat().st_mode & 0o077 == 0
    report = json.loads((directory / "result.json").read_text())
    calendar = _calendar_proof(directory / report["calendar"])
    assert calendar.session(_FRI).shortened is True
    assert calendar.received_at == _CUTOFF
    for batch in report["batches"]:
        manifest = directory / batch["proof"]
        assert batch["proof_sha256"] == sha256(manifest.read_bytes()).hexdigest()
        spec = json.loads(manifest.read_text())
        assert spec["start"] == spec["end"] == _FRI.isoformat()
        for page in spec["pages"]:
            assert page["request_url"] == page["response_url"]
            assert page["headers"].keys() == {"content-length", "content-type"}
            assert (
                page["sha256"] == sha256((manifest.parent / page["file"]).read_bytes()).hexdigest()
            )
        assert (
            json.loads((manifest.parent / spec["pages"][-1]["file"]).read_bytes())[
                "next_page_token"
            ]
            is None
        )
        fixed = _fixed_alpaca_capture(
            spec,
            tuple(spec["symbols"]),
            _adjacent_loader(manifest),
            _CUTOFF,
            adjustment="raw",
            feed="sip",
        )
        assert not isinstance(fixed, str)
        assert len(normalize_alpaca_daily_bars(fixed)) == batch["expected"]
        if "BRK.B" in spec["symbols"]:
            _app_roundtrip(tmp_path, capsys, pinned, manifest, calendar, fixed)


def _app_roundtrip(tmp_path, capsys, pinned, manifest, calendar, fixed):
    # Use the generated files with the unchanged real authoring entry point.
    def when(minutes):
        return (_CUTOFF + timedelta(minutes=minutes)).isoformat()

    raw = _draft(ticker="BRK.B")
    raw.pop("iex_band")
    raw.pop("official_close")
    raw.update(
        cutoff_at=when(0),
        prepared_at=when(1),
        daily_close=sip_daily_close_fields(
            fixed,
            ticker="BRK.B",
            session_et=_FRI,
            session_close_at=calendar.session(_FRI).close_at,
            cutoff_at=_CUTOFF,
        ),
    )
    context = [
        "--current-listing-master",
        str(pinned),
        "--calendar-proof",
        str(manifest.parent.parent / "calendar.json"),
        "--daily-close-proof",
        str(manifest),
    ]
    draft = _write(tmp_path / "research-draft.json", raw)
    initial = ["draft-digest", draft, *context]
    at = _CUTOFF + timedelta(minutes=1)
    report = manifest.parent.parent / "result.json"
    original_result = report.read_bytes()
    report.unlink()
    assert research_cli(initial, now=at) == 2
    assert capsys.readouterr().err == "FAIL draft\n"
    report.write_bytes(original_result)
    original_proof = manifest.read_bytes()
    manifest.write_bytes(original_proof + b"\n")
    assert research_cli(initial, now=at) == 2
    assert capsys.readouterr().err == "FAIL draft\n"
    manifest.write_bytes(original_proof)
    assert research_cli(initial, now=at) == 0
    binding = json.loads(capsys.readouterr().out)
    replacement = tmp_path / "same-label-other-instrument.json"
    another = ((("XNYS", "BRK.B"), 999),)
    prior = observe_alpaca_us(another, at=_CUTOFF - timedelta(days=3))
    current = observe_alpaca_us(another, at=_CUTOFF, previous=prior)
    replacement.write_bytes(encode_current_listing_master({"US": current}))
    wrong_context = [context[0], str(replacement), *context[2:]]
    assert (
        research_cli(["draft-digest", draft, *wrong_context], now=_CUTOFF + timedelta(minutes=1))
        == 2
    )
    assert capsys.readouterr().err == "FAIL draft\n"
    analysis_review = _write(
        tmp_path / "analysis-review.json",
        {
            **binding,
            "reviewer_identity": "synthetic analyst peer",
            "reviewed_at": when(2),
            "conclusion": "pass",
            "notes": "合成の出典・値を確認。",
        },
    )
    analysis_path = tmp_path / "analysis.json"
    analysis = str(analysis_path)
    assert (
        research_cli(
            ["save", draft, analysis_review, "--output", analysis, *wrong_context],
            now=_CUTOFF + timedelta(minutes=3),
        )
        == 2
    )
    assert capsys.readouterr().err == "FAIL draft\n"
    assert not analysis_path.exists()
    assert (
        research_cli(
            ["save", draft, analysis_review, "--output", analysis, *context],
            now=_CUTOFF + timedelta(minutes=3),
        )
        == 0
    )
    decision = _write(
        tmp_path / "decision.json",
        {
            "proposal_id": "us-proposal-synthetic-local-handoff",
            "author_identity": "synthetic decision author",
            "prepared_at": when(4),
            "action": "no_action",
            "reason": "合成の比較で見送り。",
            "revisit_when": "根拠更新時。",
        },
    )
    # The formal decision still re-checks semantic identity rather than byte equality.
    repin = tmp_path / "same-instruments-refreshed.json"
    listings = read_local_listing_master(pinned)
    listings["US"] = replace(
        listings["US"], received_at=listings["US"].received_at + timedelta(seconds=1)
    )
    repin.write_bytes(encode_current_listing_master(listings))
    assert repin.read_bytes() != pinned.read_bytes()
    formal_context = [context[0], str(repin), *context[2:]]
    assert (
        research_cli(
            ["proposal-digest", analysis, decision, *formal_context],
            now=_CUTOFF + timedelta(minutes=4),
        )
        == 0
    )
    proposal_binding = json.loads(capsys.readouterr().out)
    decision_review = _write(
        tmp_path / "decision-review.json",
        {
            **proposal_binding,
            "reviewer_identity": "synthetic judgment peer",
            "reviewed_at": when(5),
            "conclusion": "pass",
            "notes": "合成の判断を独立確認。",
        },
    )
    db = tmp_path / APPLICATION_DB_PATH
    initialize_database(db)
    assert (
        research_cli(
            [
                "proposal-save",
                analysis,
                decision,
                decision_review,
                "--db",
                str(db),
                *formal_context,
            ],
            now=_CUTOFF + timedelta(minutes=6),
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == proposal_binding
    manifest.unlink()
    assert (
        research_cli(
            ["proposal-show", "--db", str(db), "--proposal-id", proposal_binding["proposal_id"]]
        )
        == 0
    )
    output = capsys.readouterr()
    saved = json.loads(output.out)
    assert output.err == ""
    assert saved["review"]["draft_sha256"] == proposal_binding["draft_sha256"]
    assert saved["analysis"]["draft"]["daily_close"] == raw["daily_close"]
    assert "next_page_token" not in output.out
    assert all(token not in output.out for token in (*_ENV.values(), _PRIVATE_HEADER))
    views = tmp_path / "serving"
    export_read_models(tmp_path, views)
    served = json.loads((views / "views/latest-us-proposal.json").read_bytes())["proposal"]
    assert served["proposal_id"] == proposal_binding["proposal_id"]
    assert served["draft_sha256"] == proposal_binding["draft_sha256"]
    assert served["reason"] == saved["decision"]["reason"]


@pytest.mark.parametrize(
    ("clock", "master_day", "admitted"),
    [
        (_SATURDAY_SLOT, _FRI, True),  # Saturday after a shortened Friday.
        (_SATURDAY_SLOT, _FRI - timedelta(days=2), False),
        (_CUTOFF, _FRI, False),  # Open Monday before the close needs Monday's universe.
    ],
)
def test_raw_daily_uses_the_calendar_to_select_the_listing_session(
    invoke, monkeypatch, clock, master_day, admitted
):
    monkeypatch.setattr(us_daily_handoff, "_now", lambda: clock)
    current = make_current_us(
        venue="XNYS", ticker="BRK.B", day=master_day, identity_since=master_day
    )

    def receive(request):
        parts = urlsplit(str(request.url))
        query = parse_qs(parts.query)
        if parts.path == "/v2/calendar":
            return _whole(
                request, [row for row in _CALENDAR_ROWS if row["date"] <= query["end"][0]]
            )
        assert admitted
        assert (query["start"], query["end"], query["adjustment"], query["symbols"]) == (
            [_FRI.isoformat()],
            [_FRI.isoformat()],
            ["raw"],
            ["BRK.B"],
        )
        return _whole(request, _payload(["BRK.B"]))

    code, output, calls, directory, _ = invoke(receive, current=current)
    assert len(calls) == 1 + int(admitted)  # The calendar, then (only for an exact scope) SIP.
    if not admitted:
        assert code == 1
        assert not directory.exists()
        return
    assert code == 0
    result = json.loads(output.out)
    assert (result["asof"], result["listing_session"]) == (_FRI.isoformat(), _FRI.isoformat())
    assert result["status"] == "complete"
    assert result["received"] == result["expected"] == 1
    calendar = _calendar_proof(directory / "calendar.json")
    assert calendar.is_open(_FRI + timedelta(days=1)) is False
    assert calendar.session(_FRI).shortened is True
    assert (directory / "batch-001" / "capture.json").is_file()


def test_explicit_nonlatest_date_fails_before_sip_call_and_no_packet(invoke):
    code, output, calls, directory, _ = invoke(_calendar_only, expected=_MON)
    assert code == 1
    assert json.loads(output.out)["status"] == "session_or_scope_unavailable"
    assert len(calls) == 1
    assert not directory.exists()


def test_paper_calendar_denial_stops_before_sip_and_no_packet(invoke):
    code, output, calls, directory, _ = invoke(
        lambda request: _whole(request, {"denial": _PRIVATE_HEADER}, status=403)
    )
    assert code == 1
    assert len(calls) == 1
    assert json.loads(output.out)["status"] == "calendar_unavailable"
    assert json.loads(output.out)["http_status"] == 403
    assert not directory.exists()


@pytest.mark.parametrize(
    "missing",
    [
        ("APCA_API_KEY_ID",),
        ("APCA_API_SECRET_KEY",),
        ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"),
    ],
)
def test_missing_or_partial_pair_is_refused_before_network_without_alias_fallback(
    invoke, monkeypatch, missing
):
    monkeypatch.setenv("APCA_PAPER_API_KEY_ID", "unused-fixture-id")
    monkeypatch.setenv("APCA_PAPER_API_SECRET_KEY", "unused-fixture-secret")
    for name in missing:
        monkeypatch.delenv(name)
    code, output, calls, directory, _ = invoke(lambda _: pytest.fail("unexpected GET"))
    assert code == 1
    assert calls == []
    assert output.out == ""
    assert output.err == "source handoff failed: ValueError\n"
    assert not directory.exists()


@pytest.mark.parametrize(
    "kind", ["redirect", "wrong_url", "truncated", "compressed", "no_terminal"]
)
def test_non_whole_or_nonterminal_wire_never_produces_valid_handoff(invoke, kind):
    def get(request):
        if urlsplit(str(request.url)).path == "/v2/calendar":
            return _calendar_only(request)
        symbols = parse_qs(urlsplit(str(request.url)).query)["symbols"][0].split(",")
        data = _payload(symbols)
        if kind == "redirect":
            return _whole(
                request, {}, status=302, fields={"Location": "https://not-allowed.invalid"}
            )
        if kind == "wrong_url":
            return _whole(
                request,
                data,
                reported_request=Request("GET", "https://not-allowed.invalid"),
            )
        if kind == "truncated":
            return _whole(request, data, fields={"Content-Length": "1"})
        if kind == "compressed":
            compressed = gzip.compress(json.dumps(data).encode())
            return Response(
                200,
                request=request,
                headers={"Content-Encoding": "gzip"},
                stream=ByteStream(compressed),
            )
        data.pop("next_page_token")
        return _whole(request, data)

    code, output, calls, directory, _ = invoke(get)
    assert code == 1
    assert json.loads(output.out)["status"] == "unavailable"
    assert len(calls) == 4
    assert not directory.exists()


def test_partial_with_valid_terminal_pages_is_not_full_and_does_not_revive_sql_stage(invoke):
    def get(request):
        if urlsplit(str(request.url)).path == "/v2/calendar":
            return _calendar_only(request)
        symbols = parse_qs(urlsplit(str(request.url)).query)["symbols"][0].split(",")
        return _whole(request, _payload(symbols[:-1]))

    code, output, calls, directory, _ = invoke(get)
    assert code == 1
    assert len(calls) == 4
    assert json.loads(output.out)["missing"] == 3
    assert json.loads(output.out)["status"] == "partial"
    assert (directory / "result.json").exists()
    assert not list(directory.rglob("*.sqlite*"))
    stage, stage_output, attempted, _, _ = invoke(
        lambda _: pytest.fail("local packet must not be staged through market SQL"),
        command="stage",
    )
    assert stage == 1
    assert stage_output.err
    assert attempted == []
