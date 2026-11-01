"""決算・予想訂正・0配当を分離する合成financial document入力。"""

from __future__ import annotations

from typing import Any


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
