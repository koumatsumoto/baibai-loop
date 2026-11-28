"""Check an operator's normalized Rakuten cash ordinary-share capture locally."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import TypedDict

from baibai_engine.machine_analysis.observations.stocks.us_universe import (
    RAKUTEN_LISTING_EXCHANGES,
    RakutenOrdinarySnapshot,
    parse_rakuten_ordinary_snapshot,
)

# Broker cash product scope, not a claim of ordinary shares or matching support for each venue.
_REQUESTED_EXCHANGES = RAKUTEN_LISTING_EXCHANGES | {"NYSE American", "NYSE Arca", "Cboe"}


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
    observed = Counter(row.exchange for row in snapshot.rows)
    if observed.keys() - totals.keys() or any(observed[venue] != n for venue, n in totals.items()):
        raise ValueError("catalog rows do not match the independently reported venue totals")
    catalog_status = Counter((row.exchange, row.buy_status) for row in snapshot.rows)
    restriction_only = Counter((row.exchange, row.restriction) for row in snapshot.restriction_only)
    venues = _REQUESTED_EXCHANGES | totals.keys() | {venue for venue, _ in restriction_only}
    return {
        venue: VenueCounts(
            requested=venue in _REQUESTED_EXCHANGES,
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
    return venue, _count(count)


def _session(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use NY session date YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("use NY session date YYYY-MM-DD")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="baibai-jobs rakuten-universe-check",
        description="Validate private normalized inputs only; no network or publication.",
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
    args = parser.parse_args(argv)
    totals = dict(args.venue_count)
    try:
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
        venues = _venue_report(snapshot, totals)
    except (OSError, UnicodeError):
        print("error: cannot read the normalized UTF-8 CSV files", file=sys.stderr)
        return 2
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    missing = sorted(_REQUESTED_EXCHANGES - totals.keys())
    unsupported = sum(
        value["catalog_targets"] or 0 for value in venues.values() if not value["listing_supported"]
    )
    if RAKUTEN_LISTING_EXCHANGES - totals.keys():
        status = "incomplete_supported_intake"
    elif not any(venues[venue]["catalog_targets"] for venue in RAKUTEN_LISTING_EXCHANGES):
        status = "no_supported_catalog_targets"
    else:
        status = "supported_intake_validated"
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
            },
            ensure_ascii=False,
        )
    )
    return 0 if status == "supported_intake_validated" else 1
