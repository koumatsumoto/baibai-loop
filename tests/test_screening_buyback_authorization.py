from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from baibai_engine.screening.buyback_authorization import (
    OBSERVATION_WINDOW_DAYS,
    RECENT_FILING_WINDOW_DAYS,
    build_buyback_authorization,
    index_buyback_status_filings,
    read_buyback_status_filings,
)
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


def test_a_gap_in_the_ingested_months_shortens_the_observed_window(tmp_path: Path) -> None:
    """途中に取得の穴があると、その月に提出した会社が一斉に「提出なし」へ落ちる。

    左端だけを見ると穴に気づけないので、as-of から遡って連続している範囲を窓にする。
    """

    sqlite_path = tmp_path / "market.sqlite"
    open_connection(sqlite_path).close()
    for day in (date(2025, 9, 3), date(2026, 7, 1), date(2026, 8, 3)):
        store_edinet_documents(
            sqlite_path,
            day,
            [_document(sequence_number=1, doc_type_code="220", sec_code="60880")],
        )

    read = read_buyback_status_filings(sqlite_path, through=ASOF)

    assert read is not None
    # 2025-10 〜 2026-06 は 1 件も取り込まれていないので、窓は 2026-07 から。
    assert read.observed_from == date(2026, 7, 1)
    annotation = build_buyback_authorization(
        asof=ASOF, latest_filing_date=None, observed_from=read.observed_from
    )
    assert annotation.status == "unknown"


def test_an_ingestion_stall_at_the_recent_end_falls_back_to_the_previous_month(
    tmp_path: Path,
) -> None:
    """当月分がまだ 1 件も出ていない状態は正常なので、直前月から連続性を見る。"""

    sqlite_path = tmp_path / "market.sqlite"
    open_connection(sqlite_path).close()
    for day in (date(2026, 6, 2), date(2026, 7, 1)):
        store_edinet_documents(
            sqlite_path,
            day,
            [_document(sequence_number=1, doc_type_code="220", sec_code="60880")],
        )

    read = read_buyback_status_filings(sqlite_path, through=ASOF)

    assert read is not None
    assert read.observed_from == date(2026, 6, 1)


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
    longlist_row = payload["longlist"][0]
    assert longlist_row["buyback_authorization"] == {
        "status": "stale_filing",
        "latest_filing_date": "2026-04-13",
        "filing_age_days": 113,
        "observed_from": "2025-08-01",
    }
