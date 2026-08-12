from __future__ import annotations

import io
import sqlite3
import zipfile
from datetime import date
from pathlib import Path

from baibai_engine.market.sqlite import open_connection
from baibai_engine.screening.buyback_authorization import (
    BuybackAuthorization,
    with_authorization_state,
)
from baibai_engine.screening.buyback_store import (
    StoredBuybackReport,
    read_buyback_reports,
    refresh_buyback_reports,
)
from baibai_engine.screening.providers.edinet import (
    EDINETProviderError,
    EDINETRateLimitError,
)

_PREFIX = "jpcrp-sbr_cor:"


def _filing(
    *, month_end: str, resolved: str, cumulative: str, month: str, with_period: bool = True
) -> bytes:
    board = (
        f"（２）【取締役会決議による取得の状況】{month_end}現在 区分株式数（株）価額の総額（円）"
        "取締役会（2026年５月８日）での決議状況（取得期間　2026年５月11日～2026年７月31日）"
        f"{resolved}300,000,000報告月における取得自己株式（取得日）"
        f"計－{month}5,000,000"
        f"報告月末現在の累計取得自己株式{cumulative}200,000,000"
    )
    holding = (
        f"３【保有状況】{month_end}現在 報告月末日における保有状況株式数（株）"
        "発行済株式総数10,000,000保有自己株式数500,000"
    )
    rows = [
        (f"{_PREFIX}AcquisitionsByResolutionOfBoardOfDirectorsMeetingTextBlock", "", board),
        (f"{_PREFIX}HoldingOfTreasurySharesTextBlock", "", holding),
    ]
    if with_period:
        rows.insert(
            0, (f"{_PREFIX}ReportingPeriodCoverPage", "", f"自　2026年７月１日　至　{month_end}")
        )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "XBRL_TO_CSV/a.csv",
            "\n".join("\t".join(row) for row in rows).encode("utf-16"),
        )
    return buffer.getvalue()


class _Filings:
    """A provider stand-in that answers with one archive per doc id."""

    def __init__(self, archives: dict[str, bytes]) -> None:
        self._archives = archives
        self.requested: list[str] = []

    def download_csv_zip(self, doc_id: str) -> bytes:
        self.requested.append(doc_id)
        return self._archives[doc_id]


def _store_with_documents(path: Path, rows: list[tuple[str, str, str, str]]) -> None:
    connection = open_connection(path)
    try:
        connection.executemany(
            "INSERT INTO edinet_documents(doc_date, sequence_number, doc_id, sec_code, "
            "doc_type_code, csv_flag, xbrl_flag) VALUES (?, ?, ?, ?, ?, 1, 1)",
            [
                (doc_date, index, doc_id, sec_code, doc_type)
                for index, (doc_date, doc_id, sec_code, doc_type) in enumerate(rows)
            ],
        )
        connection.commit()
    finally:
        connection.close()


