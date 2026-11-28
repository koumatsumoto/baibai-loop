"""Prepare an explicit private Alpaca research master pin, not a production refresh."""

from collections.abc import Callable
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path

from httpx2 import BaseTransport, HTTPTransport

from baibai_engine.data_collection.providers.alpaca_http import (
    AlpacaCredentials,
    receive_paper_us_assets,
)
from baibai_engine.machine_analysis.observations.stocks.alpaca_listing_input import (
    prepare_alpaca_research_inventory,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    LISTING_MASTER_MAX_BYTES,
    encode_current_listing_master,
    read_local_listing_master,
)
from baibai_engine.storage.filesystem import write_bytes_atomic
from baibai_engine.storage.json import write_json_atomic
from baibai_jobs.integration.us_daily_handoff import alpaca_paper_credentials_from_env


def _now() -> datetime:
    return datetime.now(UTC)


def acquire_us_assets(
    *,
    previous: Path | None,
    directory: Path,
    expected_ny_date: date | None,
    transport: BaseTransport,
    credentials: AlpacaCredentials,
    clock: Callable[[], datetime],
) -> dict[str, object]:
    if directory.exists(follow_symlinks=False):
        raise FileExistsError("Alpaca Assets handoff requires a new private directory")
    entries = read_local_listing_master(previous) if previous is not None else {}
    receipt = receive_paper_us_assets(credentials=credentials, transport=transport, clock=clock)
    inventory = prepare_alpaca_research_inventory(receipt, previous=entries.get("US"))
    current = inventory.current
    if expected_ny_date is not None and current.slice.session != expected_ny_date:
        raise ValueError("Alpaca Assets receipt is not from the requested NY date")
    entries["US"] = current
    current_bytes = encode_current_listing_master(entries)
    if len(current_bytes) > LISTING_MASTER_MAX_BYTES:
        raise ValueError("Alpaca projected listing master exceeds its byte limit")
    report: dict[str, object] = {
        "source": "alpaca-us-assets",
        "status": "complete",
        "received_at": receipt.received_at.isoformat(),
        "listing_session": current.slice.session.isoformat(),
        "assets": len(receipt.assets),
        "targets": len(current.slice.rows),
        "observed_by_exchange": inventory.observed_by_exchange,
        "excluded": inventory.excluded,
        "assets_sha256": receipt.body_sha256,
        "listing_master_sha256": sha256(current_bytes).hexdigest(),
    }
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    write_bytes_atomic(directory / "assets.json", receipt.body)
    write_bytes_atomic(directory / "listing-master.json", current_bytes)
    write_json_atomic(directory / "result.json", report)
    return report


def acquire_us_assets_from_env(
    *, previous: Path | None, directory: Path, expected_ny_date: date | None
) -> dict[str, object]:
    credentials = alpaca_paper_credentials_from_env()
    with HTTPTransport(trust_env=False, retries=0) as transport:
        return acquire_us_assets(
            previous=previous,
            directory=directory,
            expected_ny_date=expected_ny_date,
            transport=transport,
            credentials=credentials,
            clock=_now,
        )
