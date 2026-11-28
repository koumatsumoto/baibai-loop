"""Acquire one bounded split-SIP technical packet into a run-private directory."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from itertools import batched
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from httpx2 import BaseTransport, HTTPTransport

from baibai_engine.data_collection.providers.alpaca import (
    MAX_SYMBOLS,
    BarsResponse,
    DailyBarsQuery,
    capture_daily_bars,
)
from baibai_engine.data_collection.providers.alpaca_calendar import (
    CALENDAR_WINDOW_MAX_DAYS,
    AlpacaCalendarReading,
)
from baibai_engine.data_collection.providers.alpaca_http import (
    AlpacaCredentials,
    AlpacaHTTPReceipt,
    receive_alpaca,
    receive_paper_calendar,
)
from baibai_engine.machine_analysis.observations.prices.us_sip_coverage import (
    AlpacaSIPDailyBatch,
    inspect_alpaca_sip_daily_batch,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    LISTING_MASTER_MAX_BYTES,
    decode_current_listing_master,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.json import write_json_atomic
from baibai_jobs.integration.us_daily_handoff import (
    alpaca_paper_credentials_from_env,
    write_bars_proof,
    write_calendar_proof,
)
from baibai_jobs.integration.us_daily_plan import (
    TECHNICAL_CALENDAR_CHUNKS,
    TECHNICAL_WINDOW_SESSIONS,
    USTechnicalScope,
    plan_us_technical_scope,
)

SOURCE = "alpaca-us-technical"
# The generic 10,000-row request can exceed the whole-response byte ceiling.
# 500 * 16 pages also covers a normally filled 20-symbol * 253-session chain.
TECHNICAL_PAGE_LIMIT = 500
_NY = ZoneInfo("America/New_York")


def _now() -> datetime:
    return datetime.now(UTC)


def technical_request_id(attempt_id: UUID, *, listing_sha: str, as_of: date, ordinal: int) -> str:
    """Bind captures to their frozen master and intended NY session before acquisition."""
    scope = canonical_json([SOURCE, str(attempt_id), listing_sha, as_of]).encode()
    return f"us-technical-{sha256(scope).hexdigest()}-{ordinal}"


def _read_calendar_windows(
    *,
    now: datetime,
    credentials: AlpacaCredentials,
    transport: BaseTransport,
    clock: Callable[[], datetime],
) -> tuple[tuple[AlpacaCalendarReading, AlpacaHTTPReceipt], ...]:
    result = []
    completed = 0
    end = now.astimezone(_NY).date()
    for _ in range(TECHNICAL_CALENDAR_CHUNKS):
        start = end - timedelta(days=CALENDAR_WINDOW_MAX_DAYS - 1)
        reading, wire = receive_paper_calendar(
            start, end, credentials=credentials, transport=transport, clock=clock
        )
        if reading.problem is not None or wire is None:
            return ()
        result.append((reading, wire))
        completed += sum(row.close_at <= now for row in reading.sessions)
        if completed >= TECHNICAL_WINDOW_SESSIONS:
            return tuple(result)
        end = start - timedelta(days=1)
    return ()


def _batches(scope: USTechnicalScope) -> Iterator[AlpacaSIPDailyBatch]:
    # A grouped query has one start bound. Never ask for an older use of a
    # recently assigned/reused/moved literal key to fill somebody else's history.
    symbols: dict[int, list[str]] = defaultdict(list)
    for symbol, start in scope.first.items():
        symbols[start].append(symbol)
    for start, group in sorted(symbols.items()):
        days = scope.calendar.trading_dates[start:]
        for requested in batched(sorted(group), MAX_SYMBOLS, strict=False):
            yield AlpacaSIPDailyBatch(
                DailyBarsQuery(
                    requested,
                    days[0],
                    days[-1],
                    "-",
                    "split",
                    feed="sip",
                    page_limit=TECHNICAL_PAGE_LIMIT,
                ),
                days,
            )


def _save_batch(
    directory: Path,
    batch: AlpacaSIPDailyBatch,
    *,
    request_id: str,
    no_earlier_than: datetime,
    credentials: AlpacaCredentials,
    transport: BaseTransport,
    clock: Callable[[], datetime],
) -> str | None:
    pages: dict[str, AlpacaHTTPReceipt] = {}

    def read_page(url: str) -> BarsResponse:
        wire = receive_alpaca(
            url, endpoint="bars", credentials=credentials, transport=transport, clock=clock
        )
        pages[url] = wire
        return wire.response

    capture = capture_daily_bars(batch.query, request_id=request_id, transport=read_page)
    checked_at = clock()
    coverage = inspect_alpaca_sip_daily_batch(batch, capture)
    # Terminal historical missing rows belong to the model's missingness.
    current = {
        row.returned_symbol for row in coverage.observations if row.trade_date == batch.query.end
    }
    if (
        coverage.failure is not None
        or current != set(batch.query.symbols)
        or any(
            not no_earlier_than <= page.response.received_at <= checked_at for page in capture.pages
        )
    ):
        return None
    return write_bars_proof(directory, capture, pages, bind_native_receipts=True)


def acquire_us_technical(
    *,
    listing_master: Path,
    directory: Path,
    expected_session: date | None,
    credentials: AlpacaCredentials,
    transport: BaseTransport,
    clock: Callable[[], datetime],
) -> dict[str, object]:
    if directory.exists(follow_symlinks=False):
        raise FileExistsError("US technical handoff requires a new private directory")
    with listing_master.open("rb") as stream:
        listing_bytes = stream.read(LISTING_MASTER_MAX_BYTES + 1)
    current = decode_current_listing_master(listing_bytes).get("US")
    if current is None or current.rakuten is None:
        return {"source": SOURCE, "status": "scope_unavailable"}
    now = clock()
    if now.utcoffset() is None:
        raise ValueError("US technical acquisition requires a timezone")
    received = _read_calendar_windows(
        now=now, credentials=credentials, transport=transport, clock=clock
    )
    if not received:
        return {"source": SOURCE, "status": "calendar_unavailable"}
    calendars = tuple(reading for reading, _ in received)
    scope = plan_us_technical_scope(current, calendars, cutoff=clock())
    if scope is None:
        return {"source": SOURCE, "status": "scope_unavailable"}
    as_of = scope.calendar.as_of
    if expected_session is not None and expected_session != as_of:
        return {"source": SOURCE, "status": "session_unavailable"}

    attempt = uuid4()
    listing_sha = sha256(listing_bytes).hexdigest()
    directory.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".us-tech-", dir=directory.parent) as work:
        packet = Path(work) / "packet"
        packet.mkdir(mode=0o700)
        calendar_keys = [
            write_calendar_proof(
                packet / f"calendar-{index:03d}.json",
                wire=wire,
                start=reading.start,
                end=reading.end,
            )
            for index, (reading, wire) in enumerate(received, 1)
        ]
        batch_keys = []
        for index, batch in enumerate(_batches(scope), 1):
            result = _save_batch(
                packet / f"batch-{index:03d}",
                batch,
                request_id=technical_request_id(
                    attempt, listing_sha=listing_sha, as_of=as_of, ordinal=index
                ),
                no_earlier_than=scope.not_before,
                credentials=credentials,
                transport=transport,
                clock=clock,
            )
            if result is None:
                return {"source": SOURCE, "status": "incomplete", "asof": as_of.isoformat()}
            batch_keys.append(result)
        cutoff = clock()
        if plan_us_technical_scope(current, calendars, cutoff=cutoff) != scope:
            return {"source": SOURCE, "status": "session_changed"}
        write_json_atomic(
            packet / "result.json",
            {
                "source": SOURCE,
                "attempt_id": str(attempt),
                "listing_master_sha256": listing_sha,
                "asof": as_of.isoformat(),
                "cutoff_at": cutoff.isoformat(),
                "calendars": calendar_keys,
                "batches": batch_keys,
            },
        )
        if directory.exists(follow_symlinks=False):
            raise FileExistsError("US technical handoff directory changed")
        packet.rename(directory)
    return {
        "source": SOURCE,
        "status": "complete",
        "asof": as_of.isoformat(),
        "targets": len(scope.targets),
        "batches": len(batch_keys),
    }


def acquire_us_technical_from_env(
    *, listing_master: Path, directory: Path, expected_session: date | None
) -> dict[str, object]:
    credentials = alpaca_paper_credentials_from_env()
    with HTTPTransport(trust_env=False, retries=0) as transport:
        return acquire_us_technical(
            listing_master=listing_master,
            directory=directory,
            expected_session=expected_session,
            credentials=credentials,
            transport=transport,
            clock=_now,
        )
