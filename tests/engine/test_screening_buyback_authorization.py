from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from baibai_engine.screening.buyback_authorization import (
    OBSERVATION_WINDOW_DAYS,
    RECENT_FILING_WINDOW_DAYS,
    BuybackAuthorization,
    build_buyback_authorization,
    index_buyback_status_filings,
    read_buyback_status_filings,
    with_authorization_state,
)
from baibai_engine.screening.buyback_store import StoredBuybackReport
from baibai_engine.screening.rule_config import DEFAULT_RULES_PATH, load_screening_rules
from baibai_engine.screening.selection import build_selection_payload, candidate_record_from_mapping
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


def _seed_complete_document_days(sqlite_path: Path, start: date, end: date) -> None:
    """Mark every daily EDINET list in the interval final with matching row counts."""

    connection = open_connection(sqlite_path)
    try:
        cursor = start
        while cursor <= end:
            day = cursor.isoformat()
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM edinet_documents WHERE doc_date = ?", (day,)
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT OR REPLACE INTO edinet_document_lists("
                "doc_date, process_datetime, result_count, fetched_at_utc, is_final"
                ") VALUES (?, NULL, ?, '2026-08-05T00:00:00+00:00', 1)",
                (day, count),
            )
            connection.execute(
                "INSERT OR REPLACE INTO source_coverage("
                "source, coverage_key, coverage_start, coverage_end, fetched_at_utc, "
                "record_count, status, error"
                ") VALUES ('edinet_documents', ?, ?, ?, "
                "'2026-08-05T00:00:00+00:00', ?, 'ok', NULL)",
                (day, day, day, count),
            )
            cursor += timedelta(days=1)
        connection.commit()
    finally:
        connection.close()


def test_a_filing_inside_the_monthly_cadence_reads_as_a_recent_filing() -> None:
    annotation = build_buyback_authorization(
        asof=ASOF,
        latest_filing_date=ASOF - timedelta(days=30),
        observed_from=COVERED_FROM,
    )

    assert annotation.status == "recent_filing"
    assert annotation.latest_filing_age_days == 30


def test_a_filing_older_than_the_monthly_cadence_reads_as_a_stale_filing() -> None:
    annotation = build_buyback_authorization(
        asof=ASOF,
        latest_filing_date=ASOF - timedelta(days=RECENT_FILING_WINDOW_DAYS + 1),
        observed_from=COVERED_FROM,
    )

    assert annotation.status == "stale_filing"
    assert annotation.latest_filing_age_days == RECENT_FILING_WINDOW_DAYS + 1


def test_an_absent_filing_inside_a_covered_window_reads_as_no_filing() -> None:
    annotation = build_buyback_authorization(
        asof=ASOF, latest_filing_date=None, observed_from=COVERED_FROM
    )

    assert annotation.status == "no_filing"
    assert annotation.latest_filing_date is None


def test_a_window_shorter_than_the_observation_requirement_reads_as_unknown() -> None:
    short_window_start = ASOF - timedelta(days=OBSERVATION_WINDOW_DAYS - 1)

    annotation = build_buyback_authorization(
        asof=ASOF, latest_filing_date=None, observed_from=short_window_start
    )

    assert annotation.status == "unknown"
    assert annotation.observed_from == short_window_start


def test_an_observed_filing_is_kept_even_before_no_filing_coverage_matures() -> None:
    short_window_start = ASOF - timedelta(days=30)

    annotation = build_buyback_authorization(
        asof=ASOF,
        latest_filing_date=ASOF - timedelta(days=10),
        observed_from=short_window_start,
    )

    assert annotation.status == "recent_filing"
    assert annotation.latest_filing_date == ASOF - timedelta(days=10)
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

    assert annotation.status == "no_filing"


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


