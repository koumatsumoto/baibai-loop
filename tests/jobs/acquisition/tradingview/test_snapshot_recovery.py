"""Bounded source-only recovery from independently retained same-cycle requests."""

import asyncio
from datetime import timedelta

import pytest
from mcp.types import CallToolResult
from tests.helpers.tradingview import DAY, NOW, _sdk, native_inputs, payload, retain_result, store

from baibai_jobs.acquisition.tradingview.collector import collect, preflight_snapshot
from baibai_jobs.integration.tradingview_handoff import export_handoff, import_handoff
from baibai_jobs.observability.tradingview import CollectionFailure


def tickers(start, end):
    return [f"TSE:{code}" for code in range(start, end)]


def missing_rating(symbols):
    body = payload(symbols)
    # One missing key rejects the entire receipt, even when other rows are valid.
    del body["data"][symbols[0]]["AnalystRating"]
    return _sdk(body)


def test_bad_first_three_batches_resume_without_starving_later_fresh_symbols(tmp_path):
    first = store(tmp_path / "source.sqlite", 350)
    resumed = store(tmp_path / "resumed.sqlite", 350)
    calls = []

    async def provider(symbols, _columns):
        calls.append(symbols)
        return missing_rating(symbols) if symbols[0] < "TSE:1150" else _sdk(payload(symbols))

    with pytest.raises(CollectionFailure):
        asyncio.run(collect(first, DAY, provider, clock=lambda: NOW, interval=0))
    assert calls == [tickers(1000, 1050), tickers(1050, 1100), tickers(1100, 1150)]
    # A different runner resumes from retained originals, not an in-process cursor.
    packet = tmp_path / "handoff.sqlite"
    remote, master, calendar = native_inputs(first, tmp_path, day=DAY)
    handoff = export_handoff(
        first, packet, day=DAY, since=NOW, master_input=master, calendar_input=calendar
    )
    replayed = import_handoff(packet, resumed, handoff, store=remote)
    assert not replayed.complete
    assert len(replayed.report["rejected_receipts"]) == 3
    assert replayed.report["rows"] == 0
    calls.clear()
    later = NOW + timedelta(hours=2)
    result = asyncio.run(collect(resumed, DAY, provider, clock=lambda: later, interval=0))
    assert calls[:4] == [
        tickers(1000, 1025),
        tickers(1150, 1200),
        tickers(1025, 1050),
        tickers(1200, 1250),
    ]
    assert (result["native_added"], result["remaining_requests"], result["rows"]) == (200, 150, 200)
    assert max(len(chunk) for chunk in calls if chunk[0] < "TSE:1150") == 25
    # None of the still malformed 150, including apparently valid rows, counts.
    # On the next slot only fresh, independent valid responses close that gap.
    calls.clear()

    async def restored(symbols, _columns):
        calls.append(symbols)
        return _sdk(payload(symbols))

    result = asyncio.run(collect(resumed, DAY, restored, clock=lambda: later, interval=0))
    assert result["status"] == "saved"
    assert result["native_added"] == result["rows_added"] == 150
    assert sorted(s for chunk in calls for s in chunk) == tickers(1000, 1150)
    assert all(len(chunk) <= 25 for chunk in calls)


def test_only_a_separately_refetched_healthy_half_can_be_accepted(tmp_path):
    path = store(tmp_path / "source.sqlite", 50)
    symbols = tickers(1000, 1050)
    retain_result(path, missing_rating(symbols), symbols=tuple(symbols))
    assert preflight_snapshot(path, DAY, now=NOW)["remaining_requests"] == 50
    calls = []

    async def same_provider(symbols, _columns):
        calls.append(symbols)
        return missing_rating(symbols) if "TSE:1000" in symbols else _sdk(payload(symbols))

    result = asyncio.run(
        collect(path, DAY, same_provider, clock=lambda: NOW, interval=0, recovery_slot=True)
    )
    assert calls == [symbols[:25], symbols[25:]]
    assert (
        result["status"],
        result["native_received"],
        result["rows"],
        result["remaining_requests"],
    ) == ("partial", 25, 25, 25)
    # The rejected original and first half remain diagnostic evidence in the packet;
    # a later good receipt does not retroactively make either one acceptable.
    packet = tmp_path / "packet.sqlite"
    remote, master, calendar = native_inputs(path, tmp_path, day=DAY)
    handoff = export_handoff(
        path, packet, day=DAY, since=NOW, master_input=master, calendar_input=calendar
    )
    assert len(handoff.receipt_ids) == 3
    canonical = store(tmp_path / "canonical.sqlite", 50)
    replayed = import_handoff(packet, canonical, handoff, store=remote)
    assert not replayed.complete
    assert len(replayed.report["rejected_receipts"]) == 2
    assert replayed.report["rows"] == 25


