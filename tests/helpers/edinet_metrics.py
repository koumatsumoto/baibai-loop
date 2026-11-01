"""Synthetic EDINET metric inputs shared by extraction and Jobs integration tests."""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from datetime import date
from io import BytesIO

from baibai_engine.machine_analysis.observations.financials.edinet_models import EDINETProviderError


@dataclass
class FakeEDINETMetricReader:
    documents: list[dict[str, object]] | None = None
    zip_by_doc_id: dict[str, bytes] | None = None
    list_calls: list[date] = field(default_factory=list)
    read_calls: list[str] = field(default_factory=list)

    def list_documents(self, on_date: date) -> list[dict[str, object]]:
        self.list_calls.append(on_date)
        return list(self.documents or [])

    def read_csv_zip(self, doc_id: str) -> bytes:
        self.read_calls.append(doc_id)
        try:
            return (self.zip_by_doc_id or {})[doc_id]
        except KeyError as exc:
            raise EDINETProviderError(f"stored EDINET archive is unavailable for {doc_id}") from exc


def _edinet_csv_zip(*, include_debt: bool = True) -> bytes:
    rows = [
        ("jppfs_cor:NetSales", "CurrentYearDuration", "1000"),
        (
            "jppfs_cor:NetCashProvidedByUsedInOperatingActivities",
            "CurrentYearDuration",
            "900",
        ),
        ("jppfs_cor:OperatingIncome", "CurrentYearDuration", "150"),
        ("jppfs_cor:CashAndDeposits", "CurrentYearInstant", "1000"),
        (
            "jppfs_cor:PurchaseOfPropertyPlantAndEquipmentInvCF",
            "CurrentYearDuration",
            "-200",
        ),
        ("jppfs_cor:PurchaseOfIntangibleAssetsInvCF", "CurrentYearDuration", "0"),
        ("jppfs_cor:NetAssets", "CurrentYearInstant", "1200"),
        ("jppfs_cor:Assets", "CurrentYearInstant", "2000"),
        ("jppfs_cor:DepreciationAndAmortizationOpeCF", "CurrentYearDuration", "50"),
    ]
    if include_debt:
        rows.extend(
            [
                ("jppfs_cor:ShortTermLoansPayable", "CurrentYearInstant", "100"),
                ("jppfs_cor:LongTermLoansPayable", "CurrentYearInstant", "300"),
            ]
        )
    text = "要素ID\tコンテキストID\tユニットID\t値\n" + "\n".join(
        f"{element}\t{context}\tJPY\t{value}" for element, context, value in rows
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("XBRL_TO_CSV/test.csv", text.encode("utf-16"))
    return buffer.getvalue()


def _edinet_document(
    *,
    ticker: str,
    doc_id: str,
    doc_type: str = "120",
) -> dict[str, object]:
    return {
        "docID": doc_id,
        "secCode": ticker,
        "docTypeCode": doc_type,
        "csvFlag": "1",
        "xbrlFlag": "1",
        "legalStatus": "1",
        "disclosureStatus": "0",
        "withdrawalStatus": "0",
        "periodStart": "2025-04-01",
        "periodEnd": "2026-03-31",
        "submitDateTime": "2026-04-01 12:00",
    }
