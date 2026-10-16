from __future__ import annotations

import zipfile
from datetime import date
from io import BytesIO

import pytest

from baibai_engine.market.metric_quality import TTMQuality
from baibai_engine.market.providers.edinet_csv import parse_csv_zip_metric_record

_CB = "jppfs_cor:ConvertibleBondTypeBondsWithSubscriptionRightsToShares"


def _csv_zip(rows: list[tuple[str, str, str, str]]) -> bytes:
    # Synthetic values; real concept/context names and explicit JPY basis.
    header = "要素ID\tコンテキストID\t連結・個別\tユニットID\t単位\t値\n"
    text = header + "\n".join(
        f"{element}\t{context}\t{basis}\tJPY\t円\t{value}"
        for element, context, basis, value in rows
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("XBRL_TO_CSV/synthetic.csv", text.encode("utf-16"))
    return buffer.getvalue()


@pytest.mark.parametrize("total_debt", [None, "420"])
def test_convertible_debt_preserves_total_priority_and_context_deduplication(total_debt):
    rows = [
        ("jppfs_cor:CashAndDeposits", "CurrentYearInstant", "連結", "1000"),
        ("jppfs_cor:ShortTermLoansPayable", "CurrentYearInstant", "連結", "40"),
        ("jppfs_cor:BondsPayable", "CurrentYearInstant", "連結", "80"),
        ("jppfs_cor:LeaseObligationsCL", "CurrentYearInstant", "連結", "10"),
        ("jppfs_cor:LeaseObligationsNCL", "CurrentYearInstant", "連結", "20"),
        (_CB, "CurrentYearInstant", "連結", "120"),
        (_CB, "CurrentYearInstant", "連結", "120"),
        (_CB, "Prior1YearInstant", "連結", "700"),
        (_CB, "CurrentYearInstant_SegmentMember", "連結", "800"),
        (_CB, "CurrentYearInstant_NonConsolidatedMember", "個別", "900"),
        # Convertible bond financing proceeds are a flow, not another debt balance.
        (
            "issuer:ProceedsFromIssuanceOfConvertibleBondsWithShareAcquisitionRightsFinCF",
            "CurrentYearDuration",
            "連結",
            "600",
        ),
    ]
    if total_debt is not None:
        rows.append(("jppfs_cor:InterestBearingDebt", "CurrentYearInstant", "連結", total_debt))
    record = parse_csv_zip_metric_record(
        ticker="0000",
        doc_id="S100TEST",
        doc_type_code="120",
        content=_csv_zip(rows),
        period_start=date(2025, 4, 1),
        period_end=date(2026, 3, 31),
    )

    expected_debt = 270.0 if total_debt is None else 420.0
    assert record.debt == expected_debt
    assert record.net_cash == 1000.0 - expected_debt
    assert record.consolidation_basis == "consolidated"
    assert record.ttm_quality_net_cash is TTMQuality.EXACT
    assert record.source_period_end == date(2026, 3, 31)
    assert "metric_unavailable:debt" not in record.failure_reasons


@pytest.mark.parametrize("cb", [None, "0"])
def test_missing_convertible_debt_is_not_reported_zero(cb):
    rows = [("jppfs_cor:CashAndDeposits", "CurrentYearInstant", "連結", "1000")]
    if cb is not None:
        rows.append((_CB, "CurrentYearInstant", "連結", cb))
    record = parse_csv_zip_metric_record(
        ticker="0000", doc_id="S100TEST", doc_type_code="120", content=_csv_zip(rows)
    )

    assert record.debt == (None if cb is None else 0.0)
    assert record.net_cash == (None if cb is None else 1000.0)
    assert record.ttm_quality_net_cash is (
        TTMQuality.UNAVAILABLE if cb is None else TTMQuality.EXACT
    )


@pytest.mark.parametrize("basis", ["個別", "非連結", "non_consolidated"])
def test_non_consolidated_basis_is_not_matched_as_consolidated(basis):
    rows = [
        ("jppfs_cor:CashAndDeposits", "CurrentYearInstant", "連結", "1000"),
        (_CB, "CurrentYearInstant", "連結", "100"),
        ("jppfs_cor:CashAndDeposits", "CurrentYearInstant", basis, "9999"),
        (_CB, "CurrentYearInstant", basis, "900"),
    ]
    record = parse_csv_zip_metric_record(
        ticker="0000", doc_id="S100TEST", doc_type_code="120", content=_csv_zip(rows)
    )
    assert record.consolidation_basis == "consolidated"
    assert record.cash == 1000.0
    assert record.debt == 100.0


@pytest.mark.parametrize("basis", ["個別", "非連結", "non_consolidated"])
def test_only_non_consolidated_rows_keep_fallback_identity(basis):
    rows = [
        ("jppfs_cor:CashAndDeposits", "CurrentYearInstant", basis, "1000"),
        (_CB, "CurrentYearInstant", basis, "100"),
    ]
    record = parse_csv_zip_metric_record(
        ticker="0000", doc_id="S100TEST", doc_type_code="120", content=_csv_zip(rows)
    )
    assert record.consolidation_basis == "non_consolidated"
    assert record.net_cash == 900.0
    assert "non_consolidated_fallback" in record.failure_reasons
