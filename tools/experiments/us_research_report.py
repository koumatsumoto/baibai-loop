"""Review USD research or save and read a non-executable decision in an isolated App."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Never
from urllib.parse import parse_qsl, urlsplit

from tools.experiments.us_research_mvp import _adjacent_loader, _fixed_alpaca_capture

from baibai_engine.data_collection.providers.alpaca import BarsCapture
from baibai_engine.data_collection.providers.alpaca_auctions import (
    NasdaqOfficialCloseQuery,
    SipAuctionReceipt,
)
from baibai_engine.data_collection.providers.alpaca_calendar import (
    AlpacaCalendarReading,
    AlpacaCalendarResponse,
    read_paper_us_calendar,
)
from baibai_engine.investment_judgment.us_proposal import (
    CloseProof,
    ReportDraft,
    ReportReview,
    ReviewedReport,
    SipCloseProof,
    USProposalCurrentInputs,
    USProposalDraft,
    USProposalReview,
    admit_new_us_report,
    draft_digest,
    load_latest_us_proposal,
    load_us_proposal,
    prepare_us_proposal,
    render_reviewed_report,
    require_close_receipt,
    reviewed_report,
    save_us_proposal,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    read_local_listing_master,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.sqlite.application.read import connect_read_only


def _unique_fields(fields: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(fields)
    if len(result) != len(fields):
        raise ValueError("duplicate JSON field")
    return result


def _read(path: Path) -> object:
    return json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=_unique_fields, parse_float=Decimal
    )


def _transfer_time(stamp: object) -> datetime:
    if (
        not isinstance(stamp, str)
        or stamp.endswith("-00:00")
        or re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
            r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])",
            stamp,
        )
        is None
    ):
        raise ValueError("receipt requires an exact transfer time")
    return datetime.fromisoformat(stamp)


def _sip_proof(draft: ReportDraft, paths: list[Path] | None) -> SipCloseProof | None:
    if paths is None:
        return None
    metadata, original = paths
    receipt = _read(metadata)
    if not isinstance(receipt, dict):
        raise ValueError("invalid SIP receipt metadata")
    url, response = receipt.get("request_url"), receipt.get("response_url")
    received_at, digest = receipt.get("received_at"), receipt.get("sha256")
    status, complete, redirected = (
        receipt.get("status"),
        receipt.get("complete"),
        receipt.get("redirected"),
    )
    if (
        not isinstance(url, str)
        or not isinstance(response, str)
        or not isinstance(digest, str)
        or type(status) is not int
        or type(complete) is not bool
        or type(redirected) is not bool
    ):
        raise ValueError("SIP receipt requires exact transfer metadata")
    # Decode only the bounds. The provider owner still requires both full captured
    # URLs to equal its reconstruction for this report's literal ticker and SIP scope.
    bounds = dict(parse_qsl(urlsplit(url).query, strict_parsing=True, max_num_fields=6))
    if not {"start", "end"} <= bounds.keys():
        raise ValueError("SIP receipt has no fixed query bounds")
    return SipCloseProof(
        query=NasdaqOfficialCloseQuery(
            draft.ticker,
            datetime.fromisoformat(bounds["start"]),
            datetime.fromisoformat(bounds["end"]),
        ),
        receipt=SipAuctionReceipt(
            request_url=url,
            response_url=response,
            status_code=status,
            received_at=_transfer_time(received_at),
            body_sha256=digest,
            complete=complete,
            redirected=redirected,
            body=original.read_bytes(),
        ),
    )


def _daily_close_proof(draft: ReportDraft, manifest: Path) -> BarsCapture:
    spec = _read(manifest)
    if not isinstance(spec, dict) or not isinstance(symbols := spec.get("symbols"), list):
        raise ValueError("daily close manifest requires the fixed request symbols")
    capture = _fixed_alpaca_capture(
        spec,
        tuple(symbols),
        _adjacent_loader(manifest),
        draft.cutoff_at,
        feed="sip",
        adjustment="raw",
    )
    if isinstance(capture, str):
        raise ValueError("daily close manifest is not a complete raw SIP capture")
    return capture


def _calendar_proof(path: Path) -> AlpacaCalendarReading:
    # This is a run-local capture, not a new live request or a calendar store.
    with path.open("rb") as stream:
        data = stream.read(32769)
    if len(data) > 32768:
        raise ValueError("US calendar receipt exceeds its size bound")
    spec = json.loads(data, object_pairs_hook=_unique_fields)
    if not isinstance(spec, dict) or set(spec) != {
        "start",
        "end",
        "request_url",
        "response_url",
        "received_at",
        "status",
        "complete",
        "redirected",
        "body",
        "sha256",
    }:
        raise ValueError("invalid US calendar receipt fields")
    if (
        not isinstance(body := spec["body"], str)
        or spec["redirected"] is not False
        or type(spec["status"]) is not int
        or spec["complete"] is not True
    ):
        raise ValueError("unverified US calendar receipt")
    payload = body.encode("utf-8")
    if sha256(payload).hexdigest() != spec["sha256"]:
        raise ValueError("US calendar receipt body differs")

    def fixed_response(url: str) -> AlpacaCalendarResponse:
        if spec["request_url"] != url:
            raise ValueError("US calendar receipt request does not match its window")
        return AlpacaCalendarResponse(
            response_url=spec["response_url"],
            status_code=spec["status"],
            received_at=_transfer_time(spec["received_at"]),
            body=payload,
            complete=True,
        )

    return read_paper_us_calendar(
        date.fromisoformat(spec["start"]),
        date.fromisoformat(spec["end"]),
        get_authorized=fixed_response,
    )


def _current_inputs(args: argparse.Namespace) -> USProposalCurrentInputs:
    if args.current_listing_master is None or args.calendar_proof is None:
        raise ValueError("new US Research or decision requires the current master and calendar")
    return (
        read_local_listing_master(args.current_listing_master).get("US"),
        _calendar_proof(args.calendar_proof),
    )


def _proof(draft: ReportDraft, args: argparse.Namespace) -> CloseProof | None:
    if args.daily_close_proof is not None:
        return _daily_close_proof(draft, args.daily_close_proof)
    return _sip_proof(draft, args.sip_proof)


def _save(
    path: Path, draft: ReportDraft, review: ReportReview, *, sip_proof: CloseProof | None
) -> None:
    saved = reviewed_report(draft, review, sip_proof=sip_proof)
    content = canonical_json(saved.model_dump(mode="json")).encode() + b"\n"
    # One create-only private file. Do not replace a draft, prior report or symlink.
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(content)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        self.exit(2, "FAIL arguments\n")


def build_parser() -> _Parser:
    parser = _Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    draft_digest_command = commands.add_parser("draft-digest")
    draft_digest_command.add_argument("draft", type=Path)
    save = commands.add_parser("save")
    save.add_argument("draft", type=Path)
    save.add_argument("review", type=Path)
    save.add_argument("--output", type=Path, required=True)
    commands.add_parser("render").add_argument("saved", type=Path)
    check = commands.add_parser("check-render")
    check.add_argument("saved", type=Path)
    check.add_argument("rendered", type=Path)
    digest = commands.add_parser("proposal-digest", help="bind a USD decision to reviewed analysis")
    publish = commands.add_parser("proposal-save", help="save the reviewed decision in an App copy")
    for command in (digest, publish):
        command.add_argument("saved_analysis", type=Path)
        command.add_argument("decision", type=Path)
    publish.add_argument("proposal_review", type=Path)
    publish.add_argument("--db", type=Path, required=True)
    show = commands.add_parser("proposal-show", help="read an exact or latest saved US decision")
    show.add_argument("--db", type=Path, required=True)
    show.add_argument("--proposal-id")
    for authoring in (draft_digest_command, save, digest, publish):
        authoring.add_argument("--current-listing-master", type=Path, metavar="PATH")
        authoring.add_argument("--calendar-proof", type=Path, metavar="RECEIPT")
        sources = authoring.add_mutually_exclusive_group()
        sources.add_argument(
            "--sip-proof",
            nargs=2,
            type=Path,
            metavar=("RECEIPT", "ORIGINAL"),
            help="private authorized SIP auction receipt for a relayed official close",
        )
        sources.add_argument(
            "--daily-close-proof",
            type=Path,
            metavar="CAPTURE",
            help="private fixed receipt manifest for the raw SIP daily close",
        )
    return parser


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    args = build_parser().parse_args(argv)
    stage = "draft"

    def checked_time() -> datetime:
        return now if now is not None else datetime.now(UTC)

    try:
        if args.command in {"draft-digest", "save"}:
            draft = ReportDraft.model_validate(_read(args.draft))
            proof = _proof(draft, args)
            draft = admit_new_us_report(draft, *_current_inputs(args), checked_at=checked_time())
            if args.command == "save":
                stage = "review/save"
                _save(
                    args.output,
                    draft,
                    ReportReview.model_validate(_read(args.review)),
                    sip_proof=proof,
                )
            else:
                require_close_receipt(draft, proof)
                print(
                    canonical_json(
                        {"report_id": draft.report_id, "draft_sha256": draft_digest(draft)}
                    )
                )
        elif args.command in {"render", "check-render"}:
            stage = "saved report"
            saved = ReviewedReport.model_validate(_read(args.saved))
            content = render_reviewed_report(saved)
            if args.command == "render":
                sys.stdout.buffer.write(content)
            else:
                stage = "rendered file"
                if args.rendered.read_bytes() != content:
                    raise ValueError("rendered content differs")
                print("PASS exact private analytical report render")
        elif args.command == "proposal-show":
            stage = "US App read"
            with closing(connect_read_only(args.db)) as connection:
                record = (
                    load_latest_us_proposal(connection)
                    if args.proposal_id is None
                    else load_us_proposal(connection, args.proposal_id)
                )
            print(canonical_json(None if record is None else record.model_dump(mode="json")))
            return 1 if record is None else 0
        else:
            stage = "US proposal draft"
            analysis = ReviewedReport.model_validate(_read(args.saved_analysis))
            decision = USProposalDraft.model_validate(_read(args.decision))
            proof = _proof(analysis.draft, args)
            if args.command == "proposal-digest":
                digest_value = prepare_us_proposal(
                    decision,
                    analysis,
                    current=_current_inputs(args),
                    checked_at=checked_time(),
                    sip_proof=proof,
                )
            else:
                stage = "US proposal save"
                review = USProposalReview.model_validate(_read(args.proposal_review))
                saved_proposal = save_us_proposal(
                    args.db,
                    decision,
                    analysis,
                    review,
                    read_current=lambda: _current_inputs(args),
                    clock=checked_time,
                    sip_proof=proof,
                )
                digest_value = saved_proposal.review.draft_sha256
            print(
                canonical_json({"proposal_id": decision.proposal_id, "draft_sha256": digest_value})
            )
    except (OSError, ValueError, TypeError, ArithmeticError, RuntimeError, sqlite3.Error):
        print(f"FAIL {stage}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
