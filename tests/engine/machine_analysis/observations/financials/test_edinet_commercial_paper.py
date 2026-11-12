"""CP残高を借入・社債と区別し、CFの純増減や他scopeを加算しない。"""

import zipfile
from datetime import date
from io import BytesIO
from pathlib import Path

import pytest

from baibai_engine.machine_analysis.models.company_financials.metrics.financial_snapshot import (
    build_financial_snapshot,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_financials.reported_flows import (
    financial_observation_payload,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.csv import (
    parse_csv_zip_metric_record,
)

CP = "jppfs_cor:CommercialPapersLiabilities"


def zipped(text):
    body = BytesIO()
    with zipfile.ZipFile(body, "w") as archive:
        archive.writestr("facts.csv", text.encode("utf-16"))
    return body.getvalue()


def test_native_commercial_paper_reaches_reported_balances_without_changing_period_identity():
    fixture = Path(__file__).parents[4] / "fixtures" / "edinet" / "S100ZEFD.tsv"
    record = parse_csv_zip_metric_record(
        ticker="5803",
        doc_id="S100ZEFD",
        doc_type_code="160",
        content=zipped(fixture.read_text()),
        submit_datetime="2026-11-11 10:55",
        period_start=date(2026, 4, 1),
        period_end=date(2027, 3, 31),
    )
    # 40,534 + 20,000 CP + 10,000 current bonds + 10,000 bonds + 26,950 (million JPY).
    assert record.debt == 107_484_000_000
    assert record.cash == 244_826_000_000
    assert record.net_cash == 137_342_000_000
    assert record.consolidation_basis == "consolidated"
    assert "metric_unavailable:debt" not in record.failure_reasons
    snapshot = build_financial_snapshot(
        None,
        [],
        record,
        DEFAULT_TTM_RULES,
        adjustment_events=(),
        asof_date=date(2026, 11, 12),
    )
    payload = financial_observation_payload(snapshot, record)
    balances = payload["edinet_reported_balances"]
    assert balances["amounts"]["debt"] == record.debt
    assert balances["amounts"]["net_cash"] == record.net_cash
    assert balances["source_doc_id"] == "S100ZEFD"
    assert balances["amount_unit"] == "JPY"
    assert balances["interpretation"] == "reported_financial_balances_not_common_equity_surplus"
    assert balances["measurement_date"] == {"date": None, "reason": "fact_date_not_retained"}
    assert balances["filing_fiscal_period"]["end"] == date(2027, 3, 31)


def parse(rows, *, kind="160", context="InterimInstant"):
    text = "要素ID\tコンテキストID\tユニットID\t値\n" + "\n".join(
        "\t".join(row)
        for row in [
            ("jpdei_cor:AccountingStandardsDEI", "FilingDateInstant", "－", "Japan GAAP"),
            (
                "jpdei_cor:WhetherConsolidatedFinancialStatementsArePreparedDEI",
                "FilingDateInstant",
                "－",
                "true",
            ),
            ("jppfs_cor:CashAndDeposits", context, "JPY", "1000"),
            *rows,
        ]
    )
    return parse_csv_zip_metric_record(
        ticker="0000", doc_id="S100TEST", doc_type_code=kind, content=zipped(text)
    )


@pytest.mark.parametrize(
    ("kind", "instant", "duration"),
    [
        ("120", "CurrentYearInstant", "CurrentYearDuration"),
        ("140", "CurrentQuarterInstant", "CurrentYTDDuration"),
        ("160", "InterimInstant", "InterimDuration"),
    ],
)
def test_commercial_paper_scope_deduplication_and_financing_flow(kind, instant, duration):
    record = parse(
        [
            ("jppfs_cor:ShortTermLoansPayable", instant, "JPY", "40"),
            ("jppfs_cor:BondsPayable", instant, "JPY", "80"),
            (CP, instant, "JPY", "20"),
            (CP, instant, "JPY", "20"),
            (CP, "Prior1YearInstant", "JPY", "200"),
            (CP, f"{instant}_NonConsolidatedMember", "JPY", "300"),
            (CP, f"{instant}_SegmentMember", "JPY", "400"),
            ("issuer:CommercialPapersLiabilities", instant, "JPY", "500"),
            ("jppfs_cor:NetIncreaseDecreaseInCommercialPapersFinCF", duration, "JPY", "20"),
        ],
        kind=kind,
        context=instant,
    )
    assert record.debt == 140
    assert record.net_cash == 860


@pytest.mark.parametrize(
    ("values", "unit", "expected"),
    [
        ([], "JPY", None),
        (["0"], "JPY", 0),
        (["-"], "JPY", 0),
        (["－"], "JPY", None),
        (["20"], "USD", None),
        (["20", "21"], "JPY", None),
        (["20", "－"], "JPY", None),
    ],
)
@pytest.mark.parametrize("with_borrowing", [False, True])
def test_commercial_paper_missing_nil_currency_conflict_and_explicit_zero(
    values, unit, expected, with_borrowing
):
    rows = [(CP, "InterimInstant", unit, value) for value in values]
    if with_borrowing:
        rows.append(("jppfs_cor:ShortTermLoansPayable", "InterimInstant", "JPY", "40"))
        if not values or expected is not None:
            expected = 40 + (expected or 0)
    record = parse(rows)
    assert record.debt == expected
    assert record.net_cash == (None if expected is None else 1000 - expected)
    assert record.ttm_quality_net_cash == ("unavailable" if expected is None else "exact")
