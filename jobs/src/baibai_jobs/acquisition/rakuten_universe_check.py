"""Check an operator's normalized Rakuten cash ordinary-share capture locally."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TypedDict

from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    active_us_listing_ids,
    encode_current_listing_master,
    read_local_listing_master,
)
from baibai_engine.machine_analysis.observations.stocks.us_universe import (
    RAKUTEN_LISTING_EXCHANGES,
    RakutenOrdinaryRow,
    RakutenOrdinarySnapshot,
    RakutenRestrictionRow,
    cover_current_rakuten_catalog_targets,
    parse_rakuten_ordinary_snapshot,
    rakuten_exchange_name,
    rakuten_listing_key,
    reconcile_rakuten_catalog_venues,
)
from baibai_jobs.integration.listing_master_current import (
    RakutenListingBinding,
    prepare_rakuten_scope_update,
    write_private_listing_master_pin,
)
from baibai_jobs.integration.us_listing_refresh import (
    USListingDirectoryReceipt,
    USListingInputs,
    prepare_us_listing_update,
)


class VenueCounts(TypedDict):
    requested: bool
    listing_supported: bool
    catalog_count: int | None
    catalog_targets: int | None
    sell_only: int
    buy_suspended: int
    restriction_only: int


def _venue_report(
    snapshot: RakutenOrdinarySnapshot, totals: dict[str, int]
) -> dict[str, VenueCounts]:
    catalog_status = Counter((row.exchange, row.buy_status) for row in snapshot.rows)
    restriction_only = Counter((row.exchange, row.restriction) for row in snapshot.restriction_only)
    venues = RAKUTEN_LISTING_EXCHANGES | totals.keys() | {venue for venue, _ in restriction_only}
    return {
        venue: VenueCounts(
            requested=venue in RAKUTEN_LISTING_EXCHANGES,
            listing_supported=venue in RAKUTEN_LISTING_EXCHANGES,
            catalog_count=totals.get(venue),
            catalog_targets=catalog_status[venue, "buyable"] if venue in totals else None,
            sell_only=catalog_status[venue, "sell_only"] + restriction_only[venue, "sell_only"],
            buy_suspended=(
                catalog_status[venue, "buy_suspended"] + restriction_only[venue, "buy_suspended"]
            ),
            restriction_only=(
                restriction_only[venue, "sell_only"] + restriction_only[venue, "buy_suspended"]
            ),
        )
        for venue in sorted(venues)
    }


def _count(value: str) -> int:
    if not value.isascii() or not value.isdecimal():
        raise argparse.ArgumentTypeError("use a nonnegative integer count")
    return int(value)


def _venue_count(value: str) -> tuple[str, int]:
    venue, equals, count = value.rpartition("=")
    if not equals or not venue or venue != venue.strip() or not venue.isprintable():
        raise argparse.ArgumentTypeError("use EXCHANGE=COUNT")
    return rakuten_exchange_name(venue), _count(count)


def _session(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use NY session date YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("use NY session date YYYY-MM-DD")
    return parsed


def _now() -> datetime:
    return datetime.now(UTC)


def _assemble_master(
    args: argparse.Namespace, snapshot: RakutenOrdinarySnapshot, totals: dict[str, int]
) -> int:
    target = snapshot.effective_session
    directory = args.listing_directory
    inputs = USListingInputs(
        (
            USListingDirectoryReceipt(
                "nasdaqlisted.txt",
                target,
                args.nasdaqlisted_received_at,
                True,
                (directory / "nasdaqlisted.txt").read_bytes(),
            ),
            USListingDirectoryReceipt(
                "otherlisted.txt",
                target,
                args.otherlisted_received_at,
                True,
                (directory / "otherlisted.txt").read_bytes(),
            ),
        )
    )
    try:
        previous = read_local_listing_master(args.previous_master) if args.previous_master else {}
    except ValueError as error:
        raise ValueError("invalid previous listing master") from error
    received_at, listing_update = prepare_us_listing_update(inputs, target=target)
    cutoff = _now()
    if received_at > cutoff:
        raise ValueError("US listing receipt is after the current refresh")
    us = listing_update(previous.get("US"))
    if us is None:
        raise ValueError("US listing receipts are older than the previous master")
    active = active_us_listing_ids(us)
    native: tuple[RakutenOrdinaryRow | RakutenRestrictionRow, ...] = (
        *snapshot.rows,
        *snapshot.restriction_only,
    )
    bindings = (
        RakutenListingBinding(key, active[key], row)
        for row in native
        if (key := rakuten_listing_key(row.ticker, row.exchange)) is not None and key in active
    )
    attach = prepare_rakuten_scope_update(
        snapshot=snapshot,
        catalog_venue_counts=totals,
        bindings=bindings,
        expected_session=target,
        cutoff=cutoff,
    )
    joined = attach(us)
    if joined is None or joined.rakuten is None:
        raise ValueError("Rakuten capture is not current for the US listing master")
    targets = (
        (row.listing_key, row.broker_symbol)
        for row in joined.rakuten.rows
        if row.state == "catalog_target"
    )
    eligible = sum(
        row.state == "eligible"
        for row in cover_current_rakuten_catalog_targets(targets, listings=joined.slice.rows)
    )
    if not eligible:
        raise ValueError("no SIP-eligible Rakuten catalog targets in the current master")
    write_private_listing_master_pin(
        args.output_master, encode_current_listing_master({**previous, "US": joined})
    )
    return eligible


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="baibai-jobs rakuten-universe-check",
        description="Check private inputs or assemble a private master without publishing.",
    )
    parser.add_argument("--session", required=True, type=_session, help="verified NY session date")
    parser.add_argument("--catalog", required=True, type=Path, help="UTF-8 ticker,exchange CSV")
    parser.add_argument("--catalog-count", required=True, type=_count, help="broker catalog total")
    parser.add_argument("--catalog-received-at", required=True, type=datetime.fromisoformat)
    parser.add_argument(
        "--venue-count",
        action="append",
        required=True,
        type=_venue_count,
        metavar="EXCHANGE=COUNT",
        help="independently reported ordinary cash total for one venue (repeatable)",
    )
    parser.add_argument(
        "--restrictions", required=True, type=Path, help="UTF-8 ticker,exchange,restriction CSV"
    )
    parser.add_argument(
        "--restrictions-count",
        required=True,
        type=_count,
        help="complete restriction capture total",
    )
    parser.add_argument("--restrictions-received-at", required=True, type=datetime.fromisoformat)
    assembly = parser.add_argument_group("optional local assembly")
    assembly.add_argument("--output-master", type=Path, help="new private current.json file")
    assembly.add_argument("--listing-directory", type=Path, help="verified Nasdaq Trader file pair")
    assembly.add_argument("--nasdaqlisted-received-at", type=datetime.fromisoformat)
    assembly.add_argument("--otherlisted-received-at", type=datetime.fromisoformat)
    assembly.add_argument("--previous-master", type=Path, help="optional prior private pin")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    totals = dict(args.venue_count)
    try:
        listing_args = (
            args.listing_directory,
            args.nasdaqlisted_received_at,
            args.otherlisted_received_at,
        )
        if args.output_master is None:
            if any(value is not None for value in (*listing_args, args.previous_master)):
                raise ValueError("local assembly inputs require --output-master")
        elif any(value is None for value in listing_args):
            raise ValueError("--output-master requires a listing directory and both receipt times")
        if len(totals) != len(args.venue_count):
            raise ValueError("repeated Rakuten venue count")
        snapshot = parse_rakuten_ordinary_snapshot(
            args.catalog.read_text(encoding="utf-8"),
            catalog_mode="cash_ordinary_tradable",
            effective_session=args.session,
            catalog_received_at=args.catalog_received_at,
            reported_count=args.catalog_count,
            exclusions_csv=args.restrictions.read_text(encoding="utf-8"),
            exclusions_received_at=args.restrictions_received_at,
            exclusions_reported_count=args.restrictions_count,
        )
        missing_supported = reconcile_rakuten_catalog_venues(snapshot, totals)
        venues = _venue_report(snapshot, totals)
    except (OSError, UnicodeError):
        print("error: cannot read the normalized UTF-8 CSV files", file=sys.stderr)
        return 2
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    missing = sorted(missing_supported)
    unsupported = sum(
        value["catalog_targets"] or 0 for value in venues.values() if not value["listing_supported"]
    )
    if missing_supported:
        status = "incomplete_supported_intake"
    elif not any(venues[venue]["catalog_targets"] for venue in RAKUTEN_LISTING_EXCHANGES):
        status = "no_supported_catalog_targets"
    else:
        status = "supported_intake_validated"
    local = {}
    if args.output_master is not None and status == "supported_intake_validated":
        try:
            local["listing_sip_targets"] = _assemble_master(args, snapshot, totals)
        except OSError as error:
            print(f"error: private listing master files: {type(error).__name__}", file=sys.stderr)
            return 2
        except ValueError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
    print(
        json.dumps(
            {
                "status": status,
                "effective_session": snapshot.effective_session.isoformat(),
                "catalog_received_at": snapshot.catalog_received_at.isoformat(),
                "restrictions_received_at": args.restrictions_received_at.isoformat(),
                "catalog_count": snapshot.reported_count,
                "restrictions_count": snapshot.exclusions_reported_count,
                "missing_requested_venue_counts": missing,
                "unsupported_catalog_targets": unsupported,
                "venues": venues,
                **local,
            },
            ensure_ascii=False,
        )
    )
    return 0 if status == "supported_intake_validated" else 1
