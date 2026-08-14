from __future__ import annotations

import io
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pytest
from tests.tools.test_measure_signal_cohorts import _liquid, _store, _write_forward, _write_panel
from tools.experiments.measure_buyback_authorization import (
    DiagnosticRow,
    _complete_cohort_count,
    build_measurement,
)

from baibai_engine.market.sqlite import open_connection
from baibai_engine.screening.calibration.store import CalibrationCacheError


def _buyback_zip(*, purpose: str | None = None) -> bytes:
    board = (
        "取得期間 2026年８月１日～2026年９月30日"
        "100,000100,000,000計－10,00010,000,000"
        "報告月末現在の累計取得自己株式40,00040,000,000"
    )
    if purpose == "employee":
        disposal = (
            "引き受ける者の募集を行った取得自己株式（処分日）－月－日－－"
            "消却の処分を行った取得自己株式（消却日）－月－日－－"
            "合併、株式交換、株式交付、会社分割に係る移転を行った取得自己株式（移転日）－月－日－－"
            "その他（従業員持株会向け株式報酬）（処分日）８月15日1,0001,000,000"
            "合計1,0001,000,000"
        )
    elif purpose == "cancellation":
        disposal = (
            "引き受ける者の募集を行った取得自己株式（処分日）－月－日－－"
            "消却の処分を行った取得自己株式（消却日）８月20日2,0002,000,000"
            "合併、株式交換、株式交付、会社分割に係る移転を行った取得自己株式（移転日）－月－日－－"
            "その他（該当事項なし）（処分日）－月－日－－合計2,0002,000,000"
        )
    else:
        disposal = "２【処理状況】該当事項はありません。"
    rows = [
        ("jpcrp-sbr_cor:ReportingPeriodCoverPage", "至 2026年８月31日"),
        (
            "jpcrp-sbr_cor:AcquisitionsByResolutionOfBoardOfDirectorsMeetingTextBlock",
            board,
        ),
        ("jpcrp-sbr_cor:DisposalsOfTreasurySharesTextBlock", disposal),
        (
            "jpcrp-sbr_cor:HoldingOfTreasurySharesTextBlock",
            "発行済株式総数1,000,000保有自己株式数100,000",
        ),
    ]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "XBRL_TO_CSV/a.csv",
            "\n".join(f"{name}\t\t{value}" for name, value in rows).encode("utf-16"),
        )
    return buffer.getvalue()


def _seed_market(path: Path, zip_dir: Path) -> None:
    connection = open_connection(path)
    try:
        for index in range(20):
            ticker = f"{1100 + index}"
            doc_id = f"DOC-{ticker}"
            connection.execute(
                "INSERT INTO edinet_documents(doc_date, sequence_number, doc_id, sec_code, "
                "doc_type_code, csv_flag, xbrl_flag) VALUES ('2026-08-05', ?, ?, ?, '220', 1, 1)",
                (100 + index, doc_id, f"{ticker}0"),
            )
            active = index < 10
            connection.execute(
                "INSERT INTO edinet_buyback_reports("
                "ticker, report_month_end, doc_id, filed_on, window_start, window_end, "
                "resolved_shares, cumulative_shares, month_shares, issued_shares"
                ") VALUES (?, '2026-07-31', ?, '2026-08-05', '2026-08-01', ?, "
                "100000, ?, 10000, 1000000)",
                (
                    ticker,
                    doc_id,
                    "2026-09-30" if active else "2026-07-31",
                    40_000 if active else 100_000,
                ),
            )
            zip_dir.mkdir(parents=True, exist_ok=True)
            (zip_dir / f"{doc_id}.zip").write_bytes(
                _buyback_zip(purpose="employee" if active else "cancellation")
            )
        cursor = date(2025, 8, 31)
        through = date(2026, 8, 31)
        while cursor <= through:
            day = cursor.isoformat()
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM edinet_documents WHERE doc_date = ?", (day,)
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT INTO edinet_document_lists("
                "doc_date, process_datetime, result_count, fetched_at_utc, is_final"
                ") VALUES (?, NULL, ?, '2026-09-01T00:00:00+00:00', 1)",
                (day, count),
            )
            connection.execute(
                "INSERT INTO source_coverage("
                "source, coverage_key, coverage_start, coverage_end, fetched_at_utc, "
                "record_count, status, error"
                ") VALUES ('edinet_documents', ?, ?, ?, "
                "'2026-09-01T00:00:00+00:00', ?, 'ok', NULL)",
                (day, day, day, count),
            )
            cursor += timedelta(days=1)
        connection.commit()
    finally:
        connection.close()


