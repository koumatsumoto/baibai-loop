"""Unsupported current debt is not silently omitted from exact net cash (#2196)."""

import zipfile
from dataclasses import replace
from datetime import date
from io import BytesIO

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
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.financials.metric_quality import TTMQuality

UNSUPPORTED = ("BorrowedMoneyLiabilitiesBNK", "ShortTermBondsPayable")


def parse(
    rows, *, kind="120", instant="CurrentYearInstant", consolidated=True, standard="Japan GAAP"
):
    suffix = "" if consolidated else "_NonConsolidatedMember"
    rows = [
        ("jpdei_cor:AccountingStandardsDEI", "FilingDateInstant", "－", standard),
        (
            "jpdei_cor:WhetherConsolidatedFinancialStatementsArePreparedDEI",
            "FilingDateInstant",
            "－",
            "true" if consolidated else "false",
        ),
        ("jppfs_cor:CashAndCashEquivalents", f"{instant}{suffix}", "JPY", "90045500000000"),
        ("jppfs_cor:CommercialPapersLiabilities", f"{instant}{suffix}", "JPY", "3421893000000"),
        ("jppfs_cor:BondsPayable", f"{instant}{suffix}", "JPY", "15790570000000"),
        *rows,
    ]
    text = "要素ID\tコンテキストID\tユニットID\t値\n" + "\n".join("\t".join(r) for r in rows)
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("facts.csv", text.encode("utf-16-le"))
    return parse_csv_zip_metric_record(
        ticker="8306",
        doc_id="S100TEST",
        doc_type_code=kind,
        content=buffer.getvalue(),
        submit_datetime="2026-06-24 15:39",
        period_start=date(2025, 4, 1),
        period_end=date(2026, 3, 31),
    )


def test_public_bank_balances_do_not_become_a_complete_debt_subtotal():
    # MUFG FY2026 company iXBRL, CurrentYearInstant = 2026-03-31, scale=6.
    # https://www.mufg.jp/dam/ir/report/security_report/zip/mufg_202603_xbrl.zip
    # Public balances projected to native CSV columns, not a retained type=5 ZIP.
    record = parse(
        [
            ("jppfs_cor:BorrowedMoneyLiabilitiesBNK", "CurrentYearInstant", "JPY", "9359997000000"),
            ("jppfs_cor:ShortTermBondsPayable", "CurrentYearInstant", "JPY", "1217464000000"),
        ]
    )
    assert record.cash == 90_045_500_000_000
    assert record.debt is None
    assert record.net_cash is None
    assert record.ttm_quality_net_cash == "unavailable"
    for tag in UNSUPPORTED:
        assert f"unsupported_debt_component:jppfs_cor:{tag}" in record.failure_reasons
    assert "metric_unavailable:debt" in record.failure_reasons
    # Synthetic, nonmissing ratio denominators prove propagation rather than a
    # ratio that was null already. They are not MUFG EBITDA or market value.
    record = replace(record, ebitda_ttm=100.0, ttm_quality_ev_ebitda=TTMQuality.EXACT)
    summary = JQuantsFinancialSummary(
        ticker="8306",
        disclosed_at=date(2026, 5, 15),
        fiscal_period="FY",
        fiscal_year_end=date(2026, 3, 31),
        period_start=date(2025, 4, 1),
        period_end=date(2026, 3, 31),
        document_type="FYFinancialStatements_Consolidated_JP",
        shares_outstanding=100.0,
        treasury_shares=0.0,
    )
    snapshot = build_financial_snapshot(
        1e12,
        [summary],
        record,
        DEFAULT_TTM_RULES,
        adjustment_events=(),
        asof_date=date(2026, 11, 13),
    )
    payload = financial_observation_payload(snapshot, record)
    assert payload["market_cap"] == 1e14
    assert payload["edinet_reported_flows"]["amounts"]["ebitda"] == 100.0
    assert payload["debt"] is None
    assert payload["net_cash"] is None
    assert payload["net_cash_to_market_cap"] is None
    assert payload["ev_ebitda"] is None
    balances = payload["edinet_reported_balances"]
    assert balances["amounts"]["cash"] == record.cash
    assert balances["amounts"]["debt"] is None
    assert balances["amounts"]["net_cash"] is None
    assert (
        "unsupported_debt_component:jppfs_cor:BorrowedMoneyLiabilitiesBNK"
        in balances["failure_reasons"]
    )


