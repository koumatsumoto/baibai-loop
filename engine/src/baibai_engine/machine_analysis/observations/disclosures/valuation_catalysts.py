"""TSE capital-policy workbook facts and EDINET identities for tender-offer readers."""

from __future__ import annotations

import re
import sqlite3
from calendar import monthrange
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from typing import Literal

import openpyxl

from baibai_engine.primitives.stocks import normalize_ticker
from baibai_engine.market.edinet_decode import parse_sec_code
from baibai_engine.machine_analysis.observations.financials.edinet_models import EDINETProviderError
from baibai_engine.storage.observations.schema import open_connection

TSE_CAPITAL_POLICY_URL = "https://www.jpx.co.jp/equities/follow-up/jr4eth0000004vj2-att/list.xlsx"

# EDINET tender-offer document type codes used by the calibration exit reader.
TENDER_OFFER_REGISTRATION_DOC_TYPE = "240"
TENDER_OFFER_REGISTRATION_CORRECTION_DOC_TYPE = "250"
TENDER_OFFER_WITHDRAWAL_DOC_TYPE = "260"
TENDER_OFFER_RESULT_DOC_TYPE = "270"
TENDER_OFFER_RESULT_CORRECTION_DOC_TYPE = "280"

_MONTH_SHEET_RE = re.compile(r"(?:【過去分】)?開示企業一覧（(\d{4})年(\d{1,2})月末時点）")

type CapitalPolicyStatus = Literal["disclosed", "considering"]


class ValuationCatalystError(RuntimeError):
    """A primary-source payload cannot be converted without guessing."""


@dataclass(frozen=True, slots=True, kw_only=True)
class TSECapitalPolicyRow:
    snapshot_month_end: date
    ticker: str
    status: CapitalPolicyStatus
    status_change: str | None
    updated_on: date | None
    contact_requested: bool
    # First disclosed month observed in the selected workbook, not the exact day.
    first_disclosed_month_end: date | None
    first_disclosure_left_censored: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class TSECapitalPolicyRefreshSummary:
    sheet_count: int
    row_count: int
    disclosed_count: int
    considering_count: int


