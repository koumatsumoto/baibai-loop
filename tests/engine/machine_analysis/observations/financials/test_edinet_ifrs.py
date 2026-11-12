"""Native standard IFRS facts, without issuer-specific or standalone substitutes."""

import zipfile
from datetime import date
from io import BytesIO
from pathlib import Path

import pytest

from baibai_engine.machine_analysis.observations.financials.edinet_metrics.csv import (
    parse_csv_zip_metric_record,
)


@pytest.mark.parametrize(
    ("doc_id", "ticker", "kind", "year", "sales", "ocf", "cash", "assets", "capex"),
    [
        (
            "S100YV6Y",
            "1605",
            "160",
            2026,
            1000495000000,
            553888000000,
            216190000000,
            8386904000000,
            None,
        ),
        (
            "S100YTCC",
            "2914",
            "160",
            2026,
            1986070000000,
            331329000000,
            827909000000,
            8669953000000,
            60400000000,
        ),
        (
            "S100XU9L",
            "1605",
            "120",
            2025,
            2011351000000,
            693893000000,
            168407000000,
            7735198000000,
            None,
        ),
    ],
)
def test_native_ifrs_facts_keep_scope_and_period(
    doc_id, ticker, kind, year, sales, ocf, cash, assets, capex
):
    fixture = Path(__file__).parents[4] / "fixtures" / "edinet" / f"{doc_id}.tsv"
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("XBRL_TO_CSV/facts.csv", fixture.read_text().encode("utf-16-le"))
    record = parse_csv_zip_metric_record(
        ticker=ticker,
        doc_id=doc_id,
        doc_type_code=kind,
        content=buffer.getvalue(),
        period_start=date(year, 1, 1),
        # EDINET interim document metadata describes the fiscal year, not the
        # six-month fact. It must not upgrade that flow to twelve-month exact.
        period_end=date(year, 12, 31),
    )
    assert record.consolidation_basis == "consolidated"
    assert record.sales_ttm == sales
    assert record.ocf_ttm == ocf
    assert record.cash == cash
    assert record.total_assets == assets
    assert record.capex_ttm == capex
    assert record.fcf_ttm == (ocf - capex if capex is not None else None)
    assert record.ttm_quality_pcfr == ("exact" if kind == "120" else "approximated")
    assert record.debt is None
    assert record.net_cash is None
    assert record.investment_securities is None
    assert "consolidated_dei_no_supported_current_facts" not in record.failure_reasons
    assert "metric_unavailable:debt" in record.failure_reasons
    assert record.source_doc_id == doc_id
    assert record.source_period_end == date(year, 12, 31)


@pytest.mark.parametrize(
    ("doc_id", "ticker", "ocf", "assets", "equity", "operating_profit", "depreciation"),
    [
        ("S100YRSR", "436A", 1718237000, 7154218000, 4244058000, 1500178000, 357693000),
        ("S100Y6VZ", "4494", 615028000, 7791276000, 6270144000, 561509000, 204281000),
    ],
)
def test_native_mixed_standard_filing_uses_declared_ifrs_not_japanese_gaap(
    doc_id, ticker, ocf, assets, equity, operating_profit, depreciation
):
    fixture = Path(__file__).parents[4] / "fixtures" / "edinet" / f"{doc_id}.tsv"
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("facts.csv", fixture.read_text().encode("utf-16-le"))
    record = parse_csv_zip_metric_record(
        ticker=ticker, doc_id=doc_id, doc_type_code="120", content=buffer.getvalue()
    )
    assert record.consolidation_basis == "non_consolidated"
    assert record.ocf_ttm == ocf
    assert record.total_assets == assets
    assert record.equity == equity
    assert record.operating_profit_ttm == operating_profit
    assert record.depreciation_and_amortization_ttm == depreciation
    assert record.ebitda_ttm == operating_profit + depreciation
    assert record.debt is None
    assert record.net_cash is None


def test_native_ifrs_combined_capex_keeps_annual_period_and_missing_components():
    fixture = Path(__file__).parents[4] / "fixtures" / "edinet" / "S100YG7Z.tsv"
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("facts.csv", fixture.read_text().encode("utf-16-le"))
    record = parse_csv_zip_metric_record(
        ticker="6803",
        doc_id="S100YG7Z",
        doc_type_code="120",
        content=buffer.getvalue(),
        period_start=date(2025, 4, 1),
        period_end=date(2026, 3, 31),
        submit_datetime="2026-06-25 13:08",
    )
    assert record.consolidation_basis == "consolidated"
    assert (record.ocf_ttm, record.capex_ttm, record.fcf_ttm) == (
        1_610_000_000,
        151_000_000,
        1_459_000_000,
    )
    assert record.ttm_quality_fcf == "exact"
    assert record.capex_source == "purchase_of_fixed_assets"
    assert record.capex_ppe_reported is None
    assert record.capex_intangible_reported is None
    assert "metric_unavailable:capex" not in record.failure_reasons
    assert record.source_doc_id == "S100YG7Z"
    assert record.source_submit_datetime == "2026-06-25 13:08"
    assert record.source_period_start == date(2025, 4, 1)
    assert record.source_period_end == date(2026, 3, 31)


def test_ifrs_combined_capex_non_jpy_total_does_not_fall_back_to_components():
    prefix = "jpigp_cor:PurchaseOf"
    text = (
        "要素ID\tコンテキストID\tユニットID\t値\n"
        f"{prefix}PropertyPlantAndEquipmentAndIntangibleAssetsInvCFIFRS\tCurrentYearDuration\tUSD\t-151\n"
        f"{prefix}PropertyPlantAndEquipmentInvCFIFRS\tCurrentYearDuration\tJPY\t-100\n"
        f"{prefix}IntangibleAssetsInvCFIFRS\tCurrentYearDuration\tJPY\t-51\n"
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("facts.csv", text.encode("utf-16-le"))
    record = parse_csv_zip_metric_record(
        ticker="0000", doc_id="S100TEST", doc_type_code="120", content=buffer.getvalue()
    )
    assert record.capex_ttm is None
    assert record.fcf_ttm is None


@pytest.mark.parametrize("namespace", ["issuer", "other", "JPIGP_COR"])
@pytest.mark.parametrize(
    "concept", ["RevenueIFRS", "PurchaseOfPropertyPlantAndEquipmentAndIntangibleAssetsInvCFIFRS"]
)
def test_ifrs_aliases_do_not_guess_issuer_or_other_namespace(namespace, concept):
    text = (
        "要素ID\tコンテキストID\tユニットID\t値\n"
        f"{namespace}:{concept}\tInterimDuration\tJPY\t999\n"
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("facts.csv", text.encode("utf-16-le"))
    record = parse_csv_zip_metric_record(
        ticker="0000", doc_id="S100TEST", doc_type_code="160", content=buffer.getvalue()
    )
    assert record.sales_ttm is None
    assert record.capex_ttm is None
    assert record.consolidation_basis is None