def test_the_reader_returns_the_observed_window_from_validated_daily_lists(
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
    _seed_complete_document_days(sqlite_path, date(2026, 7, 1), ASOF)

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


def test_a_gap_in_the_daily_lists_shortens_the_observed_window(tmp_path: Path) -> None:
    """途中の日次一覧欠落は、それ以前を no-filing coverage に数えない。"""

    sqlite_path = tmp_path / "market.sqlite"
    open_connection(sqlite_path).close()
    store_edinet_documents(
        sqlite_path,
        date(2025, 9, 3),
        [_document(sequence_number=1, doc_type_code="220", sec_code="60880")],
    )
    _seed_complete_document_days(sqlite_path, date(2026, 7, 1), ASOF)

    read = read_buyback_status_filings(sqlite_path, through=ASOF)

    assert read is not None
    # 2025-10 〜 2026-06 の日次一覧が無いので、窓は 2026-07 から。
    assert read.observed_from == date(2026, 7, 1)
    annotation = build_buyback_authorization(
        asof=ASOF, latest_filing_date=None, observed_from=read.observed_from
    )
    assert annotation.status == "unknown"


def test_a_missing_recent_daily_list_invalidates_negative_coverage(tmp_path: Path) -> None:
    """as-of 当日の一覧が無ければ、その時点で「提出なし」とは言えない。"""

    sqlite_path = tmp_path / "market.sqlite"
    open_connection(sqlite_path).close()
    _seed_complete_document_days(sqlite_path, date(2026, 6, 1), ASOF - timedelta(days=1))

    read = read_buyback_status_filings(sqlite_path, through=ASOF)

    assert read is not None
    assert read.observed_from is None


def test_monthly_form_220_sentinels_do_not_prove_universe_coverage(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    open_connection(sqlite_path).close()
    cursor = ASOF - timedelta(days=OBSERVATION_WINDOW_DAYS)
    sequence = 1
    while cursor <= ASOF:
        if cursor.day == 1:
            store_edinet_documents(
                sqlite_path,
                cursor,
                [_document(sequence_number=sequence, doc_type_code="220", sec_code="99990")],
                is_final=True,
            )
            sequence += 1
        cursor += timedelta(days=1)

    read = read_buyback_status_filings(sqlite_path, through=ASOF)

    assert read is not None
    assert read.observed_from is None


def test_count_mismatch_breaks_the_validated_observation_window(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    open_connection(sqlite_path).close()
    _seed_complete_document_days(sqlite_path, ASOF - timedelta(days=2), ASOF)
    with open_connection(sqlite_path) as connection:
        connection.execute(
            "UPDATE source_coverage SET record_count = 1 "
            "WHERE source = 'edinet_documents' AND coverage_key = ?",
            ((ASOF - timedelta(days=1)).isoformat(),),
        )
        connection.commit()

    read = read_buyback_status_filings(sqlite_path, through=ASOF)

    assert read is not None
    assert read.observed_from == ASOF


def test_partial_daily_coverage_breaks_the_validated_observation_window(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    open_connection(sqlite_path).close()
    _seed_complete_document_days(sqlite_path, ASOF - timedelta(days=2), ASOF)
    with open_connection(sqlite_path) as connection:
        connection.execute(
            "UPDATE source_coverage SET status = 'partial', error = 'truncated' "
            "WHERE source = 'edinet_documents' AND coverage_key = ?",
            ((ASOF - timedelta(days=1)).isoformat(),),
        )
        connection.commit()

    read = read_buyback_status_filings(sqlite_path, through=ASOF)

    assert read is not None
    assert read.observed_from == ASOF


def test_the_annotation_reaches_both_selection_views_that_op3_reads() -> None:
    """判断面に出ない annotation は、skill が消化を要求しても実行不能な指示になる。"""

    rules = load_screening_rules(DEFAULT_RULES_PATH)
    candidates = [
        {
            "ticker": "1111",
            "name": "name-1111",
            "sector_33": "機械",
            "market_cap_oku": 300,
            "avg_turnover_oku": 2.0,
            "listing_span_days": 1200,
            "jpx_flags": [],
            "evidence_hits": [{"name": "cashflow-yield-discount"}],
            "metrics": {
                "ocf_yield": 0.11,
                "er_annual": 0.09,
                "buyback_authorization_status": "stale_filing",
                "buyback_status_latest_filing_date": "2026-04-13",
                "buyback_status_filing_age_days": 113,
                "buyback_status_observed_from": "2025-08-01",
                # 枠の中身。提出の齢と違い、carry が forward の還元かを直接決める。
                "buyback_remaining_share_ratio": 0.32,
                "buyback_remaining_amount_ratio": 0.001,
                "buyback_trailing_3m_acquired_ratio": 0.004,
                "buyback_authorization_window_end": "2026-03-13",
                "buyback_report_month_end": "2026-03-31",
            },
        }
    ]

    payload = build_selection_payload(
        asof_date=ASOF,
        candidates=tuple(candidate_record_from_mapping(item) for item in candidates),
        macro_context=None,
        rules=rules,
        top=5,
        profile="balanced",
        candidates_ref="test.yaml",
        macro_context_ref=None,
        longlist_top=5,
    )

    recommendation = payload["recommendations"][0]
    assert recommendation["buyback_authorization_status"] == "stale_filing"
    assert recommendation["buyback_status_filing_age_days"] == 113
    # The window is what decides whether the carry is forward cash, so it has to be on
    # the same surface as the status. A reader who only sees the filing age reads
    # "recent enough" off a window that closed months ago.
    assert recommendation["buyback_authorization_window_end"] == "2026-03-13"
    assert recommendation["buyback_remaining_share_ratio"] == 0.32
    # 枠は株数と金額の 2 本を上限に持つ。株数側だけを出すと、金額枠を使い切った銘柄が
    # 「枠が 3 割残っている」と読める。
    assert recommendation["buyback_remaining_amount_ratio"] == 0.001
    assert recommendation["buyback_trailing_3m_acquired_ratio"] == 0.004
    longlist_row = payload["longlist"][0]
    assert longlist_row["buyback_authorization"] == {
        "status": "stale_filing",
        "latest_filing_date": "2026-04-13",
        "filing_age_days": 113,
        "observed_from": "2025-08-01",
        "remaining_share_ratio": 0.32,
        "remaining_amount_ratio": 0.001,
        "trailing_3m_acquired_ratio": 0.004,
        "authorization_window_end": "2026-03-13",
        "report_month_end": "2026-03-31",
    }


def test_the_amount_cap_is_read_independently_of_the_share_cap() -> None:
    """決議は株数と金額の 2 本を上限に持ち、先に尽きた方で取得が終わる。

    決議後に株価が上がった銘柄は金額枠を先に使い切り、株数枠を残したまま取得を終える。
    株数側だけを見ると枠が残っているように読めるので、両方が同じ面に出る必要がある。
    """

    annotation = BuybackAuthorization(
        status="recent_filing",
        latest_filing_date=date(2026, 7, 15),
        latest_filing_age_days=20,
        observed_from=COVERED_FROM,
    )
    reports = (
        StoredBuybackReport(
            report_month_end=date(2026, 6, 30),
            window_start=date(2026, 2, 1),
            window_end=date(2026, 9, 30),
            resolved_shares=1_000_000,
            cumulative_shares=620_000,
            month_shares=40_000,
            issued_shares=50_000_000,
            resolved_amount_yen=1_000_000_000,
            cumulative_amount_yen=999_990_000,
        ),
    )

    state = with_authorization_state(annotation, reports)

    assert state.remaining_share_ratio == 0.38
    assert state.remaining_amount_ratio is not None
    assert state.remaining_amount_ratio < 0.0001


def test_an_unreadable_amount_cap_is_not_reported_as_a_spent_one() -> None:
    """欠損は「読めなかった」であって「使い切った」ではない。0.0 で埋めない。"""

    annotation = BuybackAuthorization(
        status="recent_filing",
        latest_filing_date=date(2026, 7, 15),
        latest_filing_age_days=20,
        observed_from=COVERED_FROM,
    )
    reports = (
        StoredBuybackReport(
            report_month_end=date(2026, 6, 30),
            window_start=date(2026, 2, 1),
            window_end=date(2026, 9, 30),
            resolved_shares=1_000_000,
            cumulative_shares=620_000,
            month_shares=40_000,
            issued_shares=50_000_000,
            resolved_amount_yen=None,
            cumulative_amount_yen=None,
        ),
    )

    state = with_authorization_state(annotation, reports)

    assert state.remaining_share_ratio == 0.38
    assert state.remaining_amount_ratio is None
