"""Exercise the real offline command over fixed native-receipt fixture files."""

from __future__ import annotations

import json
import socket
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from urllib.parse import urlencode
from uuid import UUID

import pytest
from tests.jobs.integration.test_us_technical_input import (
    _CUTOFF,
    _FRI,
    _MON,
    _calendars,
    _capture,
    _listing,
)
from tools.experiments.us_technical_check import main

from baibai_engine.data_collection.providers.alpaca import capture_daily_bars
from baibai_engine.data_collection.providers.alpaca_calendar import CALENDAR_URL
from baibai_engine.data_collection.providers.alpaca_http import BARS_BODY_MAX_BYTES
from baibai_engine.primitives.json import canonical_json
from baibai_jobs.integration.us_technical_handoff import TECHNICAL_PAGE_LIMIT, technical_request_id

_ATTEMPT = UUID("11111111-1234-4321-8734-111111111111")


def _write(path, value):
    body = canonical_json(value).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return sha256(body).hexdigest()


def _native_calendar(reading):
    start, end = reading.start.isoformat(), reading.end.isoformat()
    query = urlencode({"start": start, "end": end, "date_type": "TRADING"})
    body = json.dumps(
        [
            {
                "date": row.trading_date.isoformat(),
                "open": row.open_at.strftime("%H:%M"),
                "close": row.close_at.strftime("%H:%M"),
            }
            for row in reading.sessions
        ],
        indent=2,
    )
    return {
        "start": start,
        "end": end,
        "request_url": f"{CALENDAR_URL}?{query}",
        "response_url": f"{CALENDAR_URL}?{query}",
        "received_at": reading.received_at,
        "status": 200,
        "complete": True,
        "redirected": False,
        "body": body,
        "sha256": sha256(body.encode()).hexdigest(),
    }


def _make_packet(tmp_path, *, body_capture=None):
    # Simulate the capture owner writing files once, using the same replay protocol.
    directory, listing = tmp_path / "packet", tmp_path / "master.json"
    listing.write_bytes(_listing())
    listing_sha = sha256(listing.read_bytes()).hexdigest()
    calendar_sha = [
        _write(directory / f"calendar-{index:03d}.json", _native_calendar(reading))
        for index, reading in enumerate(_calendars(), 1)
    ]
    capture = _capture() if body_capture is None else body_capture
    pages = iter(page.response for page in capture.pages)
    captured = capture_daily_bars(
        replace(capture.query, page_limit=TECHNICAL_PAGE_LIMIT),
        request_id=technical_request_id(_ATTEMPT, listing_sha=listing_sha, as_of=_FRI, ordinal=1),
        transport=lambda _: next(pages),
    )
    batch = directory / "batch-001"
    batch.mkdir()
    records = []
    for page in captured.pages:
        name = f"page-{page.page_number:02d}.json"
        (batch / name).write_bytes(page.response.body)
        records.append(
            {
                "file": name,
                "status": page.response.status_code,
                "request_url": page.request_url,
                "response_url": page.request_url,
                "received_at": page.response.received_at,
                "complete": True,
                "redirected": False,
                "sha256": page.body_sha256,
                "receipt_id": page.receipt_id,
            }
        )
    proof = {
        "request_id": captured.request_id,
        "start": captured.query.start,
        "end": captured.query.end,
        "symbols": captured.query.symbols,
        "pages": records,
    }
    committed = {
        "source": "alpaca-us-technical",
        "attempt_id": str(_ATTEMPT),
        "listing_master_sha256": listing_sha,
        "asof": _FRI,
        "cutoff_at": _CUTOFF,
        "calendars": calendar_sha,
        "batches": [_write(batch / "capture.json", proof)],
    }
    _write(directory / "result.json", committed)
    return directory, listing, committed, proof


def _invoke(packet, listing):
    return main(["--packet", str(packet), "--current-listing-master", str(listing)])


def test_exact_complete_packet_has_aggregate_only_and_no_network_or_mutation(
    tmp_path, capsys, monkeypatch
):
    directory, listing, _, _ = _make_packet(tmp_path)
    files_before = {
        f: (f.stat().st_mtime_ns, sha256(f.read_bytes()).hexdigest())
        for f in tmp_path.rglob("*")
        if f.is_file()
    }

    def no_network(*args, **kwargs):
        pytest.fail("offline verifier attempted network access")

    monkeypatch.setattr(socket, "socket", no_network)
    assert _invoke(directory, listing) == 0
    result = capsys.readouterr()
    assert json.loads(result.out) == {
        "status": "admitted",
        "price_session": _FRI.isoformat(),
        "listing_session": _MON.isoformat(),
        "models": 1,
    }
    assert result.err == ""
    assert files_before == {
        f: (f.stat().st_mtime_ns, sha256(f.read_bytes()).hexdigest())
        for f in tmp_path.rglob("*")
        if f.is_file()
    }


