"""Only current company-wide statements supply valuation facts."""

import zipfile
from io import BytesIO

import pytest

from baibai_engine.market.providers.edinet_csv import parse_csv_zip_metric_record


def _parse(kind, rows):
    header = "要素ID\tコンテキストID\t連結・個別\tユニットID\t値\n"
    text = header + "\n".join(
        f"{element}\t{context}\t{basis}\tJPY\t{value}"
        for element, context, basis, value in rows
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("XBRL_TO_CSV/facts.csv", text.encode("utf-16-le"))
    return parse_csv_zip_metric_record(
        ticker="0000", doc_id="S100TEST", doc_type_code=kind, content=buffer.getvalue()
    )


@pytest.mark.parametrize("kind,instant,duration", [
    ("120", "CurrentYearInstant", "CurrentYearDuration"),
    ("130", "CurrentYearInstant", "CurrentYearDuration"),
    ("160", "InterimInstant", "InterimDuration"),
    ("170", "InterimInstant", "InterimDuration"),
    ("140", "CurrentQuarterInstant", "CurrentYTDDuration"),
    ("150", "CurrentQuarterInstant", "CurrentYTDDuration"),
])
def test_document_period_not_context_lexical_order(kind, instant, duration):
    record = _parse(kind, [
        ("jppfs_cor:CashAndDeposits", instant, "連結", "100"),
        ("jppfs_cor:NetSales", duration, "連結", "200"),
        ("jppfs_cor:NetSales", "CurrentQuarterDuration", "連結", "999"),
        ("jppfs_cor:NetSales", "Prior1YearDuration", "連結", "888"),
        ("jppfs_cor:NetSales", f"{duration}_SegmentMember", "連結", "777"),
    ])
    assert record.cash == 100
    assert record.sales_ttm == 200


@pytest.mark.parametrize("context", [
    "Prior1YearInstant", "NextYearInstant", "CurrentYearInstant_SegmentMember",
])
def test_absent_current_balance_is_not_replaced_by_other_scope(context):
    record = _parse("120", [("jppfs_cor:CashAndDeposits", context, "連結", "900")])
    assert record.cash is None
    assert record.consolidation_basis is None


def test_other_csv_basis_label_does_not_hide_financial_statement_context():
    record = _parse("120", [
        ("issuer:CashAndCashEquivalents", "CurrentYearInstant", "その他", "100"),
        ("issuer:CashAndCashEquivalents", "CurrentYearInstant_NonConsolidatedMember", "その他", "900"),
    ])
    assert record.cash == 100
    assert record.consolidation_basis == "consolidated"


def test_prior_consolidated_fact_does_not_hide_current_individual_statement():
    record = _parse("120", [
        ("jppfs_cor:CashAndDeposits", "Prior1YearInstant", "連結", "900"),
        ("jppfs_cor:CashAndDeposits", "CurrentYearInstant_NonConsolidatedMember", "個別", "100"),
    ])
    assert record.cash == 100
    assert record.consolidation_basis == "non_consolidated"


@pytest.mark.parametrize("reverse", [False, True])
def test_cash_concept_preference_is_not_csv_row_order(reverse):
    rows = [
        ("jppfs_cor:CashAndCashEquivalents", "CurrentYearInstant", "連結", "200"),
        ("jppfs_cor:CashAndDeposits", "CurrentYearInstant", "連結", "100"),
    ]
    assert _parse("120", list(reversed(rows)) if reverse else rows).cash == 100
