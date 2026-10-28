from __future__ import annotations

import sqlite3
import zipfile
from contextlib import closing
from datetime import date
from io import BytesIO, StringIO

import pytest

from baibai_engine.machine_analysis.observations.financials.edinet_metrics.csv import (
    parse_csv_zip_metric_record,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.revision import (
    EDINET_METRIC_REVISION,
)
from baibai_engine.machine_analysis.observations.financials.metric_quality import TTMQuality


def _edinet_csv_zip(rows: list[tuple[str, str, str]]) -> bytes:
    csv_text = "要素ID\tコンテキストID\tユニットID\t値\n" + "\n".join(
        f"{element}\t{context}\tJPY\t{value}" for element, context, value in rows
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("XBRL_TO_CSV/sample.csv", csv_text.encode("utf-16"))
    return buffer.getvalue()


def test_metric_values_and_quality_reasons_remain_stable() -> None:
    record = parse_csv_zip_metric_record(
        ticker="7203",
        doc_id="S100REVISION",
        doc_type_code="120",
        content=_edinet_csv_zip(
            [
                ("jppfs_cor:NetSales", "CurrentYearDuration", "1000"),
                (
                    "jppfs_cor:NetCashProvidedByUsedInOperatingActivities",
                    "CurrentYearDuration",
                    "150",
                ),
                ("jppfs_cor:OperatingIncome", "CurrentYearDuration", "90"),
                (
                    "jppfs_cor:DepreciationAndAmortizationOpeCF",
                    "CurrentYearDuration",
                    "30",
                ),
                (
                    "jppfs_cor:PurchaseOfPropertyPlantAndEquipmentInvCF",
                    "CurrentYearDuration",
                    "-40",
                ),
                ("jppfs_cor:PurchaseOfIntangibleAssetsInvCF", "CurrentYearDuration", "0"),
                ("jppfs_cor:CashAndDeposits", "CurrentYearInstant", "300"),
                ("jppfs_cor:InvestmentSecurities", "CurrentYearInstant", "250"),
                ("jppfs_cor:ShortTermLoansPayable", "CurrentYearInstant", "20"),
                ("jppfs_cor:LongTermLoansPayable", "CurrentYearInstant", "50"),
                ("jppfs_cor:NetAssets", "CurrentYearInstant", "800"),
                ("jppfs_cor:Assets", "CurrentYearInstant", "1400"),
            ]
        ),
        submit_datetime="2026-04-01 12:00",
        period_start=date(2025, 4, 1),
        period_end=date(2026, 3, 31),
    )

    assert record.ticker == "7203"
    assert record.source_doc_id == "S100REVISION"
    assert record.sales_ttm == 1000.0
    assert record.ocf_ttm == 150.0
    assert record.capex_ttm == 40.0
    assert record.fcf_ttm == 110.0
    assert record.ebitda_ttm == 120.0
    assert record.cash == 300.0
    assert record.debt == 70.0
    assert record.net_cash == 230.0
    assert record.investment_securities == 250.0
    assert record.equity == 800.0
    assert record.total_assets == 1400.0
    assert record.ttm_quality_fcf is TTMQuality.EXACT
    assert record.failure_reasons == ()


@pytest.mark.parametrize(
    ("change", "expected_exit", "downloads"),
    [
        ("current_revision", 0, 0),
        ("old_scope_revision", 0, 1),
        ("legacy", 0, 1),
        ("other_revision", 0, 1),
        ("null_revision", 0, 1),
        ("failed_coverage", 0, 1),
        ("hard_failure", 1, 0),
        ("corrupt_count", 1, 0),
        ("invalid_ticker", 1, 0),
    ],
)
def test_existing_baseline_reuse_and_failure_contract(tmp_path, change, expected_exit, downloads):
    from tests.engine.test_source_maintenance_cli import (
        FakeEDINETMetricReader,
    )
    from tests.engine.test_source_maintenance_cli import _edinet_csv_zip as csv_zip
    from tests.engine.test_source_maintenance_cli import _edinet_document

    from baibai_engine.machine_analysis.observations.financials.edinet_metrics.service import (
        extract_edinet_metrics_command,
    )

    db = tmp_path / "market.sqlite"
    document = _edinet_document(ticker="72030", doc_id="S100BASELINE")
    initial = FakeEDINETMetricReader(
        documents=[document], zip_by_doc_id={"S100BASELINE": csv_zip()}
    )
    assert (
        extract_edinet_metrics_command(
            asof_date=date(2026, 4, 24),
            lookback_days=0,
            reader=initial,
            sqlite_path=db,
            stdout=StringIO(),
        )
        == 0
    )
    with closing(sqlite3.connect(db)) as connection, connection:
        original = connection.execute("SELECT * FROM edinet_metrics ORDER BY ticker").fetchall()
        assert connection.execute("SELECT extractor_revision FROM edinet_metrics").fetchone() == (
            EDINET_METRIC_REVISION,
        )
        if change == "legacy":
            connection.execute(
                "UPDATE edinet_metrics SET extractor_revision=?",
                ("9b82e8d0d13ac87aaf325561de0d93fb54466178254f49856beff1b433e07187",),
            )
        elif change == "old_scope_revision":
            connection.execute(
                "UPDATE edinet_metrics SET extractor_revision=?",
                ("edinet-metrics-v6-capex-total-presence",),
            )
        elif change in {"other_revision", "null_revision"}:
            connection.execute(
                "UPDATE edinet_metrics SET extractor_revision=?",
                ("different-meaning" if change == "other_revision" else None,),
            )
        elif change == "failed_coverage":
            connection.execute(
                "UPDATE source_coverage SET status='failed' WHERE source='edinet_metrics'"
            )
        elif change == "hard_failure":
            connection.execute(
                "UPDATE edinet_metrics SET failure_reasons=?", ('["csv_parse_failed"]',)
            )
        elif change == "corrupt_count":
            connection.execute(
                "UPDATE source_coverage SET record_count=999 WHERE source='edinet_metrics'"
            )
        elif change == "invalid_ticker":
            connection.execute("UPDATE edinet_metrics SET ticker='invalid'")
        retained = connection.execute("SELECT * FROM edinet_metrics ORDER BY ticker").fetchall()
    # A missing ZIP for the current revision makes accidental re-extraction fail.
    retry = FakeEDINETMetricReader(
        documents=[document], zip_by_doc_id={"S100BASELINE": csv_zip()} if downloads else {}
    )
    assert (
        extract_edinet_metrics_command(
            asof_date=date(2026, 4, 25),
            lookback_days=0,
            reader=retry,
            sqlite_path=db,
            stdout=StringIO(),
        )
        == expected_exit
    )
    assert len(retry.read_calls) == downloads
    with closing(sqlite3.connect(db)) as connection, connection:
        latest = connection.execute(
            "SELECT * FROM edinet_metrics WHERE asof_date='2026-04-25'"
        ).fetchall()
    if expected_exit:
        assert latest == []
        with closing(sqlite3.connect(db)) as connection:
            assert (
                connection.execute("SELECT * FROM edinet_metrics ORDER BY ticker").fetchall()
                == retained
            )
    elif change == "current_revision":
        # All extracted columns, including provenance and quality, are reused unchanged.
        assert [row[1:] for row in latest] == [row[1:] for row in original]
    elif change == "old_scope_revision":
        with closing(sqlite3.connect(db)) as connection:
            assert connection.execute(
                "SELECT extractor_revision FROM edinet_metrics WHERE asof_date='2026-04-25'"
            ).fetchone() == (EDINET_METRIC_REVISION,)
