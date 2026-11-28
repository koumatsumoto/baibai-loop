"""Read a trusted run-private split SIP packet and check US L2 input without writing."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterator, Sequence
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID

from tools.experiments.us_research_mvp import _fixed_alpaca_capture
from tools.experiments.us_research_report import (
    CALENDAR_PROOF_MAX_BYTES,
    _decode,
    _transfer_time,
    calendar_proof_from_bytes,
)

from baibai_engine.data_collection.providers.alpaca import BarsCapture
from baibai_engine.data_collection.providers.alpaca_http import BARS_BODY_MAX_BYTES
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    LISTING_MASTER_MAX_BYTES,
)
from baibai_engine.primitives.json import canonical_json
from baibai_jobs.integration.us_daily_plan import TECHNICAL_CALENDAR_CHUNKS
from baibai_jobs.integration.us_technical_handoff import (
    SOURCE,
    TECHNICAL_PAGE_LIMIT,
    technical_request_id,
)
from baibai_jobs.integration.us_technical_input import (
    USTechnicalInput,
    admit_us_technical_input,
)

_RESULT_MAX_BYTES = 1024**2
_SHA = re.compile(r"[0-9a-f]{64}")


def _bounded_file(path: Path, limit: int) -> bytes:
    if not path.is_file():
        raise ValueError("fixed input must be a file")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("fixed input exceeds its size bound")
    return raw


def _member(root: Path, relative: str, limit: int, *, digest: str | None = None) -> bytes:
    path = root / relative
    if not path.is_relative_to(root) or ".." in Path(relative).parts or path.resolve() != path:
        raise ValueError("fixed input must be inside its packet without aliases")
    raw = _bounded_file(path, limit)
    if digest is not None and sha256(raw).hexdigest() != digest:
        raise ValueError("packet member differs from committed bytes")
    return raw


def _digest(value: object) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ValueError("packet requires SHA-256 digests")
    return value


def _digests(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("packet requires its exact nonempty receipt inventory")
    items = tuple(_digest(raw) for raw in value)
    if len(set(items)) != len(items):
        raise ValueError("packet repeats a receipt")
    return items


def _captures(
    directory: Path,
    digests: tuple[str, ...],
    *,
    attempt: UUID,
    listing_sha: str,
    as_of: date,
    cutoff: datetime,
) -> Iterator[BarsCapture]:
    for ordinal, digest in enumerate(digests, 1):
        batch = f"batch-{ordinal:03d}"
        raw = _member(directory, f"{batch}/capture.json", BARS_BODY_MAX_BYTES, digest=digest)
        spec = _decode(raw)
        if not isinstance(spec, dict) or set(spec) != {
            "request_id",
            "start",
            "end",
            "symbols",
            "pages",
        }:
            raise ValueError("invalid technical capture fields")
        if (
            spec["request_id"]
            != technical_request_id(attempt, listing_sha=listing_sha, as_of=as_of, ordinal=ordinal)
            or not isinstance(symbols := spec["symbols"], list)
            or not isinstance(pages := spec["pages"], list)
        ):
            raise ValueError("capture does not belong to the fixed technical attempt")

        def load(name: str, batch: str = batch) -> bytes:
            if Path(name).name != name:
                raise ValueError("provider body must be next to its capture")
            return _member(directory, f"{batch}/{name}", BARS_BODY_MAX_BYTES)

        capture = _fixed_alpaca_capture(
            spec,
            tuple(symbols),
            load,
            cutoff,
            feed="sip",
            adjustment="split",
            page_limit=TECHNICAL_PAGE_LIMIT,
        )
        if isinstance(capture, str) or any(
            raw["receipt_id"] != page.receipt_id
            for raw, page in zip(pages, capture.pages, strict=True)
        ):
            raise ValueError("technical response chain is not the recorded SIP split capture")
        yield capture


def check_packet(directory: Path, listing: Path) -> USTechnicalInput | None:
    """Only accept one attempt's committed proofs, never an operator-supplied replacement SHA."""
    directory = directory.resolve()
    report = _decode(_member(directory, "result.json", _RESULT_MAX_BYTES))
    if not isinstance(report, dict) or set(report) != {
        "source",
        "attempt_id",
        "listing_master_sha256",
        "asof",
        "cutoff_at",
        "calendars",
        "batches",
    }:
        raise ValueError("invalid technical packet fields")
    if report["source"] != SOURCE:
        raise ValueError("not a split technical packet")
    attempt_text, date_text = report["attempt_id"], report["asof"]
    if not isinstance(attempt_text, str) or not isinstance(date_text, str):
        raise ValueError("technical attempt and NY session must be strings")
    attempt, as_of = UUID(attempt_text), date.fromisoformat(date_text)
    if str(attempt) != attempt_text or as_of.isoformat() != date_text:
        raise ValueError("technical attempt and NY session require canonical IDs")
    cutoff = _transfer_time(report["cutoff_at"])
    listing_sha = _digest(report["listing_master_sha256"])
    listing_bytes = _bounded_file(listing, LISTING_MASTER_MAX_BYTES)
    if sha256(listing_bytes).hexdigest() != listing_sha:
        raise ValueError("fixed listing master differs from the packet")
    calendar_shas = _digests(report["calendars"])
    if len(calendar_shas) > TECHNICAL_CALENDAR_CHUNKS:
        raise ValueError("technical calendar inventory exceeds the private window bound")
    calendars = tuple(
        calendar_proof_from_bytes(
            _member(
                directory, f"calendar-{index:03d}.json", CALENDAR_PROOF_MAX_BYTES, digest=digest
            )
        )
        for index, digest in enumerate(calendar_shas, 1)
    )
    result = admit_us_technical_input(
        listing_bytes=listing_bytes,
        captured_listing_sha256=listing_sha,
        calendars=calendars,
        captures=_captures(
            directory,
            _digests(report["batches"]),
            attempt=attempt,
            listing_sha=listing_sha,
            as_of=as_of,
            cutoff=cutoff,
        ),
        cutoff=cutoff,
    )
    if result is not None and result.price_session != as_of:
        raise ValueError("technical packet labels a different NY session")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--current-listing-master", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = check_packet(args.packet, args.current_listing_master)
    except (
        OSError,
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        ArithmeticError,
        RecursionError,
    ):
        print("US technical receipt packet invalid", file=sys.stderr)
        return 2
    if result is None:
        print(json.dumps({"status": "unavailable"}))
        return 1
    print(
        canonical_json(
            {
                "status": "admitted",
                "listing_session": result.listing_session,
                "price_session": result.price_session,
                "models": len(result.models),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
