"""Tender-offer-price proxies for delisted calibration windows.

The filings establish the reported cash-offer outcome and its ordinary-share price. They do
not establish a non-tendering holder's squeeze-out proceeds or payment date. The
calibration convention uses that offer price at the delisting date as an exit proxy,
not as a broker execution or a confirmed shareholder cash receipt.

Registration statements, corrections, withdrawal status and result statements must
agree before the proxy is used. Unresolved cases remain subject to the calibration
sensitivity cases; those cases do not bound every possible recovery value.
Measurement meaning and limits are in docs/reference/estimate-calibration.md.
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

from baibai_engine.machine_analysis.observations.disclosures.delistings import DelistingRecord, read_jpx_delistings
from baibai_engine.market.edinet_decode import is_usable_filing_status
from baibai_engine.machine_analysis.observations.disclosures.edinet_document_store import read_current_filing_documents
from baibai_engine.machine_analysis.observations.financials.edinet_models import EDINETProviderError
from baibai_engine.machine_analysis.observations.disclosures.valuation_catalysts import (
    TENDER_OFFER_REGISTRATION_CORRECTION_DOC_TYPE,
    TENDER_OFFER_REGISTRATION_DOC_TYPE,
    TENDER_OFFER_RESULT_CORRECTION_DOC_TYPE,
    TENDER_OFFER_RESULT_DOC_TYPE,
    TENDER_OFFER_WITHDRAWAL_DOC_TYPE,
    edinet_code_to_ticker,
)
from baibai_engine.storage.observations.schema import connect_current, open_connection

# JPX may name the squeeze-out mechanism rather than its preceding tender offer.
# These reasons select cases to investigate; only the filing evidence below can
# establish a completed cash offer and its ordinary-share price (#1481).
TENDER_OFFER_CANDIDATE_MARKERS = ("公開買付", "株式の併合", "株式併合", "株式等売渡請求")

_PRICE_BLOCK_SUFFIX = "PriceOfPurchaseEtcTextBlock"
_SUCCESS_BLOCK_SUFFIX = "SuccessOrFailureOfTenderOfferTextBlock"
# The price element opens with the statutory table — one row per class of security, each
# either priced or filled with a dash — and then explains how the price was reached.
# Only the table is read: the explanation quotes reference prices, valuation ranges and
# prior offers, any of which would be picked up as a second candidate.
_PRICE_NARRATIVE_HEADING = "算定の基礎"
# Share rows are quoted per share and everything else per unit, so the per-share phrase
# is what separates the share price from a warrant price. The class label in front of it
# varies across filings ("株券", "株券普通株式", "株券対象者株式"); the amount is
# sometimes preceded by 金 and sometimes not. The 円 suffix is required.
_PER_SHARE_PRICE = re.compile(r"[１1]株につき[、,\s　]*金?[\s　]*(?P<yen>[0-9][0-9,]*)円")
# The condition clause states what would happen if the floor were missed ("行わない旨の
# 条件"), so only the polite form that reports what did happen is read. Filings write the
# act as either 買付け or 買付け等.
_OUTCOME = re.compile(r"買付け等?を行いま(?P<outcome>す|せん)")
_SUB_YEN_UNIT = re.compile(r"^[\s　]*[0-9０-９]+[\s　]*銭")
# The funding table names the non-cash consideration, or fills that row with a dash when
# there is none. A share-exchange leg would pay part of the price in stock the holder had
# to sell later, which the offer price would misreport as cash received on the delisting
# day, so the dash is required rather than assumed.
_FUNDING_BLOCK_SUFFIX = "FundEtcForPurchaseEtcTextBlock"
_NON_CASH_CONSIDERATION = re.compile(r"金銭以外の対価の種類[\s　]*(?P<value>.)")
_DASHES = "―—－-‐‑–"


class TenderOfferError(RuntimeError):
    """A tender-offer filing cannot be read into an exit value without guessing."""


class TenderOfferDocumentReader(Protocol):
    def read_csv_zip(self, doc_id: str) -> bytes: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class TenderOfferFiling:
    """One row of the EDINET document index that belongs to a tender offer."""

    doc_id: str
    doc_type_code: str
    doc_date: date
    submit_datetime: str
    sequence_number: int
    offeror_edinet_code: str
    subject_edinet_code: str
    parent_doc_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class TenderOfferExitValue:
    """A verified offer price used as an exit proxy on the delisting date."""

    ticker: str
    delisted_on: date
    offer_price_yen: float
    offer_doc_id: str
    result_doc_id: str
    filed_on: date


@dataclass(frozen=True, slots=True, kw_only=True)
class TenderOfferExitSummary:
    delisting_count: int
    candidate_delisting_count: int
    resolved_count: int
    # Document reads requested, including archives that cannot be decoded.
    read_document_count: int
    rejection_reason_counts: Mapping[str, int]


def parse_ordinary_share_offer_price(blocks: Mapping[str, str]) -> float | None:
    """Read the single per-share cash price from the offer table, or nothing at all.

    Returns ``None`` when the block is absent, when the table quotes more than one
    distinct per-share price — two classes of share, or a price the filing revised in
    place — or when a price carries sub-yen units an integer reading would truncate.
    """
    text = _block(blocks, _PRICE_BLOCK_SUFFIX)
    if not text:
        return None
    table = text.split(_PRICE_NARRATIVE_HEADING, 1)[0]
    prices: set[int] = set()
    for match in _PER_SHARE_PRICE.finditer(table):
        if _SUB_YEN_UNIT.match(table[match.end() :]):
            return None
        prices.add(int(match.group("yen").replace(",", "")))
    if len(prices) != 1:
        return None
    price = prices.pop()
    return float(price) if price > 0 else None


def pays_in_cash_only(blocks: Mapping[str, str]) -> bool | None:
    """Whether the funding table reports no consideration other than cash.

    ``None`` means the table did not answer, which is not the same as answering "cash".
    """
    text = _block(blocks, _FUNDING_BLOCK_SUFFIX)
    if not text:
        return None
    match = _NON_CASH_CONSIDERATION.search(text)
    if match is None:
        return None
    return match.group("value") in _DASHES


def read_tender_offer_outcome(blocks: Mapping[str, str]) -> bool | None:
    """Whether the offer bought the tendered shares, or ``None`` when unreadable."""
    text = _block(blocks, _SUCCESS_BLOCK_SUFFIX)
    if not text:
        return None
    outcomes = {match.group("outcome") for match in _OUTCOME.finditer(text)}
    if len(outcomes) != 1:
        return None
    return outcomes.pop() == "す"


def read_document_blocks(content: bytes) -> dict[str, str]:
    """Flatten one EDINET type=5 archive into element name to value."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as error:
        raise TenderOfferError(f"tender-offer filing is not a readable archive: {error}") from error
    blocks: dict[str, str] = {}
    with archive:
        for name in archive.namelist():
            if not name.lower().endswith(".csv"):
                continue
            try:
                decoded = archive.read(name).decode("utf-16")
            except (UnicodeDecodeError, OSError) as error:
                raise TenderOfferError(f"tender-offer filing CSV is unreadable: {error}") from error
            for row in csv.reader(io.StringIO(decoded), delimiter="\t"):
                if len(row) >= 2:
                    element, value = row[0], row[-1]
                    # Keep disagreement present-but-unreadable. A later duplicate
                    # must not restore one candidate or make an older filing inherit.
                    blocks[element] = (
                        "" if element in blocks and blocks[element] != value else value
                    )
    return blocks


