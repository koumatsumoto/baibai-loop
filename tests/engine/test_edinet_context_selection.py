"""Only current company-wide statements supply valuation facts."""

import zipfile
from datetime import date
from io import BytesIO

import pytest

from baibai_engine.market.providers.edinet_csv import parse_csv_zip_metric_record


def _parse(kind, rows, *, start=None, end=None, submitted=None):
    header = "要素ID\tコンテキストID\t連結・個別\tユニットID\t値\n"
    text = header + "\n".join(
        f"{element}\t{context}\t{basis}\tJPY\t{value}" for element, context, basis, value in rows
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("XBRL_TO_CSV/facts.csv", text.encode("utf-16-le"))
    return parse_csv_zip_metric_record(
        ticker="0000",
        doc_id="S100TEST",
        doc_type_code=kind,
        content=buffer.getvalue(),
        period_start=start,
        period_end=end,
        submit_datetime=submitted,
    )


@pytest.mark.parametrize(
    ("kind", "instant", "duration"),
    [
        ("120", "CurrentYearInstant", "CurrentYearDuration"),
        ("130", "CurrentYearInstant", "CurrentYearDuration"),
        ("160", "InterimInstant", "InterimDuration"),
        ("170", "InterimInstant", "InterimDuration"),
        ("140", "CurrentQuarterInstant", "CurrentYTDDuration"),
        ("150", "CurrentQuarterInstant", "CurrentYTDDuration"),
    ],
)
def test_document_period_not_context_lexical_order(kind, instant, duration):
    record = _parse(
        kind,
        [
            ("jppfs_cor:CashAndDeposits", instant, "連結", "100"),
            ("jppfs_cor:NetSales", duration, "連結", "200"),
            ("jppfs_cor:NetSales", "CurrentQuarterDuration", "連結", "999"),
            ("jppfs_cor:NetSales", "Prior1YearDuration", "連結", "888"),
            ("jppfs_cor:NetSales", f"{duration}_SegmentMember", "連結", "777"),
        ],
    )
    assert record.cash == 100
    assert record.sales_ttm == 200


@pytest.mark.parametrize(
    "context",
    [
        "Prior1YearInstant",
        "NextYearInstant",
        "CurrentYearInstant_SegmentMember",
    ],
)
def test_absent_current_balance_is_not_replaced_by_other_scope(context):
    record = _parse("120", [("jppfs_cor:CashAndDeposits", context, "連結", "900")])
    assert record.cash is None
    assert record.consolidation_basis is None


def test_other_csv_basis_label_does_not_hide_financial_statement_context():
    record = _parse(
        "120",
        [
            ("issuer:CashAndCashEquivalents", "CurrentYearInstant", "その他", "100"),
            (
                "issuer:CashAndCashEquivalents",
                "CurrentYearInstant_NonConsolidatedMember",
                "その他",
                "900",
            ),
        ],
    )
    assert record.cash == 100
    assert record.consolidation_basis == "consolidated"


def test_prior_consolidated_fact_does_not_hide_current_individual_statement():
    record = _parse(
        "120",
        [
            ("jppfs_cor:CashAndDeposits", "Prior1YearInstant", "連結", "900"),
            (
                "jppfs_cor:CashAndDeposits",
                "CurrentYearInstant_NonConsolidatedMember",
                "個別",
                "100",
            ),
        ],
    )
    assert record.cash == 100
    assert record.consolidation_basis == "non_consolidated"


@pytest.mark.parametrize("reverse", [False, True])
def test_cash_concept_preference_is_not_csv_row_order(reverse):
    rows = [
        ("jppfs_cor:CashAndCashEquivalents", "CurrentYearInstant", "連結", "200"),
        ("jppfs_cor:CashAndDeposits", "CurrentYearInstant", "連結", "100"),
    ]
    assert _parse("120", list(reversed(rows)) if reverse else rows).cash == 100


@pytest.mark.parametrize("reverse", [False, True])
def test_standard_depreciation_and_goodwill_are_distinct_addends(reverse):
    rows = [
        ("jppfs_cor:OperatingIncome", "CurrentYearDuration", "連結", "100"),
        ("jppfs_cor:DepreciationAndAmortizationOpeCF", "CurrentYearDuration", "連結", "30"),
        ("jppfs_cor:AmortizationOfGoodwillOpeCF", "CurrentYearDuration", "連結", "5"),
        ("issuer:DepreciationAndAmortization", "CurrentYearDuration", "その他", "900"),
        ("issuer:Depreciation", "CurrentYearDuration", "その他", "800"),
        ("issuer:AmortizationOfGoodwill", "CurrentYearDuration", "その他", "700"),
    ]
    record = _parse("120", list(reversed(rows)) if reverse else rows)
    assert record.depreciation_and_amortization_ttm == 35
    assert record.ebitda_ttm == 135


def test_unknown_lease_total_is_not_added_to_current_and_noncurrent_parts():
    record = _parse(
        "120",
        [
            ("jppfs_cor:LeaseObligationsCL", "CurrentYearInstant", "連結", "10"),
            ("jppfs_cor:LeaseObligationsNCL", "CurrentYearInstant", "連結", "20"),
            ("issuer:LeaseObligations", "CurrentYearInstant", "その他", "999"),
        ],
    )
    assert record.debt == 30


def test_goodwill_amortization_alone_does_not_establish_ebitda():
    record = _parse(
        "120",
        [
            ("jppfs_cor:OperatingIncome", "CurrentYearDuration", "連結", "100"),
            ("jppfs_cor:AmortizationOfGoodwillOpeCF", "CurrentYearDuration", "連結", "5"),
        ],
    )
    assert record.depreciation_and_amortization_ttm is None
    assert record.ebitda_ttm is None


@pytest.mark.parametrize(
    ("kind", "context", "start", "end", "quality"),
    [
        ("120", "CurrentYearDuration", date(2025, 4, 1), date(2026, 3, 31), "exact"),
        ("130", "CurrentYearDuration", date(2023, 4, 1), date(2024, 3, 31), "exact"),
        ("120", "CurrentYearDuration", date(2025, 7, 1), date(2026, 3, 31), "approximated"),
        ("120", "CurrentYearDuration", None, None, "approximated"),
        ("120", "CurrentYearDuration", date(2026, 4, 1), date(2026, 3, 31), "approximated"),
        ("160", "InterimDuration", date(2025, 4, 1), date(2026, 3, 31), "approximated"),
    ],
)
def test_flow_quality_requires_a_twelve_month_annual_period(kind, context, start, end, quality):
    record = _parse(
        kind,
        [
            ("jppfs_cor:NetSales", context, "連結", "200"),
            ("jppfs_cor:OperatingIncome", context, "連結", "100"),
            ("jppfs_cor:DepreciationAndAmortizationOpeCF", context, "連結", "30"),
        ],
        start=start,
        end=end,
    )
    assert record.sales_ttm == 200
    assert record.ebitda_ttm == 130
    assert record.ttm_quality_p_s == quality
    assert record.ttm_quality_ev_ebitda == quality


@pytest.mark.parametrize(
    ("submitted", "quality"),
    [
        ("2026-03-30 15:00", "approximated"),
        ("2026-03-31 15:00", "exact"),
        ("2026-03-30T16:00:00Z", "exact"),
        ("not-a-date", "approximated"),
    ],
)
def test_annual_metadata_cannot_end_after_known_submission(submitted, quality):
    record = _parse(
        "120",
        [
            ("jppfs_cor:NetSales", "CurrentYearDuration", "連結", "200"),
        ],
        start=date(2025, 4, 1),
        end=date(2026, 3, 31),
        submitted=submitted,
    )
    assert record.sales_ttm == 200
    assert record.source_submit_datetime == submitted
    assert record.ttm_quality_p_s == quality