def parse_tse_capital_policy_workbook(content: bytes) -> tuple[TSECapitalPolicyRow, ...]:
    """Expand every monthly sheet without inventing an exact first disclosure day.

    Each sheet is one snapshot month within this workbook. This does not prove that
    earlier months are all present. The earliest included sheet cannot say whether a
    company already on it disclosed that month or earlier, so those rows are marked
    left-censored instead of assigning an exact first-disclosure day.
    """
    try:
        workbook = openpyxl.load_workbook(BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:
        raise ValuationCatalystError("failed to parse TSE capital-policy workbook") from exc

    raw_rows: list[tuple[date, str, CapitalPolicyStatus, str | None, date | None, bool]] = []
    sheet_months: list[date] = []
    try:
        for worksheet in workbook.worksheets:
            match = _MONTH_SHEET_RE.search(worksheet.title.strip())
            if match is None:
                continue
            month_end = _calendar_month_end(int(match.group(1)), int(match.group(2)))
            sheet_months.append(month_end)
            rows = list(worksheet.iter_rows(values_only=True))
            columns = _resolve_tse_columns(rows, sheet=worksheet.title)
            for values in rows[columns.first_data_row :]:
                ticker_raw = _cell(values, columns.ticker)
                status_raw = _text(_cell(values, columns.status))
                if ticker_raw in (None, "") and not status_raw:
                    continue
                raw_rows.append(
                    (
                        month_end,
                        _ticker_from_excel(ticker_raw),
                        _capital_policy_status(status_raw),
                        _text(_cell(values, columns.status_change)) or None,
                        _excel_date(_cell(values, columns.updated_on)),
                        bool(_text(_cell(values, columns.contact_requested))),
                    )
                )
    finally:
        workbook.close()
    if not sheet_months or not raw_rows:
        raise ValuationCatalystError("TSE capital-policy workbook has no monthly rows")
    if len(sheet_months) != len(set(sheet_months)):
        raise ValuationCatalystError("TSE capital-policy workbook repeats a snapshot month")

    earliest_sheet = min(sheet_months)
    first_disclosed: dict[str, date] = {}
    for month_end, ticker, status, *_ in sorted(raw_rows):
        if status == "disclosed" and ticker not in first_disclosed:
            first_disclosed[ticker] = month_end

    return tuple(
        TSECapitalPolicyRow(
            snapshot_month_end=month_end,
            ticker=ticker,
            status=status,
            status_change=status_change,
            updated_on=updated_on,
            contact_requested=contact_requested,
            first_disclosed_month_end=(
                first_disclosed.get(ticker)
                if first_disclosed.get(ticker, date.max) <= month_end
                else None
            ),
            first_disclosure_left_censored=first_disclosed.get(ticker) == earliest_sheet,
        )
        for month_end, ticker, status, status_change, updated_on, contact_requested in sorted(
            raw_rows
        )
    )


def store_tse_capital_policy_rows(sqlite_path: Path, rows: Sequence[TSECapitalPolicyRow]) -> int:
    """Atomically replace the full view with one successfully decoded workbook."""
    if not rows:
        raise ValuationCatalystError("refusing to replace TSE capital-policy facts with zero rows")
    connection = open_connection(sqlite_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM tse_capital_policy_snapshots")
        connection.executemany(
            "INSERT INTO tse_capital_policy_snapshots("
            "snapshot_month_end, ticker, status, status_change, updated_on, "
            "contact_requested, first_disclosed_month_end, first_disclosure_left_censored"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    row.snapshot_month_end.isoformat(),
                    row.ticker,
                    row.status,
                    row.status_change,
                    _date_iso(row.updated_on),
                    int(row.contact_requested),
                    _date_iso(row.first_disclosed_month_end),
                    int(row.first_disclosure_left_censored),
                )
                for row in rows
            ),
        )
        connection.commit()
        return len(rows)
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def edinet_code_to_ticker(connection: sqlite3.Connection, *, through: date) -> dict[str, str]:
    """Resolve EDINET codes through the (edinetCode, secCode) pairs the list observed.

    Only filing dates through the caller's cutoff participate. Later discoveries
    or conflicts must not change an earlier lookup. A code seen against more than
    one ticker in that window is dropped rather than picked between.
    """
    observed: dict[str, set[str]] = {}
    for edinet_code, sec_code in connection.execute(
        "SELECT DISTINCT edinet_code, sec_code FROM edinet_documents "
        "WHERE edinet_code IS NOT NULL AND sec_code IS NOT NULL AND doc_date <= ?",
        (through.isoformat(),),
    ):
        try:
            ticker = parse_sec_code(sec_code)
        except EDINETProviderError:
            continue
        observed.setdefault(str(edinet_code), set()).add(ticker)
    return {code: next(iter(values)) for code, values in observed.items() if len(values) == 1}


@dataclass(frozen=True, slots=True, kw_only=True)
class _TSEColumns:
    """Where each field sits on one monthly sheet, resolved from its own header."""

    first_data_row: int
    ticker: int
    status: int
    status_change: int
    updated_on: int
    contact_requested: int


# The header spans two rows: a group label with merged cells above, and the label of
# each column inside the group below. Both are needed because a group label alone is
# ambiguous — the contact block prints three columns under one heading, and the column
# holding the disclosure text was inserted before that block in later sheets, so a
# fixed index reads free-form disclosure text as a contact request.
_TSE_TICKER_HEADER = "証券コード"
_TSE_STATUS_GROUP = "開示状況"
_TSE_STATUS_HEADER = "要請に基づく開示状況"
_TSE_STATUS_CHANGE_HEADER = "前月からの開示状況の変更"
_TSE_UPDATED_ON_HEADER = "開示内容のアップデート日"
_TSE_CONTACT_GROUP_PREFIX = "機関投資家からのより活発なコンタクトを希望"
_TSE_CONTACT_HEADER = "申請状況"