def read_tender_offer_blocks(reader: TenderOfferDocumentReader, doc_id: str) -> Mapping[str, str]:
    """Read a retained EDINET archive; missing or non-ZIP source is fatal."""
    content = reader.read_csv_zip(doc_id)
    if not zipfile.is_zipfile(io.BytesIO(content)):
        raise EDINETProviderError("stored EDINET tender-offer archive is not a ZIP")
    return read_document_blocks(content)


def read_tender_offer_filings(
    connection: sqlite3.Connection, *, through: date
) -> tuple[TenderOfferFiling, ...]:
    """Usable tender-offer origins after their retained operations have been applied."""
    doc_types = {
        TENDER_OFFER_REGISTRATION_DOC_TYPE,
        TENDER_OFFER_REGISTRATION_CORRECTION_DOC_TYPE,
        TENDER_OFFER_WITHDRAWAL_DOC_TYPE,
        TENDER_OFFER_RESULT_DOC_TYPE,
        TENDER_OFFER_RESULT_CORRECTION_DOC_TYPE,
    }
    filings = [
        TenderOfferFiling(
            doc_id=str(doc["docID"]),
            doc_type_code=str(doc["docTypeCode"]),
            doc_date=date.fromisoformat(str(doc["doc_date"])),
            submit_datetime=str(doc.get("submitDateTime") or ""),
            sequence_number=int(doc["seqNumber"]),
            offeror_edinet_code=str(doc["edinetCode"]),
            subject_edinet_code=str(doc["subjectEdinetCode"]),
            parent_doc_id=str(doc["parentDocID"]) if doc.get("parentDocID") else None,
        )
        for doc in read_current_filing_documents(connection, through=through)
        if str(doc.get("docTypeCode")) in doc_types
        and doc.get("edinetCode")
        and doc.get("subjectEdinetCode")
        and is_usable_filing_status(
            doc.get("legalStatus"), doc.get("disclosureStatus"), doc.get("withdrawalStatus")
        )
    ]
    return tuple(sorted(filings, key=_filing_order))