def test_refresh_stores_one_row_per_reporting_month(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    _store_with_documents(
        path,
        [
            ("2026-07-01", "DOC-JUNE", "60880", "220"),
            ("2026-08-05", "DOC-JULY", "60880", "220"),
        ],
    )
    provider = _Filings(
        {
            "DOC-JUNE": _filing(
                month_end="2026年６月30日",
                resolved="600,000",
                cumulative="313,100",
                month="183,600",
            ),
            "DOC-JULY": _filing(
                month_end="2026年７月31日",
                resolved="600,000",
                cumulative="533,500",
                month="220,400",
            ),
        }
    )

    summary = refresh_buyback_reports(path, provider=provider, since=date(2026, 1, 1))

    assert summary.stored == 2
    assert summary.unreadable == 0
    reports = read_buyback_reports(
        path, tickers=["6088"], asof=date(2026, 8, 31), months=6
    ).reports["6088"]
    assert [report.report_month_end for report in reports] == [
        date(2026, 7, 31),
        date(2026, 6, 30),
    ]
    assert reports[0].cumulative_shares == 533_500


def test_refresh_skips_filings_it_already_holds(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    _store_with_documents(path, [("2026-08-05", "DOC-JULY", "60880", "220")])
    provider = _Filings(
        {
            "DOC-JULY": _filing(
                month_end="2026年７月31日",
                resolved="600,000",
                cumulative="533,500",
                month="220,400",
            )
        }
    )

    refresh_buyback_reports(path, provider=provider, since=date(2026, 1, 1))
    again = refresh_buyback_reports(path, provider=provider, since=date(2026, 1, 1))

    assert again.considered == 0
    assert provider.requested == ["DOC-JULY"]


def test_a_correction_replaces_the_month_it_corrects(tmp_path: Path) -> None:
    """Form 230 restates a month; the store must not keep both readings."""

    path = tmp_path / "market.sqlite"
    _store_with_documents(
        path,
        [
            ("2026-08-05", "DOC-JULY", "60880", "220"),
            ("2026-08-20", "DOC-JULY-FIX", "60880", "230"),
        ],
    )
    with open_connection(path) as connection:
        connection.execute(
            "UPDATE edinet_documents SET parent_doc_id = 'DOC-JULY' WHERE doc_id = 'DOC-JULY-FIX'"
        )
        connection.commit()
    provider = _Filings(
        {
            "DOC-JULY": _filing(
                month_end="2026年７月31日",
                resolved="600,000",
                cumulative="533,500",
                month="220,400",
            ),
            "DOC-JULY-FIX": _filing(
                month_end="2026年７月31日",
                resolved="600,000",
                cumulative="540,000",
                month="226,900",
            ),
        }
    )

    refresh_buyback_reports(path, provider=provider, since=date(2026, 1, 1))

    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT doc_id, cumulative_shares FROM edinet_buyback_reports"
        ).fetchall()
    assert rows == [("DOC-JULY-FIX", 540_000)]

    # A later refresh still sees the superseded original in the document list.  It must
    # not downgrade the stored correction merely because only the winning doc id is in
    # the one-row-per-month table.
    again = refresh_buyback_reports(path, provider=provider, since=date(2026, 1, 1))
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT doc_id, cumulative_shares FROM edinet_buyback_reports"
        ).fetchall() == [("DOC-JULY-FIX", 540_000)]
    assert again.considered == 0
    assert again.stored == 0
    assert provider.requested == ["DOC-JULY-FIX"]


def test_same_day_correction_wins_repeated_refreshes_independent_of_doc_id(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    _store_with_documents(
        path,
        [
            ("2026-08-05", "ZZZ-ORIGINAL", "60880", "220"),
            ("2026-08-05", "AAA-CORRECTION", "60880", "230"),
            ("2026-08-05", "BBB-CORRECTION-LATEST", "60880", "230"),
        ],
    )
    with open_connection(path) as connection:
        connection.execute(
            "UPDATE edinet_documents SET parent_doc_id = 'ZZZ-ORIGINAL' "
            "WHERE doc_id IN ('AAA-CORRECTION', 'BBB-CORRECTION-LATEST')"
        )
        connection.commit()
    provider = _Filings(
        {
            "ZZZ-ORIGINAL": _filing(
                month_end="2026年７月31日",
                resolved="600,000",
                cumulative="533,500",
                month="220,400",
            ),
            "AAA-CORRECTION": _filing(
                month_end="2026年７月31日",
                resolved="600,000",
                cumulative="540,000",
                month="226,900",
            ),
            "BBB-CORRECTION-LATEST": _filing(
                month_end="2026年７月31日",
                resolved="600,000",
                cumulative="545,000",
                month="231,900",
            ),
        }
    )

    for _ in range(3):
        refresh_buyback_reports(path, provider=provider, since=date(2026, 1, 1))

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT doc_id, cumulative_shares FROM edinet_buyback_reports"
        ).fetchall() == [("BBB-CORRECTION-LATEST", 545_000)]
    assert provider.requested == ["BBB-CORRECTION-LATEST"]