def _resolve_tse_columns(rows: Sequence[Sequence[object]], *, sheet: str) -> _TSEColumns:
    header_index = next(
        (
            index
            for index, values in enumerate(rows[:20])
            for labels in ({_text(value) for value in values if value is not None},)
            if _TSE_TICKER_HEADER in labels and _TSE_STATUS_GROUP in labels
        ),
        None,
    )
    if header_index is None or header_index + 1 >= len(rows):
        raise ValuationCatalystError(f"TSE capital-policy sheet has no typed header: {sheet}")
    group = _forward_filled_labels(rows[header_index])
    detail = [_text(value) for value in rows[header_index + 1]]
    width = max(len(group), len(detail))

    def find(predicate: Callable[[int], bool], label: str) -> int:
        column = next((index for index in range(width) if predicate(index)), None)
        if column is None:
            raise ValuationCatalystError(f"TSE capital-policy sheet has no {label} column: {sheet}")
        return column

    def at(labels: Sequence[str], index: int) -> str:
        return labels[index] if index < len(labels) else ""

    return _TSEColumns(
        first_data_row=header_index + 2,
        ticker=find(lambda index: at(group, index) == _TSE_TICKER_HEADER, _TSE_TICKER_HEADER),
        status=find(lambda index: at(detail, index) == _TSE_STATUS_HEADER, _TSE_STATUS_HEADER),
        status_change=find(
            lambda index: at(detail, index) == _TSE_STATUS_CHANGE_HEADER,
            _TSE_STATUS_CHANGE_HEADER,
        ),
        updated_on=find(
            lambda index: at(group, index) == _TSE_UPDATED_ON_HEADER, _TSE_UPDATED_ON_HEADER
        ),
        contact_requested=find(
            lambda index: (
                at(group, index).startswith(_TSE_CONTACT_GROUP_PREFIX)
                and at(detail, index) == _TSE_CONTACT_HEADER
            ),
            _TSE_CONTACT_GROUP_PREFIX,
        ),
    )


def _forward_filled_labels(values: Sequence[object]) -> list[str]:
    """Spread a merged group label across the columns it covers."""
    labels: list[str] = []
    current = ""
    for value in values:
        text = _text(value)
        if text:
            current = text
        labels.append(current)
    return labels


def _capital_policy_status(value: str) -> CapitalPolicyStatus:
    if value == "開示済":
        return "disclosed"
    if value == "検討中":
        return "considering"
    raise ValuationCatalystError(f"unknown TSE capital-policy status: {value!r}")


def _ticker_from_excel(value: object) -> str:
    if isinstance(value, bool):
        raise ValuationCatalystError(f"invalid TSE ticker: {value!r}")
    if isinstance(value, int):
        raw = str(value)
    elif isinstance(value, float) and value.is_integer():
        raw = str(int(value))
    else:
        raw = _text(value)
    try:
        return normalize_ticker(raw)
    except ValueError as exc:
        raise ValuationCatalystError(f"invalid TSE ticker: {value!r}") from exc


def _excel_date(value: object) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = _text(value)
    try:
        return date.fromisoformat(
            raw.replace("/", "-").replace("年", "-").replace("月", "-").removesuffix("日")
        )
    except ValueError as exc:
        raise ValuationCatalystError(f"invalid TSE update date: {raw!r}") from exc


def _calendar_month_end(year: int, month: int) -> date:
    try:
        return date(year, month, monthrange(year, month)[1])
    except ValueError as exc:
        raise ValuationCatalystError(f"invalid TSE snapshot month: {year}-{month}") from exc


def _cell(values: Sequence[object], index: int) -> object | None:
    return values[index] if index < len(values) else None


def _text(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


def _date_iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


__all__ = (
    "TENDER_OFFER_REGISTRATION_CORRECTION_DOC_TYPE",
    "TENDER_OFFER_REGISTRATION_DOC_TYPE",
    "TENDER_OFFER_RESULT_CORRECTION_DOC_TYPE",
    "TENDER_OFFER_RESULT_DOC_TYPE",
    "TENDER_OFFER_WITHDRAWAL_DOC_TYPE",
    "CapitalPolicyStatus",
    "TSECapitalPolicyRefreshSummary",
    "TSECapitalPolicyRow",
    "ValuationCatalystError",
    "edinet_code_to_ticker",
    "parse_tse_capital_policy_workbook",
    "store_tse_capital_policy_rows",
)