def build_tender_offer_exit_values(
    sqlite_path: Path,
    *,
    reader: TenderOfferDocumentReader,
    asof: date,
) -> tuple[tuple[TenderOfferExitValue, ...], TenderOfferExitSummary]:
    """Derive exit proxies from retained EDINET archives without acquisition."""
    return derive_tender_offer_exit_values(
        sqlite_path,
        read_blocks=lambda doc_id: read_tender_offer_blocks(reader, doc_id),
        asof=asof,
    )


def derive_tender_offer_exit_values(
    sqlite_path: Path,
    *,
    read_blocks: Callable[[str], Mapping[str, str]],
    asof: date,
) -> tuple[tuple[TenderOfferExitValue, ...], TenderOfferExitSummary]:
    """Investigate candidate mechanisms in their established document request order."""
    connection = connect_current(sqlite_path)
    if connection is None:
        raise TenderOfferError(f"market store is not readable: {sqlite_path}")
    try:
        connection.execute("BEGIN")
        ticker_by_code = edinet_code_to_ticker(connection, through=asof)
        filings = read_tender_offer_filings(connection, through=asof)
    finally:
        connection.close()
    delistings = [row for row in read_jpx_delistings(sqlite_path) if row.delisted_on <= asof]
    candidates = [
        row
        for row in delistings
        if any(marker in row.reason for marker in TENDER_OFFER_CANDIDATE_MARKERS)
    ]
    by_target = _filings_by_target_ticker(filings, ticker_by_code)

    exits: list[TenderOfferExitValue] = []
    reasons: dict[str, int] = {}
    reads = 0

    def counted_read_blocks(doc_id: str) -> Mapping[str, str]:
        nonlocal reads
        reads += 1
        return read_blocks(doc_id)

    for delisting in candidates:
        case, reason = _select_case(by_target.get(delisting.ticker, ()), delisting=delisting)
        if case is None:
            reasons[reason] = reasons.get(reason, 0) + 1
            continue
        try:
            value = _realize_case(case, delisting=delisting, read_blocks=counted_read_blocks)
        except TenderOfferError:
            reasons["document_unreadable"] = reasons.get("document_unreadable", 0) + 1
            continue
        if value is None:
            reasons["price_or_outcome_unreadable"] = (
                reasons.get("price_or_outcome_unreadable", 0) + 1
            )
            continue
        exits.append(value)
    return tuple(sorted(exits, key=lambda value: (value.ticker, value.delisted_on))), (
        TenderOfferExitSummary(
            delisting_count=len(delistings),
            candidate_delisting_count=len(candidates),
            resolved_count=len(exits),
            read_document_count=reads,
            rejection_reason_counts=dict(sorted(reasons.items())),
        )
    )