def test_global_missing_field_is_still_red_and_rotates_across_slots(tmp_path):
    path = store(tmp_path / "source.sqlite", 450)
    seen = []

    async def outage(symbols, _columns):
        seen.append(symbols)
        return missing_rating(symbols)

    passes = []
    for hour in (0, 2, 4):
        seen.clear()
        with pytest.raises(CollectionFailure) as exc:
            asyncio.run(
                collect(
                    path,
                    DAY,
                    outage,
                    clock=lambda hour=hour: NOW + timedelta(hours=hour),
                    interval=0,
                )
            )
        assert exc.value.progress.requests_attempted == len(seen) == 3
        passes.append(list(seen))
    assert passes[1] == [tickers(1000, 1025), tickers(1150, 1200), tickers(1025, 1050)]
    assert passes[2] == [tickers(1050, 1075), tickers(1200, 1250), tickers(1075, 1100)]
    state = preflight_snapshot(path, DAY, now=NOW + timedelta(hours=4))
    assert (state["status"], state["native_received"], state["rows"]) == ("needs_fetch", 0, 0)
    assert state["remaining_requests"] == 450


def test_recovery_slot_caps_calls_and_resumes_without_repeating_symbols(tmp_path):
    path = store(tmp_path / "source.sqlite", 351)
    calls = []

    async def healthy(symbols, _columns):
        calls.append(symbols)
        return _sdk(payload(symbols))

    result = asyncio.run(
        collect(path, DAY, healthy, clock=lambda: NOW, interval=0, recovery_slot=True)
    )
    assert (result["status"], result["stop_reason"], len(calls)) == (
        "partial",
        "recovery_request_cap",
        6,
    )
    assert result["native_added"] == result["rows"] == 300
    assert result["remaining_requests"] == 51
    second = asyncio.run(
        collect(path, DAY, healthy, clock=lambda: NOW, interval=0, recovery_slot=True)
    )
    assert (second["status"], second["native_added"]) == ("saved", 51)
    assert [s for chunk in calls for s in chunk] == tickers(1000, 1351)
    noop = asyncio.run(
        collect(path, DAY, healthy, clock=lambda: NOW, interval=0, recovery_slot=True)
    )
    assert (noop["status"], noop["requests_attempted"], len(calls)) == ("already_saved", 0, 8)


@pytest.mark.parametrize(
    "bad",
    [
        CallToolResult(isError=True, content=[{"type": "text", "text": "HTTP 429"}]),
        CallToolResult(isError=True, content=[{"type": "text", "text": "HTTP 403"}]),
        _sdk({"success": False, "error": "unavailable"}),
        _sdk({"success": True}),
    ],
)
def test_error_envelopes_do_not_trigger_smaller_requests(tmp_path, bad):
    path = store(tmp_path / "source.sqlite", 51)
    first = tickers(1000, 1050)
    retain_result(path, bad, symbols=tuple(first))
    calls = []

    async def healthy(symbols, _columns):
        calls.append(symbols)
        return _sdk(payload(symbols))

    result = asyncio.run(collect(path, DAY, healthy, clock=lambda: NOW, interval=0))
    assert calls == [first, ["TSE:1050"]]
    assert (result["status"], result["native_added"]) == ("saved", 51)