def test_same_ticker_replacement_cannot_be_relabelled_by_editing_the_commit_and_request_id(
    tmp_path, capsys
):
    directory, listing, committed, proof = _make_packet(tmp_path)
    replacement = _listing(instrument_id="other-current-valid-id")
    listing.write_bytes(replacement)
    assert _invoke(directory, listing) == 2
    # Recompute the committed listing digest and leave the earlier capture intact.
    new_sha = sha256(replacement).hexdigest()
    committed["listing_master_sha256"] = new_sha
    _write(directory / "result.json", committed)
    assert _invoke(directory, listing) == 2
    # Even changing the request ID and rehashing its manifest does not match the
    # original per-page receipt identities saved by the capture.
    proof["request_id"] = technical_request_id(_ATTEMPT, listing_sha=new_sha, as_of=_FRI, ordinal=1)
    committed["batches"] = [_write(directory / "batch-001" / "capture.json", proof)]
    _write(directory / "result.json", committed)
    assert _invoke(directory, listing) == 2
    result = capsys.readouterr()
    assert not result.out
    assert "other-current-valid-id" not in result.err


@pytest.mark.parametrize(
    "child",
    [
        "calendar-001.json",
        "batch-001/capture.json",
        "batch-001/page-02.json",
        "result.json",
    ],
)
def test_editing_any_committed_file_cannot_pass(tmp_path, capsys, child):
    directory, listing, _, _ = _make_packet(tmp_path)
    file = directory / child
    if child == "result.json":
        file.unlink()
    else:
        file.write_bytes(file.read_bytes() + b" ")
    assert _invoke(directory, listing) == 2
    assert capsys.readouterr().err == "US technical receipt packet invalid\n"


@pytest.mark.parametrize(
    "options", [{"adjustment": "raw"}, {"feed": "iex"}, {"omit_terminal": True}]
)
def test_not_split_sip_or_an_unfinished_wire_does_not_pass(tmp_path, options):
    directory, listing, _, _ = _make_packet(tmp_path, body_capture=_capture(**options))
    assert _invoke(directory, listing) == 2


def test_entire_terminal_scope_without_latest_observation_returns_unavailable(tmp_path, capsys):
    directory, listing, _, _ = _make_packet(tmp_path, body_capture=_capture(absent=(_FRI,)))
    assert _invoke(directory, listing) == 1
    assert json.loads(capsys.readouterr().out) == {"status": "unavailable"}


def test_calendar_wire_cannot_declare_an_oversized_inclusive_window(tmp_path, capsys):
    directory, listing, committed, _ = _make_packet(tmp_path)
    calendar = directory / "calendar-001.json"
    record = json.loads(calendar.read_bytes())
    first = _calendars()[0]
    record["end"] = (first.end + timedelta(days=1)).isoformat()
    query = urlencode({"start": record["start"], "end": record["end"], "date_type": "TRADING"})
    record["request_url"] = record["response_url"] = f"{CALENDAR_URL}?{query}"
    committed["calendars"][0] = _write(calendar, record)
    _write(directory / "result.json", committed)
    assert _invoke(directory, listing) == 1
    assert json.loads(capsys.readouterr().out) == {"status": "unavailable"}


def test_wire_and_file_route_stay_bounded(tmp_path):
    directory, listing, committed, proof = _make_packet(tmp_path)
    proof_path = directory / "batch-001" / "capture.json"
    body = proof_path.parent / "page-01.json"
    large = body.read_bytes().ljust(BARS_BODY_MAX_BYTES + 1, b" ")
    body.write_bytes(large)
    proof["pages"][0]["sha256"] = sha256(large).hexdigest()
    committed["batches"] = [_write(proof_path, proof)]
    _write(directory / "result.json", committed)
    assert _invoke(directory, listing) == 2
    proof["pages"][0]["file"] = "../master.json"
    committed["batches"] = [_write(proof_path, proof)]
    _write(directory / "result.json", committed)
    assert _invoke(directory, listing) == 2
