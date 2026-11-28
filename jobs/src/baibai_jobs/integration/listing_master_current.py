"""Read and publish the current Market listing master independently of Machine."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, Protocol

from baibai_engine.machine_analysis.observations.stocks.listing_master import (
    ListingKey,
    ListingMarket,
    ObservedListing,
    VerifiedListingEvent,
    refresh_listing_slice,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    LISTING_MASTER_MAX_BYTES,
    RAKUTEN_SCOPE_ZONE,
    CurrentListingMarket,
    CurrentRakutenScope,
    NasdaqDirectoryVersion,
    RakutenBrokerRow,
    RakutenBrokerState,
    checked_nasdaq_directory_versions,
    decode_current_listing_master,
    encode_current_listing_master,
)
from baibai_engine.machine_analysis.observations.stocks.us_universe import (
    RakutenOrdinaryRow,
    RakutenOrdinarySnapshot,
    RakutenRestrictionRow,
    rakuten_listing_key,
)
from baibai_engine.primitives.json import encode_json
from baibai_engine.storage.filesystem import fsync_directory, write_bytes_atomic
from baibai_engine.storage.objects.json import put_private
from baibai_engine.storage.objects.r2 import (
    Boto3R2Store,
    CheckedObjectMetadataReader,
    LakeCASConflict,
    ObjectStore,
    R2OperationStopped,
    RemoteObject,
)

LISTING_MASTER_CURRENT_KEY = "market/listing-master/current.json"

type ListingUpdateStatus = Literal["updated", "unchanged", "stale"]


@dataclass(frozen=True, slots=True)
class RakutenListingBinding:
    """A verified broker join to the exact current exchange instrument."""

    listing_key: ListingKey
    instrument_id: str
    observation: RakutenOrdinaryRow | RakutenRestrictionRow


_RAKUTEN_STATES: dict[str, RakutenBrokerState] = {
    "buyable": "catalog_target",
    "sell_only": "sell_only",
    "buy_suspended": "buy_suspended",
}


class ListingMasterStore(ObjectStore, CheckedObjectMetadataReader, Protocol):
    """Use acknowledged conditional PUTs (Boto3R2Store with verify_put_with_head=False)."""


def _is_directory_rollback(
    previous: tuple[NasdaqDirectoryVersion, ...] | None,
    received: tuple[NasdaqDirectoryVersion, ...] | None,
) -> bool:
    if previous is None or received is None:
        return False
    rollback = False
    for old, new in zip(previous, received, strict=True):
        old_minute, new_minute = old.native_minute, new.native_minute
        if new_minute == old_minute and new.sha256 != old.sha256:
            raise ValueError(f"{new.file}: different bytes claim the same native creation minute")
        rollback |= new_minute < old_minute
    return rollback


def _get_current(
    store: CheckedObjectMetadataReader, *, single_attempt: bool = False
) -> tuple[bytes, RemoteObject] | None:
    if single_attempt and isinstance(store, Boto3R2Store):
        return store.get_checked_object(
            LISTING_MASTER_CURRENT_KEY,
            max_bytes=LISTING_MASTER_MAX_BYTES,
            single_attempt=True,
        )
    return store.get_checked_object(LISTING_MASTER_CURRENT_KEY, max_bytes=LISTING_MASTER_MAX_BYTES)


def _read(
    store: CheckedObjectMetadataReader,
) -> tuple[dict[ListingMarket, CurrentListingMarket], str | None]:
    saved = _get_current(store)
    if saved is None:
        return {}, None
    body, metadata = saved
    return decode_current_listing_master(body), metadata.etag


def read_current_listing_master(
    store: CheckedObjectMetadataReader,
) -> dict[ListingMarket, CurrentListingMarket]:
    """Read the one current file; a market with no successful update is absent."""
    return _read(store)[0]


def pin_current_listing_master(store: CheckedObjectMetadataReader, *, output: Path) -> None:
    """Create a run-private copy once so every consumer sees the same checked bytes."""
    if output.exists(follow_symlinks=False):
        raise FileExistsError("the run's listing master is already pinned")
    saved = _get_current(store, single_attempt=True)
    if saved is None:
        raise FileNotFoundError("the current listing master is not published")
    payload, _ = saved
    decode_current_listing_master(payload)
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output.parent, prefix=".listing-master-") as directory:
        staged = Path(directory) / "current.json"
        write_bytes_atomic(staged, payload)
        staged.chmod(0o600)
        output.hardlink_to(staged)
    fsync_directory(output.parent)


def refresh_current_listing_master(
    store: ListingMasterStore,
    *,
    market: ListingMarket,
    session: date,
    source: str,
    received_at: datetime,
    observations: Iterable[ObservedListing],
    complete: bool,
    verified_events: Iterable[VerifiedListingEvent] = (),
    nasdaq_directories: tuple[NasdaqDirectoryVersion, ...] | None = None,
) -> ListingUpdateStatus:
    """Replace just one market using a verified complete source observation.

    Reconcile again after one actual CAS conflict: the new prior may include
    identity changes for this market as well as an update to the other market.
    """
    if market not in ("JP", "US") or type(session) is not date or received_at.utcoffset() is None:
        raise ValueError("current listing update requires a market, local session and receipt")
    if complete is not True:
        raise ValueError("incomplete listing capture cannot replace current")
    received_at = received_at.astimezone(UTC)
    versions = checked_nasdaq_directory_versions(
        nasdaq_directories, market=market, source=source, session=session
    )
    rows, events = tuple(observations), tuple(verified_events)
    clock = (session, received_at)

    def prepare(prior: CurrentListingMarket | None) -> CurrentListingMarket | None:
        prior_clock = (prior.slice.session, prior.received_at) if prior is not None else None
        if prior_clock is not None and clock < prior_clock:
            return None
        if (
            prior is not None
            and session == prior.slice.session
            and _is_directory_rollback(prior.nasdaq_directories, versions)
        ):
            return None

        refresh = refresh_listing_slice(
            prior.slice if prior is not None else None,
            market=market,
            session=session,
            observations=rows,
            complete=complete,
            verified_events=events,
        )
        if prior is None:
            return CurrentListingMarket(refresh.slice, source, received_at, len(rows), versions)
        if (
            versions is not None
            and versions == prior.nasdaq_directories
            and refresh.slice == prior.slice
        ):
            return prior  # A transport retry of the same files/evidence is not a new capture.
        replacement = replace(
            prior,
            slice=refresh.slice,
            source=source,
            received_at=received_at,
            received_count=len(rows),
            nasdaq_directories=versions,
        )
        if replacement != prior and clock == prior_clock:
            raise ValueError("different current listings claim the same market receipt")
        return replacement

    return _update_market(store, market, prepare)


def _update_market(
    store: ListingMasterStore,
    market: ListingMarket,
    prepare: Callable[[CurrentListingMarket | None], CurrentListingMarket | None],
) -> ListingUpdateStatus:
    """Prepare one partition against the latest read on each conditional attempt."""
    for last_attempt in (False, True):
        current, etag = _read(store)
        prior = current.get(market)
        replacement = prepare(prior)
        if replacement is None:
            return "stale"
        if replacement == prior:
            return "unchanged"
        current[market] = replacement
        payload = encode_current_listing_master(current)
        try:
            put_private(
                store,
                LISTING_MASTER_CURRENT_KEY,
                payload,
                previous_etag=etag,
                max_bytes=LISTING_MASTER_MAX_BYTES,
            )
        except LakeCASConflict:
            if last_attempt:
                raise
        else:
            return "updated"
    raise AssertionError("conditional update exited without an outcome")


def _active_ids(current: CurrentListingMarket) -> dict[ListingKey, str]:
    return {
        row.key: row.instrument_id
        for row in current.slice.rows
        if row.key[0] in ("XNAS", "XNYS")
        and row.listing_state == "listed"
        and row.instrument_id is not None
    }


def _active_listing_sha256(active: dict[ListingKey, str]) -> str:
    return sha256(encode_json(sorted(active.items())).encode()).hexdigest()


def _current_us_slice(
    current: CurrentListingMarket | None, *, expected_session: date, cutoff: datetime
) -> bool:
    return (
        current is not None
        and current.slice.market == "US"
        and current.slice.session == expected_session
        and cutoff.astimezone(RAKUTEN_SCOPE_ZONE).date() == expected_session
        and current.received_at <= cutoff
    )


def _bound_rakuten_rows(
    snapshot: RakutenOrdinarySnapshot,
    *,
    bindings: Iterable[RakutenListingBinding],
) -> tuple[RakutenBrokerRow, ...]:
    available: set[RakutenOrdinaryRow | RakutenRestrictionRow] = {
        *snapshot.rows,
        *snapshot.restriction_only,
    }
    rows: list[RakutenBrokerRow] = []
    for binding in bindings:
        native = binding.observation
        if native not in available or not binding.instrument_id:
            raise ValueError("binding does not identify a unique normalized Rakuten observation")
        if rakuten_listing_key(native.ticker, native.exchange) != binding.listing_key:
            raise ValueError("binding does not match the exact Rakuten listing identity")
        available.remove(native)
        state = native.buy_status if isinstance(native, RakutenOrdinaryRow) else native.restriction
        if state not in _RAKUTEN_STATES:
            raise ValueError("unknown buyability cannot create a broker state")
        rows.append(
            RakutenBrokerRow(
                binding.listing_key,
                binding.instrument_id,
                native.ticker,
                _RAKUTEN_STATES[state],
            )
        )
    return tuple(sorted(rows, key=lambda row: row.listing_key))


def read_current_rakuten_scope(
    current: CurrentListingMarket | None, *, expected_session: date, cutoff: datetime
) -> CurrentRakutenScope | None:
    """Return a complete broker intersection for this active US listing set or None."""
    if cutoff.utcoffset() is None:
        raise ValueError("Rakuten cutoff requires a timezone")
    if current is None or not _current_us_slice(
        current, expected_session=expected_session, cutoff=cutoff
    ):
        return None
    scope = current.rakuten
    if scope is None or scope.effective_session != expected_session or max(scope.clocks) > cutoff:
        return None
    active = _active_ids(current)
    if scope.active_listing_sha256 != _active_listing_sha256(active) or any(
        active.get(row.listing_key) != row.instrument_id for row in scope.rows
    ):
        return None  # A cached set cannot tell us whether newly active keys were matched.
    return scope


def refresh_current_us_rakuten_scope(
    store: ListingMasterStore,
    *,
    snapshot: RakutenOrdinarySnapshot | None,
    bindings: Iterable[RakutenListingBinding],
    expected_session: date,
    cutoff: datetime,
) -> ListingUpdateStatus:
    """Replace only broker facts from a complete current pair; never create exchange listings."""
    if cutoff.utcoffset() is None:
        raise ValueError("Rakuten cutoff requires a timezone")
    if (
        snapshot is None
        or snapshot.catalog_mode != "cash_ordinary_tradable"
        or snapshot.exclusions_received_at is None
        or snapshot.exclusions_reported_count is None
    ):
        raise ValueError("a complete Rakuten catalog and restrictions capture are required")
    if snapshot.effective_session != expected_session:
        raise ValueError("Rakuten capture has a different NY session")
    catalog_at, exclusions_at = snapshot.catalog_received_at, snapshot.exclusions_received_at
    catalog_count, exclusions_count = snapshot.reported_count, snapshot.exclusions_reported_count
    if any(clock.utcoffset() is None or clock > cutoff for clock in (catalog_at, exclusions_at)):
        raise ValueError("Rakuten receipt is unzoned or after the cutoff")
    bound = _bound_rakuten_rows(snapshot, bindings=bindings)
    native_rows: tuple[RakutenOrdinaryRow | RakutenRestrictionRow, ...] = (
        *snapshot.rows,
        *snapshot.restriction_only,
    )
    captured_keys = {
        key
        for row in native_rows
        if (key := rakuten_listing_key(row.ticker, row.exchange)) is not None
    }
    bound_keys = {row.listing_key for row in bound}
    negatives = {
        (row.listing_key, row.instrument_id, row.state)
        for row in bound
        if row.state != "catalog_target"
    }

    def prepare(current: CurrentListingMarket | None) -> CurrentListingMarket | None:
        if current is None or not _current_us_slice(
            current, expected_session=expected_session, cutoff=cutoff
        ):
            return None
        active = _active_ids(current)
        if any(active.get(row.listing_key) != row.instrument_id for row in bound):
            return None
        if active.keys() & captured_keys != bound_keys:
            raise ValueError("Rakuten bindings must cover all active matched observations")
        scope = CurrentRakutenScope(
            expected_session,
            catalog_at,
            exclusions_at,
            catalog_count,
            exclusions_count,
            _active_listing_sha256(active),
            bound,
        )
        previous = current.rakuten
        if previous is not None and previous.effective_session == expected_session:
            if any(new < old for new, old in zip(scope.clocks, previous.clocks, strict=True)):
                return None
            if scope.clocks == previous.clocks and scope != previous:
                raise ValueError("different broker states claim the same Rakuten receipt pair")
            if scope.exclusions_received_at == previous.exclusions_received_at and any(
                (row.listing_key, row.instrument_id, row.state) not in negatives
                for row in previous.rows
                if row.state != "catalog_target"
            ):
                return None  # A newer catalog alone cannot clear a known restriction.
        return replace(current, rakuten=scope)

    return _update_market(store, "US", prepare)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="現行銘柄マスターを今回の処理用に固定する。")
    parser.add_argument("--output", required=True, type=Path, help="この実行専用の未作成ファイル")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        bucket = os.environ.get("R2_STORES_BUCKET") or "baibai-stores"
        with closing(Boto3R2Store(bucket=bucket)) as store:
            pin_current_listing_master(store, output=args.output)
    except (Exception, R2OperationStopped) as error:
        print(f"listing master pin failed: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
