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

from .buyback_report import BuybackReport, BuybackReportError, parse_buyback_report
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
) -> Iterator[tuple[str, str, str]]:
    """(doc_id, ticker, filed_on) for readable buyback filings from `since` onwards.

    Ordered oldest first so a correction filed later overwrites the original it fixes.
    """
    placeholders = ", ".join("?" for _ in BUYBACK_FORM_DOC_TYPES)
    rows = connection.execute(
        "SELECT doc_id, sec_code, doc_date FROM edinet_documents "  # nosec B608
        f"WHERE doc_type_code IN ({placeholders}) AND csv_flag = 1 "
        "AND sec_code IS NOT NULL AND doc_date >= ? "
        "ORDER BY doc_date, doc_id",
        (*BUYBACK_FORM_DOC_TYPES, since.isoformat()),
    ).fetchall()
    for doc_id, sec_code, doc_date in rows:
        try:
            ticker = normalize_ticker(str(sec_code)[:4])
        except ValueError:
            continue
        yield str(doc_id), ticker, str(doc_date)


def _store(
    connection: sqlite3.Connection,
    *,
    ticker: str,
    doc_id: str,
    filed_on: str,
    report: BuybackReport,
) -> None:
    assert report.report_month_end is not None
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
        for doc_id, ticker, filed_on in _candidate_filings(connection, since=since):
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
            _store(connection, ticker=ticker, doc_id=doc_id, filed_on=filed_on, report=report)
            stored += 1
            if stored % _COMMIT_EVERY == 0:
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


def read_buyback_reports(
    sqlite_path: Path, *, tickers: Sequence[str], asof: date, months: int
) -> dict[str, tuple[StoredBuybackReport, ...]]:
    """The newest `months` reports per ticker whose month end is at or before `asof`.

    Bounded by `asof` so a historical replay never sees a filing that did not exist yet.
    """
    if not tickers or months <= 0:
        return {}
    connection = connect_current(sqlite_path)
    if connection is None:
        return {}
    try:
        placeholders = ", ".join("?" for _ in tickers)
        rows = connection.execute(
            "SELECT ticker, report_month_end, window_start, window_end, resolved_shares, "  # nosec B608
            "cumulative_shares, month_shares, issued_shares "
            f"FROM edinet_buyback_reports WHERE ticker IN ({placeholders}) "
            "AND report_month_end <= ? ORDER BY ticker, report_month_end DESC",
            (*tickers, asof.isoformat()),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        connection.close()

    grouped: dict[str, list[StoredBuybackReport]] = {}
    for row in rows:
        bucket = grouped.setdefault(str(row[0]), [])
        if len(bucket) >= months:
            continue
        bucket.append(
            StoredBuybackReport(
                report_month_end=date.fromisoformat(str(row[1])),
                window_start=None if row[2] is None else date.fromisoformat(str(row[2])),
                window_end=None if row[3] is None else date.fromisoformat(str(row[3])),
                resolved_shares=None if row[4] is None else int(row[4]),
                cumulative_shares=None if row[5] is None else int(row[5]),
                month_shares=None if row[6] is None else int(row[6]),
                issued_shares=None if row[7] is None else int(row[7]),
            )
        )
    return {ticker: tuple(reports) for ticker, reports in grouped.items()}
