"""IFRS aggregate debt includes explicit leases, without inferred missing balances."""

import zipfile
from datetime import date
from io import BytesIO
from pathlib import Path

import pytest

from baibai_engine.machine_analysis.observations.financials.edinet_metrics.csv import (
    parse_csv_zip_metric_record,
)

_COMPONENTS = (
    "BondsAndBorrowingsCLIFRS",
    "BondsAndBorrowingsNCLIFRS",
    "LeaseLiabilitiesCLIFRS",
    "LeaseLiabilitiesNCLIFRS",
)


def _zip(text: str) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("facts.csv", text.encode("utf-16-le"))
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("doc_id", "ticker", "kind", "period_start", "period_end", "debt", "net_cash"),
    [
        ("S100YG7Z", "6803", "120", date(2025, 4, 1), date(2026, 3, 31), 3575000000, -1402000000),
        ("S100Z3FV", "6030", "120", date(2025, 7, 1), date(2026, 6, 30), 10424656000, 2839036000),
        (
            "S100ZCHE",
            "285A",
            "160",
            date(2026, 4, 1),
            date(2027, 3, 31),
            802729000000,
            594604000000,
        ),
    ],
)
def test_native_ifrs_debt_includes_current_and_noncurrent_leases(
    doc_id, ticker, kind, period_start, period_end, debt, net_cash
):
    fixture = Path(__file__).parents[4] / "fixtures" / "edinet" / f"{doc_id}.tsv"
    record = parse_csv_zip_metric_record(
        ticker=ticker,
        doc_id=doc_id,
        doc_type_code=kind,
        content=_zip(fixture.read_text()),
        period_start=period_start,
        period_end=period_end,
    )
    assert record.consolidation_basis == "consolidated"
    assert record.debt == debt
    assert record.net_cash == net_cash
    assert record.ttm_quality_net_cash == "exact"
    assert record.ttm_quality_pcfr == ("exact" if kind == "120" else "approximated")
    assert record.source_doc_id == doc_id
    # Fiscal-year metadata is retained, not relabelled as the interim BS date.
    assert record.source_period_end == period_end
    assert "metric_unavailable:debt" not in record.failure_reasons


def _parse(rows: list[tuple[str, str, str, str]]):
    text = "要素ID\tコンテキストID\tユニットID\t値\n"
    text += "jpdei_cor:AccountingStandardsDEI\tFilingDateInstant\t－\tIFRS\n"
    text += "jpdei_cor:WhetherConsolidatedFinancialStatementsArePreparedDEI\tFilingDateInstant\t－\ttrue\n"
    text += "jpigp_cor:CashAndCashEquivalentsIFRS\tCurrentYearInstant\tJPY\t1000\n"
    text += "\n".join("\t".join(row) for row in rows)
    return parse_csv_zip_metric_record(
        ticker="0000", doc_id="S100TEST", doc_type_code="120", content=_zip(text)
    )


def _components(value: str = "10") -> list[tuple[str, str, str, str]]:
    return [(f"jpigp_cor:{name}", "CurrentYearInstant", "JPY", value) for name in _COMPONENTS]


@pytest.mark.parametrize("component", range(4))
@pytest.mark.parametrize(
    "unusable",
    ["absent", "nil", "currency", "conflict", "prior", "individual", "segment", "issuer"],
)
def test_ifrs_debt_requires_all_four_usable_same_scope_components(component, unusable):
    rows = _components()
    tag, context, unit, value = rows.pop(component)
    replacements = {
        "nil": (tag, context, unit, "－"),
        "currency": (tag, context, "USD", value),
        "prior": (tag, "Prior1YearInstant", unit, value),
        "individual": (tag, f"{context}_NonConsolidatedMember", unit, value),
        "segment": (tag, f"{context}_SegmentMember", unit, value),
        "issuer": (tag.replace("jpigp_cor:", "issuer:"), context, unit, value),
    }
    if unusable == "conflict":
        rows.extend([(tag, context, unit, value), (tag, context, unit, "20")])
    elif unusable != "absent":
        rows.append(replacements[unusable])
    # Neither a narrower borrowing nor an unrelated financial-liability total
    # can repair a missing aggregate or a lease in the supported presentation.
    rows.extend(
        [
            ("jpigp_cor:BorrowingsCLIFRS", "CurrentYearInstant", "JPY", "10"),
            ("jpigp_cor:BorrowingsNCLIFRS", "CurrentYearInstant", "JPY", "10"),
            ("jpigp_cor:OtherFinancialLiabilitiesCLIFRS", "CurrentYearInstant", "JPY", "10"),
            ("jpigp_cor:OtherFinancialLiabilitiesNCLIFRS", "CurrentYearInstant", "JPY", "10"),
            ("jppfs_cor:ShortTermLoansPayable", "CurrentYearInstant", "JPY", "100"),
            ("jppfs_cor:CommercialPapersLiabilities", "CurrentYearInstant", "JPY", "100"),
            ("jppfs_cor:LeaseObligationsCL", "CurrentYearInstant", "JPY", "100"),
        ]
    )
    record = _parse(rows)
    assert record.debt is None
    assert record.net_cash is None
    assert record.ttm_quality_net_cash == "unavailable"
    assert "metric_unavailable:debt" in record.failure_reasons


@pytest.mark.parametrize("value", ["0", "-"])
def test_ifrs_debt_accepts_reported_zero_but_does_not_infer_it(value):
    record = _parse(_components(value))
    assert record.debt == 0
    assert record.net_cash == 1000


def test_ifrs_aggregate_is_not_added_to_duplicates_components_or_japanese_gaap():
    rows = _components() * 2
    rows.extend(
        [
            ("jpigp_cor:BorrowingsCLIFRS", "CurrentYearInstant", "JPY", "7"),
            ("jpigp_cor:BorrowingsNCLIFRS", "CurrentYearInstant", "JPY", "8"),
            ("jpigp_cor:BondsCLIFRS", "CurrentYearInstant", "JPY", "3"),
            ("jpigp_cor:BondsNCLIFRS", "CurrentYearInstant", "JPY", "2"),
            ("jppfs_cor:ShortTermLoansPayable", "CurrentYearInstant", "JPY", "500"),
            ("jppfs_cor:CommercialPapersLiabilities", "CurrentYearInstant", "JPY", "500"),
            ("jppfs_cor:LeaseObligationsNCL", "CurrentYearInstant", "JPY", "600"),
        ]
    )
    record = _parse(rows)
    assert record.debt == 40
    assert record.net_cash == 960
