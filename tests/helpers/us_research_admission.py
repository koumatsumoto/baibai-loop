"""Synthetic verified-calendar and current-master pins for the real US Research CLI."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlencode

from tools.experiments.us_research_report import main as run_cli

from baibai_engine.data_collection.providers.alpaca_calendar import (
    AlpacaCalendarReading,
    USRegularSession,
)
from baibai_engine.investment_judgment.us_proposal import (
    ReportDraft,
    ReviewedReport,
    USProposalDraft,
    prepare_us_proposal,
    save_us_proposal,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master import (
    ListingMasterRow,
    ListingSlice,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    NASDAQ_DIRECTORY_SOURCE,
    RAKUTEN_SCOPE_ZONE,
    CurrentListingMarket,
    CurrentRakutenScope,
    NasdaqDirectoryVersion,
    RakutenBrokerRow,
    RakutenBrokerState,
    active_us_listing_sha256,
    encode_current_listing_master,
)


def write_calendar_capture(
    path: Path,
    *,
    start: date,
    end: date,
    sessions: dict[date, str],
    received_at: datetime,
) -> Path:
    body = json.dumps(
        [
            {"date": day.isoformat(), "open": "09:30", "close": close}
            for day, close in sessions.items()
        ]
    )
    params = urlencode({"start": str(start), "end": str(end), "date_type": "TRADING"})
    url = f"https://paper-api.alpaca.markets/v2/calendar?{params}"
    path.write_text(
        json.dumps(
            {
                "start": str(start),
                "end": str(end),
                "request_url": url,
                "response_url": url,
                "status": 200,
                "received_at": received_at.isoformat(),
                "complete": True,
                "redirected": False,
                "body": body,
                "sha256": sha256(body.encode()).hexdigest(),
            }
        )
    )
    return path


def make_current_us(
    *,
    venue: str,
    ticker: str,
    day: date,
    identity_since: date,
    instrument_id: str = "synthetic-bound-instrument",
    source_identity: str | None = None,
    state: RakutenBrokerState = "catalog_target",
    issue_type: str = "not_flagged_fund",
) -> CurrentListingMarket:
    received = datetime.combine(day, time.min, tzinfo=RAKUTEN_SCOPE_ZONE)
    key = (venue, ticker)
    broker_ticker = ticker.replace(".", " ") if venue == "XNYS" else ticker
    listing = ListingMasterRow(
        key, "Synthetic company", issue_type, "listed", instrument_id, identity_since
    )
    binding = CurrentRakutenScope(
        day,
        received,
        received,
        1,
        int(state != "catalog_target"),
        active_us_listing_sha256({key: instrument_id}),
        (RakutenBrokerRow(key, source_identity or instrument_id, broker_ticker, state),),
    )
    stamp = f"{day:%m%d%Y}00:00"
    return CurrentListingMarket(
        ListingSlice("US", day, (listing,)),
        NASDAQ_DIRECTORY_SOURCE,
        received,
        1,
        (
            NasdaqDirectoryVersion("nasdaqlisted.txt", stamp, "1" * 64),
            NasdaqDirectoryVersion("otherlisted.txt", stamp, "2" * 64),
        ),
        binding,
    )


def current_flags(listing: Path, calendar: Path) -> list[str]:
    return ["--current-listing-master", str(listing), "--calendar-proof", str(calendar)]


def _synthetic_inputs(draft: ReportDraft, checked_at: datetime):
    day = draft.expected_session_et
    basis = draft.listing_basis
    listing = make_current_us(
        venue=draft.venue,
        ticker=draft.ticker,
        day=basis.listing_session_et if basis is not None else day,
        identity_since=day,
        instrument_id=basis.instrument_id if basis is not None else "synthetic-bound-instrument",
    )
    open_at = datetime.combine(day, time(9, 30), tzinfo=RAKUTEN_SCOPE_ZONE)
    sessions = [USRegularSession(day, open_at, draft.session_close_at)]
    listing_day = listing.slice.session
    if listing_day != day:
        sessions.append(
            USRegularSession(
                listing_day,
                datetime.combine(listing_day, time(9, 30), tzinfo=RAKUTEN_SCOPE_ZONE),
                datetime.combine(listing_day, time(16), tzinfo=RAKUTEN_SCOPE_ZONE),
            )
        )
    return listing, AlpacaCalendarReading(
        day,
        checked_at.astimezone(RAKUTEN_SCOPE_ZONE).date(),
        tuple(sessions),
        draft.cutoff_at,
    )


def prepare_with_synthetic_current(decision, analysis, *, sip_proof=None):
    now = decision.prepared_at
    return prepare_us_proposal(
        decision,
        analysis,
        current=_synthetic_inputs(analysis.draft, now),
        checked_at=now,
        sip_proof=sip_proof,
    )


def save_with_synthetic_current(path, decision, analysis, review, *, published_at, sip_proof=None):
    return save_us_proposal(
        path,
        decision,
        analysis,
        review,
        read_current=lambda: _synthetic_inputs(analysis.draft, published_at),
        clock=lambda: published_at,
        sip_proof=sip_proof,
    )


def run_with_synthetic_current(args: list[str]) -> int:
    """Migrate older fixture clocks and inputs without relaxing the real CLI."""
    research = args[0] in {"draft-digest", "save"}
    if not research and args[0] not in {"proposal-digest", "proposal-save"}:
        return run_cli(args)
    path = Path(args[1])
    try:
        if research:
            draft = ReportDraft.model_validate_json(path.read_bytes())
            now = draft.prepared_at
        else:
            draft = ReviewedReport.model_validate_json(path.read_bytes()).draft
            decision = USProposalDraft.model_validate_json(Path(args[2]).read_bytes())
            # Fixed relative to the fixture's authorship, not any supplied review time.
            now = decision.prepared_at + timedelta(hours=2)
    except (ValueError, OSError):
        return run_cli(args)
    current, reading = _synthetic_inputs(draft, now)
    listing = path.with_suffix(".listing-fixture.json")
    listing.write_bytes(encode_current_listing_master({"US": current}))
    calendar = write_calendar_capture(
        path.with_suffix(".calendar-fixture.json"),
        start=reading.start,
        end=reading.end,
        sessions={
            row.trading_date: row.close_at.astimezone(RAKUTEN_SCOPE_ZONE).strftime("%H:%M")
            for row in reading.sessions
        },
        received_at=draft.cutoff_at,
    )
    return run_cli([*args, *current_flags(listing, calendar)], now=now)
