"""Acquire one date of approved SIP originals into a run-private workflow handoff."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from httpx2 import BaseTransport, HTTPTransport

from baibai_engine.data_collection.providers.alpaca import BarsCapture, BarsResponse
from baibai_engine.data_collection.providers.alpaca_http import (
    AlpacaCredentials,
    AlpacaHTTPReceipt,
    receive_alpaca,
    receive_paper_calendar,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    LISTING_MASTER_MAX_BYTES,
    decode_current_listing_master,
)
from baibai_engine.storage.filesystem import write_bytes_atomic
from baibai_engine.storage.json import canonical_json, write_json_atomic
from baibai_jobs.integration.us_daily_prices import (
    USDailyPriceCollection,
    collect_current_us_daily_prices,
)

_SOURCE = "alpaca-us-daily"
_NY = ZoneInfo("America/New_York")


def _now() -> datetime:
    return datetime.now(UTC)


def _page_spec(wire: AlpacaHTTPReceipt, *, digest: str) -> dict[str, object]:
    return {
        "request_url": wire.url,
        "response_url": wire.url,
        "status": wire.response.status_code,
        "complete": True,
        "redirected": False,
        "received_at": wire.response.received_at.isoformat(),
        "sha256": digest,
    }


def write_calendar_proof(path: Path, *, wire: AlpacaHTTPReceipt, start: date, end: date) -> str:
    body = wire.response.body
    write_json_atomic(
        path,
        {
            **_page_spec(wire, digest=sha256(body).hexdigest()),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "body": body.decode("utf-8"),
        },
    )
    return sha256(path.read_bytes()).hexdigest()


def write_bars_proof(
    directory: Path,
    capture: BarsCapture,
    pages: dict[str, AlpacaHTTPReceipt],
    *,
    bind_native_receipts: bool,
) -> str:
    manifest: list[dict[str, object]] = []
    for page in capture.pages:
        wire = pages[page.request_url]
        name = f"page-{page.page_number:02d}.json"
        write_bytes_atomic(directory / name, page.response.body)
        member = {
            **_page_spec(wire, digest=page.body_sha256),
            "file": name,
            "headers": dict(wire.headers),
        }
        if bind_native_receipts:
            member["receipt_id"] = page.receipt_id
        manifest.append(member)
    data = canonical_json(
        {
            **({} if bind_native_receipts else {"source_handoff": _SOURCE}),
            "request_id": capture.request_id,
            "start": capture.query.start.isoformat(),
            "end": capture.query.end.isoformat(),
            "symbols": list(capture.query.symbols),
            "pages": manifest,
        }
    )
    write_bytes_atomic(directory / "capture.json", data)
    return sha256(data).hexdigest()


def _save(
    directory: Path,
    collection: USDailyPriceCollection,
    *,
    listing_sha: str,
    calendar: AlpacaHTTPReceipt,
    calendar_start: date,
    calendar_end: date,
    pages: dict[str, AlpacaHTTPReceipt],
) -> dict[str, object]:
    plan = collection.plan
    observations = len(collection.observations)
    report: dict[str, object] = {
        "source": _SOURCE,
        "asof": plan.price_session.isoformat(),
        "listing_session": plan.listing_session.isoformat(),
        "listing_master_sha256": listing_sha,
        "calendar": "calendar.json",
        "status": "complete" if collection.fully_observed else "partial",
        "expected": len(plan.targets),
        "received": observations,
        "missing": sum(len(r.coverage.missing) for r in collection.receipts),
        "unverified": sum(len(r.coverage.unverified) for r in collection.receipts),
        "http_errors": sorted(
            {
                receipt.capture.pages[-1].response.status_code
                for receipt in collection.receipts
                if receipt.capture is not None and receipt.capture.failure == "http_status"
            }
        ),
    }
    # No local packet is necessary unless a complete chain contributes observations.
    if not observations:
        return {**report, "status": "unavailable", "calendar": None}
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    write_calendar_proof(
        directory / "calendar.json", wire=calendar, start=calendar_start, end=calendar_end
    )
    batches: list[dict[str, object]] = []
    for index, item in enumerate(collection.receipts, 1):
        coverage, capture = item.coverage, item.capture
        proof: str | None = None
        proof_sha: str | None = None
        if capture is not None and coverage.failure is None and coverage.observations:
            batch_directory = directory / f"batch-{index:03d}"
            proof = f"{batch_directory.name}/capture.json"
            proof_sha = write_bars_proof(
                batch_directory, capture, pages, bind_native_receipts=False
            )
        batches.append(
            {
                "proof": proof,
                "proof_sha256": proof_sha,
                "expected": len(coverage.batch.expected),
                "received": len(coverage.received),
                "missing": len(coverage.missing),
                "unverified": len(coverage.unverified),
                "failure": coverage.failure,
            }
        )
    # Commit marker, after all private proof files. The existing authored proof
    # reader uses these exact manifests without installing a second price store.
    write_json_atomic(directory / "result.json", {**report, "batches": batches})
    return report


def acquire_us_daily(
    *,
    listing_master: Path,
    directory: Path,
    expected_session: date | None,
    transport: BaseTransport,
    credentials: AlpacaCredentials,
    clock: Callable[[], datetime],
) -> dict[str, object]:
    if directory.exists(follow_symlinks=False):
        raise FileExistsError("US price handoff must use a new run-private directory")
    with listing_master.open("rb") as stream:
        listing_bytes = stream.read(LISTING_MASTER_MAX_BYTES + 1)
    current = decode_current_listing_master(listing_bytes).get("US")
    if current is None or current.rakuten is None:
        return {"source": _SOURCE, "status": "scope_unavailable"}
    first_clock = clock()
    if first_clock.utcoffset() is None:
        raise ValueError("US handoff clock requires a timezone")
    end = first_clock.astimezone(_NY).date()
    if current.slice.session != end:
        return {"source": _SOURCE, "status": "scope_unavailable"}
    start = end - timedelta(days=7)
    calendar, calendar_wire = receive_paper_calendar(
        start, end, credentials=credentials, transport=transport, clock=clock
    )
    if calendar.problem is not None:
        report: dict[str, object] = {
            "source": _SOURCE,
            "status": "calendar_unavailable",
            "reason": calendar.problem,
        }
        if calendar_wire is not None:
            return {**report, "http_status": calendar_wire.response.status_code}
        return report
    pages: dict[str, AlpacaHTTPReceipt] = {}

    def read_bar_page(url: str) -> BarsResponse:
        page = receive_alpaca(
            url, endpoint="bars", credentials=credentials, transport=transport, clock=clock
        )
        pages[url] = page
        return page.response

    collection = collect_current_us_daily_prices(
        current,
        calendar,
        attempt_id=uuid4(),
        clock=clock,
        transport=read_bar_page,
        expected_price_session=expected_session,
    )
    if collection is None or calendar_wire is None:
        return {"source": _SOURCE, "status": "session_or_scope_unavailable"}
    return _save(
        directory,
        collection,
        listing_sha=sha256(listing_bytes).hexdigest(),
        calendar=calendar_wire,
        calendar_start=start,
        calendar_end=end,
        pages=pages,
    )


def alpaca_paper_credentials_from_env() -> AlpacaCredentials:
    return AlpacaCredentials(
        os.environ.get("APCA_API_KEY_ID", ""), os.environ.get("APCA_API_SECRET_KEY", "")
    )


def acquire_us_daily_from_env(
    *, listing_master: Path, directory: Path, expected_session: date | None
) -> dict[str, object]:
    credentials = alpaca_paper_credentials_from_env()
    with HTTPTransport(trust_env=False, retries=0) as transport:
        return acquire_us_daily(
            listing_master=listing_master,
            directory=directory,
            expected_session=expected_session,
            transport=transport,
            credentials=credentials,
            clock=_now,
        )
