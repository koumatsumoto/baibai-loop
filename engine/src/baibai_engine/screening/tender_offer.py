"""Realized cash exit values for names a completed tender offer took off the market.

A delisted name has no market close to end a forward window with, so the calibration
authority brackets it between a total loss and what the rest of its cohort returned. A
takeover settles above the neutral case, so that bracket cannot bound the error from
above. When the offer actually completed in cash, the price it paid is the observed exit
and no imputation is needed.

Everything here is fail-closed. A case is realized only when the registration statement,
its corrections, the absence of a withdrawal and the result statement agree on one
ordinary-share cash price for one target. Anything else is left to the bracket rather
than estimated, because a wrong exit price is worse than an acknowledged gap. The rules
are pre-registered in reports/studies/2026-08-11-capital-control-exit-values/.
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

from baibai_engine.market.sqlite import connect_current, open_connection

from .delistings import DelistingRecord, read_jpx_delistings
from .valuation_catalysts import (
    TENDER_OFFER_REGISTRATION_CORRECTION_DOC_TYPE,
    TENDER_OFFER_REGISTRATION_DOC_TYPE,
    TENDER_OFFER_RESULT_CORRECTION_DOC_TYPE,
    TENDER_OFFER_RESULT_DOC_TYPE,
    TENDER_OFFER_WITHDRAWAL_DOC_TYPE,
    edinet_code_to_ticker,
    is_usable_filing_status,
)

# JPX names the mechanism in the delisting reason. Only a reason that names a tender
# offer can be priced from a tender-offer filing; a share consolidation or a demand for
# sale of shares with no tender offer behind it settles at a price this module never
# sees, and stays bracketed.
TENDER_OFFER_DELISTING_MARKER = "公開買付"

_PRICE_BLOCK_SUFFIX = "PriceOfPurchaseEtcTextBlock"
_SUCCESS_BLOCK_SUFFIX = "SuccessOrFailureOfTenderOfferTextBlock"
# The price element opens with the statutory table — one row per class of security, each
# either priced or filled with a dash — and then explains how the price was reached.
# Only the table is read: the explanation quotes reference prices, valuation ranges and
# prior offers, any of which would be picked up as a second candidate.
_PRICE_NARRATIVE_HEADING = "算定の基礎"
# Share rows are quoted per share and everything else per unit, so the per-share phrase
# is what separates the share price from a warrant price. The class label in front of it
# varies across filings ("株券", "株券普通株式", "株券対象者株式"), and the yen marker is
# sometimes dropped, so neither is required — the per-share phrase carries the meaning.
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


class TenderOfferDocumentSource(Protocol):
    def download_csv_zip(self, doc_id: str) -> bytes: ...


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


@dataclass(frozen=True, slots=True, kw_only=True)
class TenderOfferExitValue:
    """One realized cash exit, keyed by the ticker and the day it stopped trading."""

    ticker: str
    delisted_on: date
    offer_price_yen: float
    offer_doc_id: str
    result_doc_id: str
    filed_on: date


@dataclass(frozen=True, slots=True, kw_only=True)
class TenderOfferExitSummary:
    delisting_count: int
    tender_offer_delisting_count: int
    resolved_count: int
    downloaded_document_count: int
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
                    blocks[row[0]] = row[-1]
    return blocks


def read_tender_offer_filings(
    connection: sqlite3.Connection, *, through: date
) -> tuple[TenderOfferFiling, ...]:
    """Every usable tender-offer index row that names both an offeror and a target."""
    doc_types = (
        TENDER_OFFER_REGISTRATION_DOC_TYPE,
        TENDER_OFFER_REGISTRATION_CORRECTION_DOC_TYPE,
        TENDER_OFFER_WITHDRAWAL_DOC_TYPE,
        TENDER_OFFER_RESULT_DOC_TYPE,
        TENDER_OFFER_RESULT_CORRECTION_DOC_TYPE,
    )
    placeholders = ",".join("?" for _ in doc_types)
    rows = connection.execute(
        "SELECT doc_id, doc_type_code, doc_date, submit_datetime, sequence_number, "
        "edinet_code, subject_edinet_code, legal_status, disclosure_status, withdrawal_status "
        f"FROM edinet_documents WHERE doc_date <= ? AND doc_type_code IN ({placeholders}) "  # nosec B608
        "AND edinet_code IS NOT NULL AND subject_edinet_code IS NOT NULL",
        (through.isoformat(), *doc_types),
    ).fetchall()
    filings = [
        TenderOfferFiling(
            doc_id=str(doc_id),
            doc_type_code=str(doc_type_code),
            doc_date=date.fromisoformat(str(doc_date)),
            submit_datetime=str(submit_datetime or ""),
            sequence_number=int(sequence_number),
            offeror_edinet_code=str(offeror),
            subject_edinet_code=str(subject),
        )
        for (
            doc_id,
            doc_type_code,
            doc_date,
            submit_datetime,
            sequence_number,
            offeror,
            subject,
            legal,
            disclosure,
            withdrawal,
        ) in rows
        if is_usable_filing_status(legal, disclosure, withdrawal)
    ]
    return tuple(sorted(filings, key=_filing_order))


def build_tender_offer_exit_values(
    sqlite_path: Path,
    *,
    provider: TenderOfferDocumentSource,
    asof: date,
) -> tuple[tuple[TenderOfferExitValue, ...], TenderOfferExitSummary]:
    """Resolve every delisting JPX attributes to a tender offer that the index can price."""
    connection = connect_current(sqlite_path)
    if connection is None:
        raise TenderOfferError(f"market store is not readable: {sqlite_path}")
    try:
        ticker_by_code = edinet_code_to_ticker(connection)
        filings = read_tender_offer_filings(connection, through=asof)
    finally:
        connection.close()
    delistings = [row for row in read_jpx_delistings(sqlite_path) if row.delisted_on <= asof]
    priceable = [row for row in delistings if TENDER_OFFER_DELISTING_MARKER in row.reason]
    by_target = _filings_by_target_ticker(filings, ticker_by_code)

    exits: list[TenderOfferExitValue] = []
    reasons: dict[str, int] = {}
    downloads = 0
    for delisting in priceable:
        case, reason = _select_case(by_target.get(delisting.ticker, ()), delisting=delisting)
        if case is None:
            reasons[reason] = reasons.get(reason, 0) + 1
            continue
        try:
            value, used_downloads = _realize_case(case, delisting=delisting, provider=provider)
        except TenderOfferError:
            reasons["document_unreadable"] = reasons.get("document_unreadable", 0) + 1
            continue
        downloads += used_downloads
        if value is None:
            reasons["price_or_outcome_unreadable"] = (
                reasons.get("price_or_outcome_unreadable", 0) + 1
            )
            continue
        exits.append(value)
    return tuple(sorted(exits, key=lambda value: (value.ticker, value.delisted_on))), (
        TenderOfferExitSummary(
            delisting_count=len(delistings),
            tender_offer_delisting_count=len(priceable),
            resolved_count=len(exits),
            downloaded_document_count=downloads,
            rejection_reason_counts=dict(sorted(reasons.items())),
        )
    )


def store_tender_offer_exit_values(
    sqlite_path: Path, values: Sequence[TenderOfferExitValue]
) -> int:
    """Replace the derived table wholesale: it is a function of the two sources.

    A derivation that established nothing does not clear a table that already holds
    values. It reads the same as a real emptying, but the way it actually happens is a
    run against a store whose EDINET identity columns are not filled in yet, and the
    published copy cannot restore what it erases — the merge treats this table as owned
    by whichever store derived it last.
    """
    connection = open_connection(sqlite_path)
    try:
        if not values:
            stored = int(
                connection.execute("SELECT COUNT(*) FROM tender_offer_exit_values").fetchone()[0]
                or 0
            )
            if stored:
                raise TenderOfferError(
                    f"refusing to replace {stored} exit values with an empty derivation; "
                    "run `screening backfill-edinet-identity` and retry"
                )
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
    by_offeror: dict[str, list[TenderOfferFiling]] = {}
    for filing in relevant:
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
    if not cases:
        return None, "withdrawn" if withdrawn and not missing_result else "incomplete_case"
    return cases[0], ""


def _realize_case(
    case: _Case, *, delisting: DelistingRecord, provider: TenderOfferDocumentSource
) -> tuple[TenderOfferExitValue | None, int]:
    """Read the final price state and the reported outcome out of the filings."""
    downloads = 0
    outcome: bool | None = None
    result_doc_id = ""
    # A correction supersedes the result it corrects, and a correction that leaves the
    # outcome alone omits the block, so the newest document that states an outcome at
    # all is the one that holds.
    for filing in (case.result_document, *case.result_corrections)[::-1]:
        blocks = read_document_blocks(provider.download_csv_zip(filing.doc_id))
        downloads += 1
        outcome = read_tender_offer_outcome(blocks)
        if outcome is not None:
            result_doc_id = filing.doc_id
            break
    if outcome is not True:
        return None, downloads

    priced: list[tuple[float, TenderOfferFiling]] = []
    for chain in case.offers:
        # A correction restates only what it changes, so the newest document in the
        # chain that prints a price at all carries that offer's final price, and the
        # newest that reports the funding carries the consideration it was paid in.
        cash_only: bool | None = None
        priced_filing: tuple[float, TenderOfferFiling] | None = None
        for filing in chain[::-1]:
            blocks = read_document_blocks(provider.download_csv_zip(filing.doc_id))
            downloads += 1
            if cash_only is None:
                cash_only = pays_in_cash_only(blocks)
            if priced_filing is None:
                price = parse_ordinary_share_offer_price(blocks)
                if price is not None:
                    priced_filing = (price, filing)
            if priced_filing is not None and cash_only is not None:
                break
        if priced_filing is None or cash_only is not True:
            # An offer this offeror ran that cannot be read is not an offer that did not
            # happen. Dropping it would let a second tier — the one whose funding names
            # a non-cash leg, or whose price the table does not state — disappear, and
            # the remaining tier would then look like the single price of the case.
            return None, downloads
        priced.append(priced_filing)
    if len({price for price, _ in priced}) != 1:
        return None, downloads
    price, filing = priced[0]
    return (
        TenderOfferExitValue(
            ticker=delisting.ticker,
            delisted_on=delisting.delisted_on,
            offer_price_yen=price,
            offer_doc_id=filing.doc_id,
            result_doc_id=result_doc_id,
            filed_on=filing.doc_date,
        ),
        downloads,
    )


def _registration_chains(
    filings: Sequence[TenderOfferFiling],
) -> tuple[tuple[TenderOfferFiling, ...], ...]:
    """Split one offeror's registration filings into one chain per offer.

    A correction belongs to the most recent registration that precedes it, which is the
    filing order EDINET publishes them in.
    """
    chains: list[list[TenderOfferFiling]] = []
    for filing in filings:
        if filing.doc_type_code == TENDER_OFFER_REGISTRATION_DOC_TYPE:
            chains.append([filing])
        elif filing.doc_type_code == TENDER_OFFER_REGISTRATION_CORRECTION_DOC_TYPE and chains:
            chains[-1].append(filing)
    return tuple(tuple(chain) for chain in chains)


def _filing_order(filing: TenderOfferFiling) -> tuple[str, str, int]:
    return filing.doc_date.isoformat(), filing.submit_datetime, filing.sequence_number


def _block(blocks: Mapping[str, str], suffix: str) -> str:
    for element, value in blocks.items():
        if element.endswith(suffix):
            return value
    return ""


__all__ = (
    "TENDER_OFFER_DELISTING_MARKER",
    "TenderOfferError",
    "TenderOfferExitSummary",
    "TenderOfferExitValue",
    "TenderOfferFiling",
    "build_tender_offer_exit_values",
    "parse_ordinary_share_offer_price",
    "pays_in_cash_only",
    "read_document_blocks",
    "read_tender_offer_exit_values",
    "read_tender_offer_filings",
    "read_tender_offer_outcome",
    "store_tender_offer_exit_values",
)
