from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from baibai_engine.screening.buyback_authorization import (
    ACTIVE_WINDOW_DAYS,
    OBSERVATION_WINDOW_DAYS,
    build_buyback_authorization,
    index_buyback_status_filings,
    read_buyback_status_filings,
)
from baibai_engine.screening.sqlite_cache import open_connection, store_edinet_documents

ASOF = date(2026, 8, 4)
COVERED_FROM = ASOF.replace(year=ASOF.year - 1)


def _document(
    *,
    sequence_number: int,
    doc_type_code: str,
    sec_code: str | None,
) -> dict[str, object]:
    return {
        "seqNumber": sequence_number,
        "docID": f"S{sequence_number:07d}",
        "secCode": sec_code,
        "docTypeCode": doc_type_code,
        "docDescription": "自己株券買付状況報告書",
    }


def test_a_recent_filing_reads_as_an_authorization_that_is_still_running() -> None:
    annotation = build_buyback_authorization(
        asof=ASOF,
        latest_filing_date=ASOF - timedelta(days=30),
        observed_from=COVERED_FROM,
    )

    assert annotation.status == "active"
    assert annotation.latest_filing_age_days == 30


def test_a_filing_older_than_the_monthly_cadence_reads_as_lapsed() -> None:
    annotation = build_buyback_authorization(
        asof=ASOF,
        latest_filing_date=ASOF - timedelta(days=ACTIVE_WINDOW_DAYS + 1),
        observed_from=COVERED_FROM,
    )

    assert annotation.status == "lapsed"
    assert annotation.latest_filing_age_days == ACTIVE_WINDOW_DAYS + 1


def test_no_filing_inside_a_covered_window_reads_as_none() -> None:
    annotation = build_buyback_authorization(
        asof=ASOF, latest_filing_date=None, observed_from=COVERED_FROM
    )

    assert annotation.status == "none"
    assert annotation.latest_filing_date is None


def test_a_window_shorter_than_the_observation_requirement_reads_as_unknown() -> None:
    short_window_start = ASOF - timedelta(days=OBSERVATION_WINDOW_DAYS - 1)

    annotation = build_buyback_authorization(
        asof=ASOF, latest_filing_date=None, observed_from=short_window_start
    )

    assert annotation.status == "unknown"
    assert annotation.observed_from == short_window_start


def test_an_unobserved_store_never_claims_that_no_authorization_exists() -> None:
    annotation = build_buyback_authorization(asof=ASOF, latest_filing_date=None, observed_from=None)

    assert annotation.status == "unknown"


def test_a_filing_dated_after_the_asof_is_not_treated_as_current() -> None:
    annotation = build_buyback_authorization(
        asof=ASOF,
        latest_filing_date=ASOF + timedelta(days=1),
        observed_from=COVERED_FROM,
    )

    assert annotation.status == "none"


def test_the_index_keeps_the_newest_filing_and_drops_rows_without_a_security_code() -> None:
    latest = index_buyback_status_filings(
        [
            {"sec_code": "60880", "doc_date": "2026-06-03"},
            {"sec_code": "60880", "doc_date": "2026-07-01"},
            {"sec_code": None, "doc_date": "2026-07-01"},
            {"sec_code": "64580", "doc_date": "not-a-date"},
        ]
    )

    assert latest == {"6088": date(2026, 7, 1)}


def test_the_reader_returns_the_observed_window_from_the_filings_themselves(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    connection = open_connection(sqlite_path)
    connection.close()
    store_edinet_documents(
        sqlite_path,
        date(2026, 7, 1),
        [
            _document(sequence_number=1, doc_type_code="220", sec_code="60880"),
            _document(sequence_number=2, doc_type_code="120", sec_code="48870"),
        ],
    )
    store_edinet_documents(
        sqlite_path,
        date(2026, 8, 3),
        [_document(sequence_number=1, doc_type_code="230", sec_code="60880")],
    )

    read = read_buyback_status_filings(sqlite_path, through=ASOF)

    assert read is not None
    # 訂正報告も提出の事実として数える。有価証券報告書 (120) は数えない。
    assert read.latest_filing_by_ticker == {"6088": date(2026, 8, 3)}
    assert read.observed_from == date(2026, 7, 1)


def test_the_reader_excludes_filings_after_the_requested_date(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    connection = open_connection(sqlite_path)
    connection.close()
    store_edinet_documents(
        sqlite_path,
        date(2026, 7, 1),
        [_document(sequence_number=1, doc_type_code="220", sec_code="60880")],
    )
    store_edinet_documents(
        sqlite_path,
        date(2026, 8, 5),
        [_document(sequence_number=1, doc_type_code="220", sec_code="60880")],
    )

    read = read_buyback_status_filings(sqlite_path, through=ASOF)

    assert read is not None
    assert read.latest_filing_by_ticker == {"6088": date(2026, 7, 1)}


def test_the_reader_reports_an_absent_store_instead_of_an_empty_observation(
    tmp_path: Path,
) -> None:
    assert read_buyback_status_filings(tmp_path / "absent.sqlite", through=ASOF) is None