@pytest.mark.parametrize("tag", UNSUPPORTED)
@pytest.mark.parametrize(
    ("values", "unit"),
    [(["10"], "JPY"), (["-10"], "JPY"), (["－"], "JPY"), (["10", "20"], "JPY"), (["0"], "USD")],
)
@pytest.mark.parametrize(
    ("kind", "instant"),
    [("120", "CurrentYearInstant"), ("160", "InterimInstant"), ("140", "CurrentQuarterInstant")],
)
def test_nonzero_or_unresolved_selected_balance_blocks_partial_debt(
    tag, values, unit, kind, instant
):
    record = parse(
        [(f"jppfs_cor:{tag}", instant, unit, value) for value in values], kind=kind, instant=instant
    )
    assert record.debt is None
    assert record.net_cash is None
    assert record.ttm_quality_net_cash == "unavailable"
    assert f"unsupported_debt_component:jppfs_cor:{tag}" in record.failure_reasons


@pytest.mark.parametrize("tag", UNSUPPORTED)
@pytest.mark.parametrize("value", ["0", "-"])
def test_explicit_zero_is_not_an_unobserved_additional_balance(tag, value):
    record = parse([(f"jppfs_cor:{tag}", "CurrentYearInstant", "JPY", value)])
    assert record.debt == 19_212_463_000_000
    assert record.net_cash == 70_833_037_000_000
    assert not any(
        reason.startswith("unsupported_debt_component:") for reason in record.failure_reasons
    )


@pytest.mark.parametrize("tag", UNSUPPORTED)
@pytest.mark.parametrize(
    "context",
    [
        "Prior1YearInstant",
        "CurrentYearInstant_NonConsolidatedMember",
        "CurrentYearInstant_SegmentMember",
        "CurrentYearDuration",
    ],
)
def test_nonselected_scope_or_duration_cannot_block_selected_balance(tag, context):
    record = parse([(f"jppfs_cor:{tag}", context, "JPY", "10")])
    assert record.debt == 19_212_463_000_000
    assert not any(
        reason.startswith("unsupported_debt_component:") for reason in record.failure_reasons
    )


@pytest.mark.parametrize("tag", UNSUPPORTED)
def test_nonconsolidated_selected_balance_also_blocks_partial_debt(tag):
    record = parse(
        [(f"jppfs_cor:{tag}", "CurrentYearInstant_NonConsolidatedMember", "JPY", "10")],
        consolidated=False,
    )
    assert record.consolidation_basis == "non_consolidated"
    assert record.debt is None
    assert record.net_cash is None


def test_issuer_homonyms_and_cash_flow_changes_are_not_balance_components():
    record = parse(
        [(f"issuer:{tag}", "CurrentYearInstant", "JPY", "10") for tag in UNSUPPORTED]
        + [
            (
                "jppfs_cor:NetIncreaseDecreaseInBorrowedMoneyOpeCF",
                "CurrentYearDuration",
                "JPY",
                "10",
            )
        ]
    )
    assert record.debt == 19_212_463_000_000


def test_declared_ifrs_ignores_japanese_gaap_reference_balances():
    rows = [(f"jppfs_cor:{tag}", "CurrentYearInstant", "JPY", "10") for tag in UNSUPPORTED]
    rows += [
        (f"jpigp_cor:{tag}", "CurrentYearInstant", "JPY", "20")
        for tag in [
            "BondsAndBorrowingsCLIFRS",
            "BondsAndBorrowingsNCLIFRS",
            "LeaseLiabilitiesCLIFRS",
            "LeaseLiabilitiesNCLIFRS",
        ]
    ]
    rows.append(("jpigp_cor:CashAndCashEquivalentsIFRS", "CurrentYearInstant", "JPY", "100"))
    record = parse(rows, standard="IFRS")
    assert record.debt == 80
    assert record.net_cash == 20
    assert record.ttm_quality_net_cash == "exact"
