"""Only current company-wide statements supply valuation facts."""

import zipfile
from datetime import date
from io import BytesIO

import pytest
from tests.engine.screening.metric_fixtures import _summary

from baibai_engine.market.edinet_metrics.csv import parse_csv_zip_metric_record
from baibai_engine.screening.rule_config import load_screening_rules
from baibai_engine.valuation.metrics.financial_snapshot import build_financial_snapshot

_CONSOLIDATION_DEI = "jpdei_cor:WhetherConsolidatedFinancialStatementsArePreparedDEI"


def _parse(
    kind,
    rows,
    *,
    start=None,
    end=None,
    submitted=None,
    dei_rows=(),
    ticker="0000",
    doc_id="S100TEST",
):
    header = "要素ID\tコンテキストID\t連結・個別\tユニットID\t値\n"
    facts = [
        f"{element}\t{context}\t{basis}\tJPY\t{value}" for element, context, basis, value in rows
    ]
    facts.extend(
        f"{element}\t{context}\tその他\t{unit}\t{value}"
        for element, context, unit, value in dei_rows
    )
    text = header + "\n".join(facts)
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("XBRL_TO_CSV/facts.csv", text.encode("utf-16-le"))
    return parse_csv_zip_metric_record(
        ticker=ticker,
        doc_id=doc_id,
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


def test_supported_ifrs_assets_do_not_allow_individual_revenue_fallback():
    record = _parse(
        "120",
        [
            ("jpigp_cor:Revenue2IFRS", "CurrentYearDuration", "連結", "53449000000"),
            ("jpigp_cor:AssetsIFRS", "CurrentYearInstant", "連結", "37686000000"),
            (
                "jppfs_cor:NetSales",
                "CurrentYearDuration_NonConsolidatedMember",
                "個別",
                "35372000000",
            ),
            (
                "jppfs_cor:Assets",
                "CurrentYearInstant_NonConsolidatedMember",
                "個別",
                "46872000000",
            ),
        ],
        dei_rows=[(_CONSOLIDATION_DEI, "FilingDateInstant", "－", "true")],
    )
    assert record.consolidation_basis == "consolidated"
    assert record.sales_ttm is None
    assert record.total_assets == 37_686_000_000
    assert "consolidated_dei_no_supported_current_facts" not in record.failure_reasons
    assert "metric_unavailable:sales" in record.failure_reasons
    assert "non_consolidated_fallback" not in record.failure_reasons


@pytest.mark.parametrize(
    ("standards", "expected", "reason"),
    [
        (["IFRS"], 200, None),
        (["Japan GAAP"], 100, None),
        (["IFRS", "IFRS"], 200, None),
        (["IFRS", "Japan GAAP"], None, "accounting_standard_dei_conflict"),
        ([""], None, "accounting_standard_dei_invalid"),
        (["unsupported"], None, "accounting_standard_dei_unsupported"),
        ([], None, "accounting_standard_dei_missing_for_mixed_facts"),
    ],
)
def test_accounting_standard_is_selected_before_aliases_or_amounts_are_combined(
    standards, expected, reason
):
    record = _parse(
        "120",
        [
            ("jppfs_cor:Assets", "CurrentYearInstant", "連結", "100"),
            ("jpigp_cor:AssetsIFRS", "CurrentYearInstant", "連結", "200"),
        ],
        dei_rows=[
            ("jpdei_cor:AccountingStandardsDEI", "FilingDateInstant", "－", value)
            for value in standards
        ],
    )
    assert record.total_assets == expected
    if reason is not None:
        assert reason in record.failure_reasons


def test_consolidated_dei_uses_supported_consolidated_facts():
    record = _parse(
        "120",
        [
            ("jppfs_cor:NetSales", "CurrentYearDuration", "連結", "200"),
            (
                "jppfs_cor:NetSales",
                "CurrentYearDuration_NonConsolidatedMember",
                "個別",
                "900",
            ),
            ("jppfs_cor:Assets", "CurrentYearInstant_NonConsolidatedMember", "個別", "900"),
        ],
        dei_rows=[(_CONSOLIDATION_DEI, "FilingDateInstant", "－", "true")],
    )
    assert record.consolidation_basis == "consolidated"
    assert record.sales_ttm == 200
    assert record.total_assets is None
    assert "consolidated_dei_no_supported_current_facts" not in record.failure_reasons


def test_1375_supported_assets_do_not_adopt_individual_cash_or_debt():
    # S100YK0W の原本にある科目・値。単体総資産はこの J-Quants fixture と2倍以内なので、
    # 旧 parser の単体 fallback は既存の規模 gate を通ってしまった。
    individual = "CurrentYearInstant_NonConsolidatedMember"
    record = _parse(
        "120",
        [
            ("jpigp_cor:AssetsIFRS", "CurrentYearInstant", "連結", "37686000000"),
            ("jpigp_cor:Revenue2IFRS", "CurrentYearDuration", "連結", "53449000000"),
            ("jppfs_cor:CashAndDeposits", individual, "個別", "3778000000"),
            ("jppfs_cor:Assets", individual, "個別", "46872000000"),
            ("jppfs_cor:CurrentPortionOfLongTermLoansPayable", individual, "個別", "4431000000"),
            ("jppfs_cor:LeaseObligationsCL", individual, "個別", "4000000"),
            ("jppfs_cor:LongTermLoansPayable", individual, "個別", "11029000000"),
            ("jppfs_cor:LeaseObligationsNCL", individual, "個別", "14000000"),
        ],
        start=date(2025, 4, 1),
        end=date(2026, 3, 31),
        submitted="2026-06-25 15:30",
        dei_rows=[(_CONSOLIDATION_DEI, "FilingDateInstant", "－", "true")],
        ticker="1375",
        doc_id="S100YK0W",
    )
    financial = build_financial_snapshot(
        latest_price=120.0,
        summaries=[
            _summary(
                "1375",
                date(2026, 5, 15),
                fiscal_period="FY",
                period_start=date(2025, 4, 1),
                period_end=date(2026, 3, 31),
                shares_outstanding=100_000_000.0,
                total_assets=37_686_000_000.0,
                sales=53_449_000_000.0,
                eps_ttm=20.0,
                forecast_eps=25.0,
            )
        ],
        edinet=record,
        ttm_rules=load_screening_rules().ttm,
        adjustment_events=(),
        asof_date=date(2026, 7, 1),
    )
    assert record.consolidation_basis == "consolidated"
    assert record.net_cash is None
    assert financial.edinet_source_doc_id == "S100YK0W"
    assert financial.consolidation_basis == "consolidated"
    assert financial.cash is None
    assert financial.debt is None
    assert financial.net_cash is None
    assert financial.net_cash_to_market_cap is None
    assert financial.ev_ebitda is None
    assert financial.per_trailing is not None
    assert financial.per_forward is not None
    assert financial.p_s is not None
    assert financial.edinet_failure_reasons is not None
    assert "metric_unavailable:cash" in financial.edinet_failure_reasons


@pytest.mark.parametrize(
    ("dei_rows", "basis", "sales", "reason"),
    [
        (
            [(_CONSOLIDATION_DEI, "FilingDateInstant", "－", "true")],
            None,
            None,
            "consolidated_dei_no_supported_current_facts",
        ),
        (
            [(_CONSOLIDATION_DEI, "FilingDateInstant", "－", "true")] * 2,
            None,
            None,
            "consolidated_dei_no_supported_current_facts",
        ),
        ([(_CONSOLIDATION_DEI, "FilingDateInstant", "－", "false")], "non_consolidated", 100, None),
        (
            [(_CONSOLIDATION_DEI, "FilingDateInstant", "－", "false")] * 2,
            "non_consolidated",
            100,
            None,
        ),
        (
            [
                (_CONSOLIDATION_DEI, "FilingDateInstant", "－", "true"),
                (_CONSOLIDATION_DEI, "FilingDateInstant", "－", "false"),
            ],
            None,
            None,
            "consolidation_dei_conflict",
        ),
        (
            [(_CONSOLIDATION_DEI, "FilingDateInstant", "－", "TRUE")],
            None,
            None,
            "consolidation_dei_invalid",
        ),
        (
            [(_CONSOLIDATION_DEI, "FilingDateInstant", "JPY", "true")],
            None,
            None,
            "consolidation_dei_invalid",
        ),
        ([], "non_consolidated", 100, "consolidation_dei_missing"),
        (
            [(_CONSOLIDATION_DEI, "CurrentYearInstant", "－", "true")],
            "non_consolidated",
            100,
            "consolidation_dei_missing",
        ),
        (
            [(_CONSOLIDATION_DEI + "Extra", "FilingDateInstant", "－", "true")],
            "non_consolidated",
            100,
            "consolidation_dei_missing",
        ),
    ],
)
def test_dei_values_are_exact_and_missing_or_duplicate_status_is_visible(
    dei_rows, basis, sales, reason
):
    record = _parse(
        "120",
        [("jppfs_cor:NetSales", "CurrentYearDuration_NonConsolidatedMember", "個別", "100")],
        dei_rows=dei_rows,
    )
    assert record.consolidation_basis == basis
    assert record.sales_ttm == sales
    if reason is not None:
        assert reason in record.failure_reasons
    else:
        assert not any(item.startswith("consolidation_dei_") for item in record.failure_reasons)


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