def test_an_unreadable_filing_is_counted_and_does_not_stop_the_rest(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    _store_with_documents(
        path,
        [
            ("2026-08-04", "DOC-BROKEN", "70000", "220"),
            ("2026-08-05", "DOC-JULY", "60880", "220"),
        ],
    )
    provider = _Filings(
        {
            "DOC-BROKEN": b"not a zip",
            "DOC-JULY": _filing(
                month_end="2026年７月31日",
                resolved="600,000",
                cumulative="533,500",
                month="220,400",
            ),
        }
    )

    summary = refresh_buyback_reports(path, provider=provider, since=date(2026, 1, 1))

    assert summary.unreadable == 1
    assert summary.stored == 1


def test_a_month_ending_after_the_filing_is_rejected_not_stored(tmp_path: Path) -> None:
    """The form reports a month that has closed, so a later one is a misread date.

    Stored, it would file a stale reading as the newest month the ticker has, because the
    month is what orders the rows. Observed on real filings: 5 of 4,298 backfilled rows
    carried a reporting month after their own filing date.
    """
    path = tmp_path / "market.sqlite"
    _store_with_documents(
        path,
        [
            ("2026-02-05", "DOC-AHEAD", "63630", "220"),
            ("2026-08-05", "DOC-JULY", "60880", "220"),
        ],
    )
    provider = _Filings(
        {
            "DOC-AHEAD": _filing(
                month_end="2026年12月31日",
                resolved="600,000",
                cumulative="503,000",
                month="12,000",
            ),
            "DOC-JULY": _filing(
                month_end="2026年７月31日",
                resolved="600,000",
                cumulative="533,500",
                month="220,400",
            ),
        }
    )

    summary = refresh_buyback_reports(path, provider=provider, since=date(2026, 1, 1))

    assert summary.without_usable_month == 1
    assert summary.stored == 1
    assert (
        read_buyback_reports(path, tickers=["6363"], asof=date(2026, 12, 31), months=24).reports
        == {}
    )


def test_a_filing_whose_month_cannot_be_read_is_skipped(tmp_path: Path) -> None:
    """Without a month the row has no identity, so there is nowhere to put it."""
    path = tmp_path / "market.sqlite"
    _store_with_documents(path, [("2026-08-05", "DOC-NO-MONTH", "60880", "220")])
    provider = _Filings(
        {
            "DOC-NO-MONTH": _filing(
                month_end="2026年７月31日",
                resolved="600,000",
                cumulative="533,500",
                month="220,400",
                with_period=False,
            )
        }
    )

    summary = refresh_buyback_reports(path, provider=provider, since=date(2026, 1, 1))

    assert summary.without_usable_month == 1
    assert summary.stored == 0


def test_reads_are_bounded_by_the_as_of(tmp_path: Path) -> None:
    """A replay must not see a reporting month that had not happened yet."""

    path = tmp_path / "market.sqlite"
    _store_with_documents(
        path,
        [
            ("2026-07-01", "DOC-JUNE", "60880", "220"),
            ("2026-08-05", "DOC-JULY", "60880", "220"),
        ],
    )
    provider = _Filings(
        {
            "DOC-JUNE": _filing(
                month_end="2026年６月30日",
                resolved="600,000",
                cumulative="313,100",
                month="183,600",
            ),
            "DOC-JULY": _filing(
                month_end="2026年７月31日",
                resolved="600,000",
                cumulative="533,500",
                month="220,400",
            ),
        }
    )
    refresh_buyback_reports(path, provider=provider, since=date(2026, 1, 1))

    reports = read_buyback_reports(
        path, tickers=["6088"], asof=date(2026, 7, 15), months=6
    ).reports["6088"]

    assert [report.report_month_end for report in reports] == [date(2026, 6, 30)]


def test_a_closed_report_month_is_not_visible_before_its_filing_date(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    _store_with_documents(path, [("2026-08-05", "DOC-JULY", "60880", "220")])
    refresh_buyback_reports(
        path,
        provider=_Filings(
            {
                "DOC-JULY": _filing(
                    month_end="2026年７月31日",
                    resolved="600,000",
                    cumulative="533,500",
                    month="220,400",
                )
            }
        ),
        since=date(2026, 1, 1),
    )

    assert (
        read_buyback_reports(path, tickers=["6088"], asof=date(2026, 8, 4), months=6).reports == {}
    )
    visible = read_buyback_reports(path, tickers=["6088"], asof=date(2026, 8, 5), months=6).reports[
        "6088"
    ]
    assert visible[0].doc_id == "DOC-JULY"
    assert visible[0].filed_on == date(2026, 8, 5)


def _annotation() -> BuybackAuthorization:
    return BuybackAuthorization(
        status="recent_filing",
        latest_filing_date=date(2026, 8, 5),
        latest_filing_age_days=1,
        observed_from=date(2025, 8, 1),
    )


def _report(**overrides: object) -> StoredBuybackReport:
    values: dict[str, object] = {
        "report_month_end": date(2026, 7, 31),
        "window_start": date(2026, 5, 11),
        "window_end": date(2026, 7, 31),
        "resolved_shares": 600_000,
        "cumulative_shares": 533_500,
        "month_shares": 220_400,
        "issued_shares": 10_000_000,
    }
    values.update(overrides)
    return StoredBuybackReport(**values)  # type: ignore[arg-type]


def test_the_annotation_gains_the_remainder_and_the_pace() -> None:
    annotated = with_authorization_state(
        _annotation(),
        [
            _report(),
            _report(report_month_end=date(2026, 6, 30), month_shares=183_600),
            _report(report_month_end=date(2026, 5, 31), month_shares=129_500),
        ],
    )

    # 600,000 authorised, 533,500 bought: 66,500 of the authorisation is still open.
    assert annotated.remaining_share_ratio == 66_500 / 600_000
    # (220,400 + 183,600 + 129,500) / 10,000,000 = 533,500 / 10,000,000
    assert annotated.trailing_3m_acquired_ratio == 0.05335
    assert annotated.authorization_window_end == date(2026, 7, 31)
    assert annotated.report_month_end == date(2026, 7, 31)
    # The observation-window status is a separate question and is left alone.
    assert annotated.status == "recent_filing"


def test_an_unreadable_authorisation_leaves_the_remainder_absent_not_zero() -> None:
    """Absent must not read as "the authorisation is spent"."""

    annotated = with_authorization_state(_annotation(), [_report(resolved_shares=None)])

    assert annotated.remaining_share_ratio is None
    assert annotated.authorization_window_end == date(2026, 7, 31)


def test_a_gap_in_the_monthly_counts_leaves_the_pace_absent() -> None:
    """Summing the months that happen to be readable would understate the pace."""

    annotated = with_authorization_state(
        _annotation(),
        [
            _report(),
            _report(report_month_end=date(2026, 6, 30), month_shares=None),
            _report(report_month_end=date(2026, 5, 31), month_shares=129_500),
        ],
    )

    assert annotated.trailing_3m_acquired_ratio is None


def test_no_stored_report_leaves_the_annotation_untouched() -> None:
    annotated = with_authorization_state(_annotation(), [])

    assert annotated == _annotation()


def test_a_ticker_without_rows_is_absent_from_the_read(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    _store_with_documents(path, [])

    assert (
        read_buyback_reports(path, tickers=["9999"], asof=date(2026, 8, 31), months=6).reports == {}
    )


class _RefusingFilings:
    """A provider that refuses one document and serves the next."""

    def __init__(self, refuse: str, served: dict[str, bytes]) -> None:
        self._refuse = refuse
        self._served = served

    def download_csv_zip(self, doc_id: str) -> bytes:
        if doc_id == self._refuse:
            raise EDINETProviderError("EDINET CSV ZIP response was not a zip: zip_invalid")
        return self._served[doc_id]


class _RateLimitedFilings:
    def download_csv_zip(self, doc_id: str) -> bytes:
        raise EDINETRateLimitError(f"back off before {doc_id}")


def test_a_document_edinet_will_not_serve_does_not_stop_the_run(tmp_path: Path) -> None:
    """Observed on the real corpus: one filing answered with a non-zip body."""

    path = tmp_path / "market.sqlite"
    _store_with_documents(
        path,
        [
            ("2026-08-04", "DOC-REFUSED", "70000", "220"),
            ("2026-08-05", "DOC-JULY", "60880", "220"),
        ],
    )
    provider = _RefusingFilings(
        "DOC-REFUSED",
        {
            "DOC-JULY": _filing(
                month_end="2026年７月31日",
                resolved="600,000",
                cumulative="533,500",
                month="220,400",
            )
        },
    )

    summary = refresh_buyback_reports(path, provider=provider, since=date(2026, 1, 1))

    assert summary.unreadable == 1
    assert summary.stored == 1


def test_rate_limiting_ends_the_pass_without_marking_filings_unreadable(
    tmp_path: Path,
) -> None:
    """Backing off is the API asking for a pause, not thousands of bad filings."""

    path = tmp_path / "market.sqlite"
    _store_with_documents(path, [("2026-08-05", "DOC-JULY", "60880", "220")])

    summary = refresh_buyback_reports(path, provider=_RateLimitedFilings(), since=date(2026, 1, 1))

    assert summary.rate_limited is True
    assert summary.unreadable == 0
    assert summary.stored == 0


def test_a_rate_limited_pass_keeps_what_it_already_stored(tmp_path: Path) -> None:
    """A backfill of thousands must advance every run, not restart."""

    path = tmp_path / "market.sqlite"
    _store_with_documents(
        path,
        [
            ("2026-07-01", "DOC-JUNE", "60880", "220"),
            ("2026-08-05", "DOC-LATER", "70000", "220"),
        ],
    )

    class _LimitAfterFirst:
        def download_csv_zip(self, doc_id: str) -> bytes:
            if doc_id == "DOC-LATER":
                raise EDINETRateLimitError("back off")
            return _filing(
                month_end="2026年６月30日",
                resolved="600,000",
                cumulative="313,100",
                month="183,600",
            )

    summary = refresh_buyback_reports(path, provider=_LimitAfterFirst(), since=date(2026, 1, 1))

    assert summary.rate_limited is True
    assert summary.stored == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM edinet_buyback_reports").fetchone()[0] == 1


def test_a_row_dropped_at_read_time_is_counted(tmp_path: Path) -> None:
    """落とした事実を数に残さないと、様式が読めないことと提出が無いことが同じ null になる。

    規則が書かれる前に保存された行は取り込みをやり直さないと直らないので、読み取り側でも
    同じ検査を通す。そこで落ちた件数は run の fallback 行へ出す。
    """

    path = tmp_path / "market.sqlite"
    _store_with_documents(path, [("2026-08-05", "DOC-JULY", "60880", "220")])
    provider = _Filings(
        {
            "DOC-JULY": _filing(
                month_end="2026年７月31日",
                resolved="600,000",
                cumulative="533,500",
                month="220,400",
            )
        }
    )
    refresh_buyback_reports(path, provider=provider, since=date(2026, 1, 1))

    clean = read_buyback_reports(path, tickers=["6088"], asof=date(2026, 8, 31), months=6)
    assert clean.inconsistent_rows == 0
    assert clean.reports["6088"][0].cumulative_shares == 533_500

    # 桁落ちで累計株数だけが 1 株になった行。金額の側は正しいまま残る形で、実 store にも
    # 2 行ある (6417 / 3221)。
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE edinet_buyback_reports SET cumulative_shares = 1, "
            "cumulative_amount_yen = 4800600 WHERE ticker = '6088'"
        )

    broken = read_buyback_reports(path, tickers=["6088"], asof=date(2026, 8, 31), months=6)
    assert broken.inconsistent_rows == 1
    assert broken.inconsistent_tickers == 1
    assert broken.reports["6088"][0].cumulative_shares is None
