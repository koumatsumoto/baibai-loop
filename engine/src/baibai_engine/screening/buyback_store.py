"""Fetch and store the monthly buyback authorization reports.

Kept apart from the EDINET metric extraction: this reads a different form (220 / 230),
writes a different table, and must not enter that extraction's revision manifest — an
edit here would otherwise discard every stored EDINET metric row and force a full
re-download of several thousand filings.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

from baibai_engine.market.sqlite import connect_current, open_connection
from baibai_engine.market.ticker import normalize_ticker

from .buyback_report import (
    BuybackReport,
    BuybackReportError,
    inconsistent_buyback_fields,
    parse_buyback_report,
)
from .providers.edinet import EDINETProviderError, EDINETRateLimitError

# 自己株券買付状況報告書 and its correction. A correction supersedes the original for the
# same reporting month, and both carry the whole table, so the newest filing for a month
# is the one to keep.
BUYBACK_FORM_DOC_TYPES = ("220", "230")
# How often progress is written down. Small enough that a rate limit or an
# interruption costs a few filings rather than the whole pass.
_COMMIT_EVERY = 50


class BuybackFilingSource(Protocol):
    """The one provider call this path makes."""

    def download_csv_zip(self, doc_id: str) -> bytes: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class BuybackRefreshSummary:
    considered: int
    stored: int
    unreadable: int
    # Read but unusable: no reporting month, or one that ends after the filing date.
    without_usable_month: int
    # EDINET asked the run to back off before it reached the end of the list. What was
    # stored is kept; the next run picks up from there because stored filings are skipped.
    rate_limited: bool = False


def _candidate_filings(
    connection: sqlite3.Connection, *, since: date
) -> Iterator[tuple[str, str, str, str, int]]:
    """Candidate metadata for readable buyback filings from `since` onwards.

    EDINET can publish an original and correction on the same day.  The ordering therefore
    uses the form kind and list sequence as well as the date: originals are applied first,
    then corrections, and a later list sequence wins within the same form kind.
    """
    placeholders = ", ".join("?" for _ in BUYBACK_FORM_DOC_TYPES)
    rows = connection.execute(
        "SELECT d.doc_id, d.sec_code, d.doc_date, d.doc_type_code, "  # nosec B608
        "d.sequence_number, d.parent_doc_id FROM edinet_documents d "
        f"WHERE d.doc_type_code IN ({placeholders}) AND d.csv_flag = 1 "
        "AND d.sec_code IS NOT NULL AND d.doc_date >= ? "
        "ORDER BY d.doc_date, CASE d.doc_type_code WHEN '220' THEN 0 ELSE 1 END, "
        "d.sequence_number, d.doc_id",
        (*BUYBACK_FORM_DOC_TYPES, since.isoformat()),
    ).fetchall()
    # `parent_doc_id` has no index in the shared market store.  Selecting terminal
    # revisions with a correlated SQL scan is quadratic over the whole document table;
    # this bounded Form-220/230 list is small enough to resolve linearly in memory.
    newest_correction_by_parent: dict[str, tuple[str, int, str]] = {}
    superseded_doc_ids: set[str] = set()
    for doc_id, _, doc_date, doc_type_code, sequence_number, parent_doc_id in rows:
        if str(doc_type_code) != "230" or parent_doc_id in (None, ""):
            continue
        parent = str(parent_doc_id)
        superseded_doc_ids.add(parent)
        key = str(doc_date), int(sequence_number), str(doc_id)
        if key > newest_correction_by_parent.get(parent, ("", -1, "")):
            newest_correction_by_parent[parent] = key
    selected_corrections = {key[2] for key in newest_correction_by_parent.values()}

    for doc_id, sec_code, doc_date, doc_type_code, sequence_number, parent_doc_id in rows:
        doc_id = str(doc_id)
        if doc_id in superseded_doc_ids:
            continue
        if (
            str(doc_type_code) == "230"
            and parent_doc_id not in (None, "")
            and doc_id not in selected_corrections
        ):
            continue
        try:
            ticker = normalize_ticker(str(sec_code)[:4])
        except ValueError:
            continue
        yield doc_id, ticker, str(doc_date), str(doc_type_code), int(sequence_number)


def _revision_key(
    *, filed_on: str, doc_type_code: str, sequence_number: int, doc_id: str
) -> tuple[str, int, int, str]:
    """A stable EDINET revision order independent of refresh and lexical doc-id order."""

    return filed_on, int(doc_type_code == "230"), sequence_number, doc_id


def _store(
    connection: sqlite3.Connection,
    *,
    ticker: str,
    doc_id: str,
    filed_on: str,
    doc_type_code: str,
    sequence_number: int,
    report: BuybackReport,
) -> bool:
    assert report.report_month_end is not None
    current = connection.execute(
        "SELECT doc_id, filed_on FROM edinet_buyback_reports "
        "WHERE ticker = ? AND report_month_end = ?",
        (ticker, report.report_month_end.isoformat()),
    ).fetchone()
    if current is not None:
        current_doc_id, current_filed_on = str(current[0]), str(current[1])
        if filed_on < current_filed_on:
            return False
        if filed_on == current_filed_on:
            current_metadata = connection.execute(
                "SELECT doc_type_code, sequence_number FROM edinet_documents "
                "WHERE doc_id = ? AND doc_date = ?",
                (current_doc_id, current_filed_on),
            ).fetchone()
            # A same-day row without its source-list identity cannot be ordered safely.
            # Preserve it instead of allowing refresh order to choose the winner.
            if current_metadata is None:
                return False
            candidate_key = _revision_key(
                filed_on=filed_on,
                doc_type_code=doc_type_code,
                sequence_number=sequence_number,
                doc_id=doc_id,
            )
            current_key = _revision_key(
                filed_on=current_filed_on,
                doc_type_code=str(current_metadata[0]),
                sequence_number=int(current_metadata[1]),
                doc_id=current_doc_id,
            )
            if candidate_key <= current_key:
                return False
    connection.execute(
        """
        INSERT INTO edinet_buyback_reports (
            ticker, report_month_end, doc_id, filed_on, window_start, window_end,
            resolved_shares, resolved_amount_yen, cumulative_shares, cumulative_amount_yen,
            month_shares, month_amount_yen, issued_shares, treasury_shares
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, report_month_end) DO UPDATE SET
            doc_id = excluded.doc_id,
            filed_on = excluded.filed_on,
            window_start = excluded.window_start,
            window_end = excluded.window_end,
            resolved_shares = excluded.resolved_shares,
            resolved_amount_yen = excluded.resolved_amount_yen,
            cumulative_shares = excluded.cumulative_shares,
            cumulative_amount_yen = excluded.cumulative_amount_yen,
            month_shares = excluded.month_shares,
            month_amount_yen = excluded.month_amount_yen,
            issued_shares = excluded.issued_shares,
            treasury_shares = excluded.treasury_shares
        WHERE excluded.filed_on >= edinet_buyback_reports.filed_on
        """,
        (
            ticker,
            report.report_month_end.isoformat(),
            doc_id,
            filed_on,
            None if report.window_start is None else report.window_start.isoformat(),
            None if report.window_end is None else report.window_end.isoformat(),
            report.resolved_shares,
            report.resolved_amount_yen,
            report.cumulative_shares,
            report.cumulative_amount_yen,
            report.month_shares,
            report.month_amount_yen,
            report.issued_shares,
            report.treasury_shares,
        ),
    )
    return True


def refresh_buyback_reports(
    sqlite_path: Path,
    *,
    provider: BuybackFilingSource,
    since: date,
    limit: int | None = None,
) -> BuybackRefreshSummary:
    """Read every buyback filing from `since` that the store does not already hold.

    A filing that cannot be read is counted and skipped rather than failing the run: the
    form is a disclosure document, and one filer's unusual layout — or one document EDINET
    will not serve — must not stop the rest. A filing whose reporting month cannot be read
    is skipped too, because without it the row has no identity.

    Rate limiting ends the pass instead of marking filings unreadable, because the API
    saying "later" is not the filings being bad. Progress is committed as it goes, so a
    backfill of several thousand filings advances every run rather than restarting: a
    filing already stored is never fetched again.
    """
    connection = open_connection(sqlite_path)
    try:
        already = {
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT ticker, doc_id FROM edinet_buyback_reports"
            ).fetchall()
        }
        considered = stored = unreadable = without_usable_month = 0
        rate_limited = False
        for doc_id, ticker, filed_on, doc_type_code, sequence_number in _candidate_filings(
            connection, since=since
        ):
            if (ticker, doc_id) in already:
                continue
            considered += 1
            try:
                report = parse_buyback_report(provider.download_csv_zip(doc_id))
            except EDINETRateLimitError:
                rate_limited = True
                break
            except (BuybackReportError, EDINETProviderError, OSError, ValueError):
                unreadable += 1
                continue
            if not _has_usable_month(report, filed_on=filed_on):
                without_usable_month += 1
                continue
            changed = _store(
                connection,
                ticker=ticker,
                doc_id=doc_id,
                filed_on=filed_on,
                doc_type_code=doc_type_code,
                sequence_number=sequence_number,
                report=report,
            )
            stored += int(changed)
            if changed and stored % _COMMIT_EVERY == 0:
                connection.commit()
            if limit is not None and stored >= limit:
                break
        connection.commit()
    finally:
        connection.close()
    return BuybackRefreshSummary(
        considered=considered,
        stored=stored,
        unreadable=unreadable,
        without_usable_month=without_usable_month,
        rate_limited=rate_limited,
    )


def _has_usable_month(report: BuybackReport, *, filed_on: str) -> bool:
    """Whether the reporting month can identify the row.

    The month is half the primary key, so a filing without one has nowhere to go. A month
    that ends after the filing date has somewhere to go and is worse for it: the form
    reports a month that has closed, so a later one means the read latched onto a date
    from elsewhere in the document — the authorization window, or a neighbouring row —
    and storing it would file a stale reading as the newest one the ticker has.
    """
    if report.report_month_end is None:
        return False
    return report.report_month_end.isoformat() <= filed_on


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredBuybackReport:
    report_month_end: date
    window_start: date | None
    window_end: date | None
    resolved_shares: int | None
    cumulative_shares: int | None
    month_shares: int | None
    issued_shares: int | None
    # 決議は「取得し得る株式の総数」と「取得価額の総額」の 2 本を上限に持ち、先に尽きた方で
    # 取得が終わる。株数側だけでは残枠を答えられないので金額側も持つ。
    resolved_amount_yen: int | None = None
    cumulative_amount_yen: int | None = None
    doc_id: str | None = None
    filed_on: date | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class BuybackReportRead:
    """読めた報告と、読み取りで field を落とした行の数。

    落とした事実を数に残さないと、「様式が読めなかった」と「そもそも提出が無い」が
    判断面では同じ `null` になり、annotation が消えたことを誰も観測できない。数は
    run の fallback 行へ出す。
    """

    reports: dict[str, tuple[StoredBuybackReport, ...]]
    inconsistent_rows: int = 0
    inconsistent_tickers: int = 0


def read_buyback_reports(
    sqlite_path: Path, *, tickers: Sequence[str], asof: date, months: int
) -> BuybackReportRead:
    """The newest `months` reports per ticker whose month end is at or before `asof`.

    Both reporting month and filing date are bounded by `asof`.  The report describes a
    closed month but is published afterwards; bounding only the month would leak that
    later publication into a historical replay.
    """
    if not tickers or months <= 0:
        return BuybackReportRead(reports={})
    connection = connect_current(sqlite_path)
    if connection is None:
        return BuybackReportRead(reports={})
    try:
        placeholders = ", ".join("?" for _ in tickers)
        rows = connection.execute(
            "SELECT ticker, report_month_end, window_start, window_end, resolved_shares, "  # nosec B608
            "cumulative_shares, month_shares, issued_shares, doc_id, filed_on, "
            "resolved_amount_yen, cumulative_amount_yen, treasury_shares "
            f"FROM edinet_buyback_reports WHERE ticker IN ({placeholders}) "
            "AND report_month_end <= ? AND filed_on <= ? "
            "ORDER BY ticker, report_month_end DESC, filed_on DESC",
            (*tickers, asof.isoformat(), asof.isoformat()),
        ).fetchall()
    except sqlite3.OperationalError:
        return BuybackReportRead(reports={})
    finally:
        connection.close()

    grouped: dict[str, list[StoredBuybackReport]] = {}
    inconsistent_rows = 0
    inconsistent_tickers: set[str] = set()
    for row in rows:
        bucket = grouped.setdefault(str(row[0]), [])
        if len(bucket) >= months:
            continue
        window_start = None if row[2] is None else date.fromisoformat(str(row[2]))
        window_end = None if row[3] is None else date.fromisoformat(str(row[3]))
        resolved_shares = None if row[4] is None else int(row[4])
        cumulative_shares = None if row[5] is None else int(row[5])
        month_shares = None if row[6] is None else int(row[6])
        issued_shares = None if row[7] is None else int(row[7])
        resolved_amount_yen = None if row[10] is None else int(row[10])
        cumulative_amount_yen = None if row[11] is None else int(row[11])
        # 規則が書かれる前に保存された行は取り込みをやり直さないと直らないので、読み取り側
        # でも同じ検査を通す。取り込み時と同じ関数なので規則は 1 か所にとどまる。
        dropped = inconsistent_buyback_fields(
            window_start=window_start,
            window_end=window_end,
            resolved_shares=resolved_shares,
            resolved_amount_yen=resolved_amount_yen,
            cumulative_shares=cumulative_shares,
            cumulative_amount_yen=cumulative_amount_yen,
            month_shares=month_shares,
            issued_shares=issued_shares,
            treasury_shares=None if row[12] is None else int(row[12]),
        )
        if dropped:
            inconsistent_rows += 1
            inconsistent_tickers.add(str(row[0]))
        bucket.append(
            StoredBuybackReport(
                report_month_end=date.fromisoformat(str(row[1])),
                window_start=None if "window_start" in dropped else window_start,
                window_end=None if "window_end" in dropped else window_end,
                resolved_shares=None if "resolved_shares" in dropped else resolved_shares,
                cumulative_shares=None if "cumulative_shares" in dropped else cumulative_shares,
                month_shares=None if "month_shares" in dropped else month_shares,
                issued_shares=None if "issued_shares" in dropped else issued_shares,
                resolved_amount_yen=(
                    None if "resolved_amount_yen" in dropped else resolved_amount_yen
                ),
                cumulative_amount_yen=(
                    None if "cumulative_amount_yen" in dropped else cumulative_amount_yen
                ),
                doc_id=None if row[8] is None else str(row[8]),
                filed_on=None if row[9] is None else date.fromisoformat(str(row[9])),
            )
        )
    return BuybackReportRead(
        reports={ticker: tuple(reports) for ticker, reports in grouped.items()},
        inconsistent_rows=inconsistent_rows,
        inconsistent_tickers=len(inconsistent_tickers),
    )