def store_tender_offer_exit_values(
    sqlite_path: Path, values: Sequence[TenderOfferExitValue]
) -> int:
    """Replace derived proxies with exactly the currently established values.

    An empty successful derivation retracts old proxies just as a partial result
    retracts omitted ones. Source acquisition failures must propagate before this
    write; unavailable evidence is never permission to retain an old conclusion.
    """
    connection = open_connection(sqlite_path)
    try:
        connection.execute("DELETE FROM tender_offer_exit_values")
        connection.executemany(
            "INSERT INTO tender_offer_exit_values("
            "ticker, delisted_on, offer_price_yen, offer_doc_id, result_doc_id, filed_on"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                (
                    value.ticker,
                    value.delisted_on.isoformat(),
                    value.offer_price_yen,
                    value.offer_doc_id,
                    value.result_doc_id,
                    value.filed_on.isoformat(),
                )
                for value in values
            ),
        )
        connection.commit()
        return len(values)
    finally:
        connection.close()


def read_tender_offer_exit_values(sqlite_path: Path) -> tuple[TenderOfferExitValue, ...]:
    connection = connect_current(sqlite_path)
    if connection is None:
        return ()
    try:
        rows = connection.execute(
            "SELECT ticker, delisted_on, offer_price_yen, offer_doc_id, result_doc_id, filed_on "
            "FROM tender_offer_exit_values ORDER BY ticker, delisted_on"
        ).fetchall()
    finally:
        connection.close()
    return tuple(
        TenderOfferExitValue(
            ticker=str(ticker),
            delisted_on=date.fromisoformat(str(delisted_on)),
            offer_price_yen=float(price),
            offer_doc_id=str(offer_doc_id),
            result_doc_id=str(result_doc_id),
            filed_on=date.fromisoformat(str(filed_on)),
        )
        for ticker, delisted_on, price, offer_doc_id, result_doc_id, filed_on in rows
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _Case:
    """The filings one offeror made about one target before it stopped trading.

    `offers` holds one chain per registration statement: the statement itself followed
    by the corrections filed against it. One offeror can run two offers over the same
    target at once — a two-tier deal prices the pre-agreed holders apart from everyone
    else — so the chains are kept separate instead of being flattened into a single
    "latest price".
    """

    offers: tuple[tuple[TenderOfferFiling, ...], ...]
    result_document: TenderOfferFiling
    result_corrections: tuple[TenderOfferFiling, ...]


def _filings_by_target_ticker(
    filings: Iterable[TenderOfferFiling], ticker_by_code: Mapping[str, str]
) -> dict[str, tuple[TenderOfferFiling, ...]]:
    grouped: dict[str, list[TenderOfferFiling]] = {}
    for filing in filings:
        ticker = ticker_by_code.get(filing.subject_edinet_code)
        if ticker is not None:
            grouped.setdefault(ticker, []).append(filing)
    return {ticker: tuple(rows) for ticker, rows in grouped.items()}


def _select_case(
    filings: Sequence[TenderOfferFiling], *, delisting: DelistingRecord
) -> tuple[_Case | None, str]:
    """Pick the one offer that ended this listing, or say why none can be picked."""
    relevant = [filing for filing in filings if filing.doc_date <= delisting.delisted_on]
    if not relevant:
        return None, "no_filing_in_document_window"
    latest_round = _latest_offer_round(relevant)
    if latest_round is None:
        return None, "incomplete_case"
    by_offeror: dict[str, list[TenderOfferFiling]] = {}
    for filing in latest_round:
        by_offeror.setdefault(filing.offeror_edinet_code, []).append(filing)

    cases: list[_Case] = []
    withdrawn = False
    missing_result = False
    for offeror_filings in by_offeror.values():
        if any(
            filing.doc_type_code == TENDER_OFFER_WITHDRAWAL_DOC_TYPE for filing in offeror_filings
        ):
            withdrawn = True
            continue
        offers = _registration_chains(offeror_filings)
        results = [
            filing
            for filing in offeror_filings
            if filing.doc_type_code == TENDER_OFFER_RESULT_DOC_TYPE
        ]
        if not offers or not results:
            missing_result = True
            continue
        cases.append(
            _Case(
                offers=offers,
                result_document=results[-1],
                result_corrections=tuple(
                    filing
                    for filing in offeror_filings
                    if filing.doc_type_code == TENDER_OFFER_RESULT_CORRECTION_DOC_TYPE
                ),
            )
        )
    if len(cases) > 1:
        return None, "multiple_offerors"
    if cases and missing_result:
        return None, "incomplete_case"
    if not cases:
        return None, "withdrawn" if withdrawn and not missing_result else "incomplete_case"
    return cases[0], ""


def _latest_offer_round(
    filings: Sequence[TenderOfferFiling],
) -> list[TenderOfferFiling] | None:
    """Separate completed campaigns while retaining overlapping bids and tiers.

    A new registration starts a separate round only after every open registration
    has a result or withdrawal. Parent IDs attach late corrections to their original
    round. An unknown parent or an unfinished competing bid cannot prove separation.
    Same-day closing/opening filings stay together rather than implying an order
    between two offers from their submission times alone.
    """
    registrations: dict[str, TenderOfferFiling] = {}
    roots_by_doc: dict[str, set[str]] = {}
    open_roots: set[str] = set()
    round_roots: set[str] = set()
    members: list[tuple[TenderOfferFiling, set[str]]] = []
    ended_on = date.min
    for filing in sorted(filings, key=_filing_order):
        if filing.doc_type_code == TENDER_OFFER_REGISTRATION_DOC_TYPE:
            if not open_roots and filing.doc_date > ended_on:
                round_roots = set()
            roots = {filing.doc_id}
            registrations[filing.doc_id] = filing
            open_roots.update(roots)
            round_roots.update(roots)
        elif filing.parent_doc_id:
            roots = roots_by_doc.get(filing.parent_doc_id, set())
            if not roots or any(
                registrations[root].offeror_edinet_code != filing.offeror_edinet_code
                or registrations[root].subject_edinet_code != filing.subject_edinet_code
                for root in roots
            ):
                return None
        else:
            if filing.doc_type_code in {
                TENDER_OFFER_REGISTRATION_CORRECTION_DOC_TYPE,
                TENDER_OFFER_RESULT_CORRECTION_DOC_TYPE,
            }:
                return None  # A correction must identify the document it changes.
            # A result or withdrawal without a parent applies to this offeror's
            # active registrations, including every active tier.
            roots = {
                root
                for root in open_roots
                if registrations[root].offeror_edinet_code == filing.offeror_edinet_code
            }
            if not roots:
                previous = [
                    root
                    for root, registration in registrations.items()
                    if registration.offeror_edinet_code == filing.offeror_edinet_code
                ]
                roots = set(previous[-1:])
            if not roots:
                return None
        roots_by_doc[filing.doc_id] = roots
        members.append((filing, roots))
        if filing.doc_type_code in (
            TENDER_OFFER_RESULT_DOC_TYPE,
            TENDER_OFFER_WITHDRAWAL_DOC_TYPE,
        ):
            open_roots.difference_update(roots)
            ended_on = max(ended_on, filing.doc_date)
    return [filing for filing, roots in members if roots & round_roots]


def _realize_case(
    case: _Case,
    *,
    delisting: DelistingRecord,
    read_blocks: Callable[[str], Mapping[str, str]],
) -> TenderOfferExitValue | None:
    """Read the final price state and the reported outcome out of the filings."""
    outcome: bool | None = None
    result_doc_id = ""
    # A correction supersedes the result it corrects, and a correction that leaves the
    # outcome alone omits the block, so the newest document that states an outcome at
    # all is the one that holds. A present but unreadable revision blocks fallback.
    for filing in (case.result_document, *case.result_corrections)[::-1]:
        blocks = read_blocks(filing.doc_id)
        outcome = read_tender_offer_outcome(blocks)
        if outcome is not None:
            result_doc_id = filing.doc_id
            break
        if _block(blocks, _SUCCESS_BLOCK_SUFFIX) is not None:
            return None
    if outcome is not True:
        return None

    priced: list[tuple[float, TenderOfferFiling]] = []
    for chain in case.offers:
        # A correction restates only what it changes, so the newest document in the
        # chain that prints a price at all carries that offer's final price, and the
        # newest that reports the funding carries the consideration it was paid in.
        # Only an omitted block inherits an older value; unreadable revisions do not.
        cash_only: bool | None = None
        priced_filing: tuple[float, TenderOfferFiling] | None = None
        for filing in chain[::-1]:
            blocks = read_blocks(filing.doc_id)
            if cash_only is None:
                cash_only = pays_in_cash_only(blocks)
                if cash_only is None and _block(blocks, _FUNDING_BLOCK_SUFFIX) is not None:
                    return None
            if priced_filing is None:
                price = parse_ordinary_share_offer_price(blocks)
                if price is not None:
                    priced_filing = (price, filing)
                elif _block(blocks, _PRICE_BLOCK_SUFFIX) is not None:
                    return None
            if priced_filing is not None and cash_only is not None:
                break
        if priced_filing is None or cash_only is not True:
            # An offer this offeror ran that cannot be read is not an offer that did not
            # happen. Dropping it would let a second tier — the one whose funding names
            # a non-cash leg, or whose price the table does not state — disappear, and
            # the remaining tier would then look like the single price of the case.
            return None
        priced.append(priced_filing)
    if len({price for price, _ in priced}) != 1:
        return None
    price, filing = priced[0]
    return TenderOfferExitValue(
        ticker=delisting.ticker,
        delisted_on=delisting.delisted_on,
        offer_price_yen=price,
        offer_doc_id=filing.doc_id,
        result_doc_id=result_doc_id,
        filed_on=filing.doc_date,
    )


def _registration_chains(
    filings: Sequence[TenderOfferFiling],
) -> tuple[tuple[TenderOfferFiling, ...], ...]:
    """Split one offeror's registration filings into one chain per offer.

    Parent IDs own correction membership; missing ancestry remains unresolved.
    """
    chains: list[list[TenderOfferFiling]] = []
    chain_by_doc: dict[str, list[TenderOfferFiling]] = {}
    for filing in filings:
        if filing.doc_type_code == TENDER_OFFER_REGISTRATION_DOC_TYPE:
            chains.append([filing])
            chain_by_doc[filing.doc_id] = chains[-1]
        elif filing.doc_type_code == TENDER_OFFER_REGISTRATION_CORRECTION_DOC_TYPE and chains:
            chain = chain_by_doc.get(filing.parent_doc_id) if filing.parent_doc_id else None
            if chain is None:
                return ()
            chain.append(filing)
            chain_by_doc[filing.doc_id] = chain
    return tuple(tuple(chain) for chain in chains)


def _filing_order(filing: TenderOfferFiling) -> tuple[str, str, int]:
    return filing.doc_date.isoformat(), filing.submit_datetime, filing.sequence_number


def _block(blocks: Mapping[str, str], suffix: str) -> str | None:
    values = {value for element, value in blocks.items() if element.endswith(suffix)}
    if not values:
        return None
    # Different element prefixes do not establish precedence for the same block.
    # Empty means present but unreadable, distinct from an omitted correction.
    return next(iter(values)) if len(values) == 1 else ""


__all__ = (
    "TENDER_OFFER_CANDIDATE_MARKERS",
    "TenderOfferDocumentReader",
    "TenderOfferError",
    "TenderOfferExitSummary",
    "TenderOfferExitValue",
    "TenderOfferFiling",
    "build_tender_offer_exit_values",
    "derive_tender_offer_exit_values",
    "parse_ordinary_share_offer_price",
    "pays_in_cash_only",
    "read_document_blocks",
    "read_tender_offer_blocks",
    "read_tender_offer_exit_values",
    "read_tender_offer_filings",
    "read_tender_offer_outcome",
    "store_tender_offer_exit_values",
)
