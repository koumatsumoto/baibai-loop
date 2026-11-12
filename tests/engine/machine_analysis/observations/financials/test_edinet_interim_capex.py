"""中間の部分取得支出を、完全FCF・年次・別scopeへ昇格せず保持する。"""

import zipfile
from datetime import date
from io import BytesIO
from pathlib import Path

import pytest

from baibai_engine.machine_analysis.models.company_financials.reported_flows import (
    _reported_flows,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.csv import (
    parse_csv_zip_metric_record,
)

PPE = "jppfs_cor:PurchaseOfPropertyPlantAndEquipmentInvCF"
INTANGIBLE = "jppfs_cor:PurchaseOfIntangibleAssetsInvCF"
OCF = "jppfs_cor:NetCashProvidedByUsedInOperatingActivities"
CONSOLIDATED = "jpdei_cor:WhetherConsolidatedFinancialStatementsArePreparedDEI"
STANDARD = "jpdei_cor:AccountingStandardsDEI"


def zipped(text):
    body = BytesIO()
    with zipfile.ZipFile(body, "w") as archive:
        archive.writestr("facts.csv", text.encode("utf-16"))
    return body.getvalue()


def parse(rows, *, kind="160", consolidated="true", standard="Japan GAAP"):
    context = "InterimDuration" if kind in {"160", "170"} else "CurrentYTDDuration"
    text = "要素ID\tコンテキストID\tユニットID\t値\n" + "\n".join(
        "\t".join(row)
        for row in [
            (CONSOLIDATED, "FilingDateInstant", "－", consolidated),
            (STANDARD, "FilingDateInstant", "－", standard),
            (OCF, context, "JPY", "100"),
            *rows,
        ]
    )
    return parse_csv_zip_metric_record(
        ticker="0000", doc_id="S100TEST", doc_type_code=kind, content=zipped(text)
    )


@pytest.mark.parametrize(
    ("doc", "ticker", "start", "end", "basis", "ocf", "ppe"),
    [
        (
            "S100Z12G",
            "2776",
            date(2026, 2, 1),
            date(2027, 1, 31),
            "consolidated",
            605_834_000,
            715_528_000,
        ),
        (
            "S100ZC1D",
            "9063",
            date(2026, 4, 1),
            date(2027, 3, 31),
            "consolidated",
            966_008_000,
            976_034_000,
        ),
        (
            "S100Y2OX",
            "3494",
            date(2025, 10, 1),
            date(2026, 9, 30),
            "non_consolidated",
            -32_809_000,
            1_233_089_000,
        ),
    ],
)
@pytest.mark.parametrize("kind", ["160", "170"])
def test_native_interim_parts_keep_period_scope_and_unknown_total(
    doc, ticker, start, end, basis, ocf, ppe, kind
):
    fixture = Path(__file__).parents[4] / "fixtures" / "edinet" / f"{doc}.tsv"
    record = parse_csv_zip_metric_record(
        ticker=ticker,
        doc_id=doc,
        doc_type_code=kind,
        content=zipped(fixture.read_text()),
        period_start=start,
        period_end=end,
    )
    assert record.consolidation_basis == basis
    assert record.ocf_ttm == ocf
    assert record.capex_ppe_reported == ppe
    assert record.capex_intangible_reported is None
    assert record.capex_ttm is None
    assert record.fcf_ttm is None
    assert record.capex_source is None
    assert record.ttm_quality_fcf == "unavailable"
    assert record.ttm_quality_pcfr == "approximated"
    assert record.ocf_inventories_cash_effect is None
    flows = _reported_flows(record)
    assert flows["period_kind"] == "interim"
    assert flows["amount_unit"] == "JPY"
    assert flows["consolidation_basis"] == basis
    assert flows["amounts"]["capex_ppe_reported"] == ppe
    assert flows["amounts"]["free_cash_flow"] is None
    assert flows["measurement_period"] == {
        "start": None,
        "end": None,
        "reason": "fact_dates_not_retained",
    }
    assert flows["filing_fiscal_period"] == {"start": start, "end": end}


@pytest.mark.parametrize(
    ("element", "context", "unit", "value"),
    [
        (PPE, "Prior1InterimDuration", "JPY", "-10"),
        (PPE, "CurrentYearDuration", "JPY", "-10"),
        (PPE, "InterimDuration_NonConsolidatedMember", "JPY", "-10"),
        (PPE, "InterimDuration_SegmentMember", "JPY", "-10"),
        (PPE, "InterimDuration", "USD", "-10"),
        (PPE, "InterimDuration", "JPY", "－"),
        (PPE, "InterimDuration", "JPY", ""),
        (PPE.replace("jppfs_cor:", "issuer:"), "InterimDuration", "JPY", "-10"),
        ("jppfs_cor:PurchaseOfSoftwareInvCF", "InterimDuration", "JPY", "-10"),
        ("jppfs_cor:PurchaseOfNoncurrentAssetsInvCF", "InterimDuration", "JPY", "-10"),
    ],
)
def test_interim_parts_do_not_borrow_or_broaden(element, context, unit, value):
    record = parse([(element, context, unit, value)])
    assert record.capex_ppe_reported is None
    assert record.capex_intangible_reported is None
    assert record.capex_ttm is None
    assert record.fcf_ttm is None


@pytest.mark.parametrize(
    ("values", "expected"),
    [(["-10", "-10"], 10), (["-10", "-11"], None), (["-10", "－"], None), (["-"], 0)],
)
def test_interim_component_duplicates_and_zero_remain_explicit(values, expected):
    record = parse([(PPE, "InterimDuration", "JPY", value) for value in values])
    assert record.capex_ppe_reported == expected
    assert record.capex_intangible_reported is None
    assert record.fcf_ttm is None


@pytest.mark.parametrize("kind", ["140", "150"])
def test_quarter_details_are_not_expanded(kind):
    record = parse([(PPE, "CurrentYTDDuration", "JPY", "-10")], kind=kind)
    assert record.capex_ppe_reported is None


@pytest.mark.parametrize("combined", [False, True])
def test_ifrs_parts_do_not_double_count_total_or_upgrade_interim(combined):
    rows = [
        (
            "jpigp_cor:NetCashProvidedByUsedInOperatingActivitiesIFRS",
            "InterimDuration",
            "JPY",
            "100",
        ),
        ("jpigp_cor:PurchaseOfPropertyPlantAndEquipmentInvCFIFRS", "InterimDuration", "JPY", "-80"),
        ("jpigp_cor:PurchaseOfIntangibleAssetsInvCFIFRS", "InterimDuration", "JPY", "-5"),
        (PPE, "InterimDuration", "JPY", "-999"),
    ]
    if combined:
        rows.append(
            (
                "jpigp_cor:PurchaseOfPropertyPlantAndEquipmentAndIntangibleAssetsInvCFIFRS",
                "InterimDuration",
                "JPY",
                "-90",
            )
        )
    record = parse(rows, standard="IFRS")
    assert (record.capex_ppe_reported, record.capex_intangible_reported) == (80, 5)
    assert record.capex_ttm == (90 if combined else 85)
    assert record.fcf_ttm == (10 if combined else 15)
    assert record.ttm_quality_fcf == "approximated"
    assert record.ocf_inventories_cash_effect is None
