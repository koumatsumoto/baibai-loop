"""決算・予想訂正・0配当を分離する合成financial document入力。"""

from __future__ import annotations

from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Any

from baibai_engine.machine_analysis.observations.financials.jquants_normalization import (
    normalize_financial_summaries,
)
from baibai_engine.storage.sqlite.source_cache.coverage import (
    delete_date_range,
    record_range_source_coverage,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection


def seed_legacy_financial_sqlite(
    path: Path, records, *, requested_start: date, requested_end: date, fetched_at_utc=None
) -> int:
    """Arrange prior-release rows for read, merge and restore tests, not new financial input."""
    batch = normalize_financial_summaries(records)
    assert requested_start <= requested_end
    assert all(
        requested_start.isoformat() <= row[1] <= requested_end.isoformat() for row in batch.rows
    )
    with closing(open_connection(path)) as connection, connection:
        delete_date_range(
            connection, "jquants_fin_summaries", "disclosed_at", requested_start, requested_end
        )
        connection.executemany(
            "INSERT INTO jquants_fin_summaries(ticker, disclosed_at, disclosure_number, document_payload) VALUES (?, ?, ?, ?)",
            batch.rows,
        )
        return record_range_source_coverage(
            connection,
            source="jquants_fin_summaries",
            operation="get_fin_summary_range",
            table="jquants_fin_summaries",
            date_column="disclosed_at",
            requested_start=requested_start,
            requested_end=requested_end,
            status=batch.status,
            error=batch.error,
            fetched_at_utc=fetched_at_utc,
        )


def document(
    number: str, *, day: str = "2026-09-14", time: str = "16:00:00", **fields: Any
) -> dict[str, Any]:
    return {
        "Code": "99990",
        "DiscDate": day,
        "DiscTime": time,
        "DiscNo": number,
        "DocType": "EarnForecastRevision",
        "CurPerType": "FY",
        "CurFYSt": "2026-07-01",
        "CurFYEn": "2027-06-30",
        "CurPerSt": "2026-07-01",
        "CurPerEn": "2027-06-30",
        **fields,
    }


def documents() -> list[dict[str, Any]]:
    return [
        document(
            "statement",
            day="2026-08-10",
            DocType="FYFinancialStatements_NonConsolidated_JP",
            CurFYSt="2025-07-01",
            CurFYEn="2026-06-30",
            CurPerSt="2025-07-01",
            CurPerEn="2026-06-30",
            NxtFYSt="2026-07-01",
            NxtFYEn="2027-06-30",
            EPS="33.3",
            NP="86000000",
            DivAnn="7",
            NxFEPS="39.69",
            NxFNp="101000000",
            NxFOdP="164000000",
            NxFDivAnn="7",
            FNCEPS="",
            NxFNCEPS="",
        ),
        document("income-first", FNCEPS="31.9", FNCNP="81000000", FNCOdP="143000000"),
        document("dividend", DocType="DividendForecastRevision", FDivAnn="0.0"),
        document(
            "income-correction",
            time="19:00:00",
            FNCEPS="31.9",
            FNCNP="81000000",
            FNCOdP="144000000",
            FDivAnn="",
        ),
    ]