def test_diagnostic_compares_the_same_rows_and_blocks_long_horizon_adoption(
    tmp_path: Path,
) -> None:
    calibration = _store(tmp_path)
    market = tmp_path / "market.sqlite"
    zip_dir = tmp_path / "zips"
    asof = "2026-08-31"
    panel_rows = []
    forward_rows = []
    for index in range(30):
        ticker = f"{1100 + index}"
        panel_rows.append(
            _liquid(
                ticker,
                asof,
                net_share_change_yoy=(-0.03 if index < 20 else 0.0),
            )
        )
        forward_rows.append(
            {
                "asof": asof,
                "ticker": ticker,
                "horizon": "3m",
                "price_return": "0.05" if index < 10 else "0.01",
                "status": "resolved",
            }
        )
    _write_panel(calibration, asof, panel_rows)
    _write_forward(calibration, asof, forward_rows)
    _seed_market(market, zip_dir)

    payload = build_measurement(
        calibration_dir=calibration,
        market_sqlite=market,
        edinet_zip_dir=zip_dir,
        horizons=("3m", "3y", "5y"),
    )

    assert payload["production_decision"]["adoption_allowed"] is False
    assert payload["production_decision"]["blocking_reasons"] == [
        "no_complete_3y_authorization_cohort",
        "no_complete_5y_authorization_cohort",
    ]
    result = payload["horizons"]["3m"]
    comparisons = result["same_cohort_component_comparison"]
    assert {item["common_resolved_rows"] for item in comparisons} == {30}
    assert result["authorization_groups"]["active"]["resolved_rows"] == 10
    assert result["authorization_groups"]["ended"]["resolved_rows"] == 10
    assert result["authorization_groups"]["no_filing"]["resolved_rows"] == 10
    assert (
        result["treasury_disposition_groups"]["employee_compensation_esop"]["resolved_rows"] == 10
    )
    assert result["treasury_disposition_groups"]["cancellation"]["resolved_rows"] == 10


def test_source_rows_filed_after_asof_do_not_enter_the_join(tmp_path: Path) -> None:
    calibration = _store(tmp_path)
    market = tmp_path / "market.sqlite"
    asof = "2026-08-04"
    _write_panel(calibration, asof, [_liquid("1111", asof, net_share_change_yoy=-0.03)])
    _write_forward(
        calibration,
        asof,
        [
            {
                "asof": asof,
                "ticker": "1111",
                "horizon": "3m",
                "price_return": "0.02",
                "status": "resolved",
            }
        ],
    )
    connection = open_connection(market)
    try:
        connection.execute(
            "INSERT INTO edinet_documents(doc_date, sequence_number, doc_id, sec_code, "
            "doc_type_code, csv_flag, xbrl_flag) VALUES "
            "('2026-08-05', 1, 'FUTURE', '11110', '220', 1, 1)"
        )
        connection.execute(
            "INSERT INTO edinet_buyback_reports("
            "ticker, report_month_end, doc_id, filed_on, window_end, resolved_shares, "
            "cumulative_shares, month_shares, issued_shares"
            ") VALUES ('1111', '2026-07-31', 'FUTURE', '2026-08-05', '2026-09-30', "
            "100000, 10000, 10000, 1000000)"
        )
        connection.commit()
    finally:
        connection.close()

    payload = build_measurement(
        calibration_dir=calibration,
        market_sqlite=market,
        edinet_zip_dir=tmp_path / "zips",
        horizons=("3m",),
    )

    groups = payload["horizons"]["3m"]["authorization_groups"]
    assert groups["active"]["eligible_rows"] == 0
    assert groups["unknown"]["eligible_rows"] == 1


def test_complete_cohort_requires_every_identity_not_only_resolved_survivors() -> None:
    rows = [
        DiagnosticRow(
            asof="2020-01-31",
            ticker=str(1100 + index),
            share_change_signal=0.02 if index < 10 else 0.0,
            authorization_signal=0.02 if index < 10 else 0.0,
            composition_signal=0.02 if index < 10 else 0.0,
            authorization_state="active" if index < 10 else "ended",
            purpose_coverage="complete" if index < 10 else "not_applicable",
            purposes=(),
            forward={} if index == 19 else {"3y": 0.2},
        )
        for index in range(20)
    ]

    assert _complete_cohort_count(rows, "3y") == 0


def test_duplicate_panel_identity_is_rejected_before_counting_groups(tmp_path: Path) -> None:
    calibration = _store(tmp_path)
    asof = "2026-08-31"

    with pytest.raises(CalibrationCacheError, match="duplicate primary key"):
        _write_panel(calibration, asof, [_liquid("1111", asof), _liquid("1111", asof)])


def test_duplicate_forward_identity_is_rejected_instead_of_overwritten(tmp_path: Path) -> None:
    calibration = _store(tmp_path)
    asof = "2026-08-31"
    _write_panel(calibration, asof, [_liquid("1111", asof)])
    with pytest.raises(CalibrationCacheError, match="duplicate primary key"):
        _write_forward(
            calibration,
            asof,
            [
                {
                    "asof": asof,
                    "ticker": "1111",
                    "horizon": "3y",
                    "price_return": value,
                    "status": "resolved",
                }
                for value in ("0.2", "0.3")
            ],
        )
