"""Financing cash details are observations, never a common-equity FCF rewrite."""

import zipfile
from dataclasses import asdict
from datetime import date
from io import BytesIO

import pytest

from baibai_engine.machine_analysis.models.company_financials.reported_flows import _reported_flows
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.csv import (
    parse_csv_zip_metric_record,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.service import (
    _metric_record_payload,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.store import (
    read_edinet_metrics,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.write import (
    store_edinet_metrics,
)

CONSOLIDATED = "jpdei_cor:WhetherConsolidatedFinancialStatementsArePreparedDEI"
STANDARD = "jpdei_cor:AccountingStandardsDEI"
OCF = "jppfs_cor:NetCashProvidedByUsedInOperatingActivities"


def zipped(text):
    out = BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("facts.csv", text.encode("utf-16"))
    return out.getvalue()


LEASE = "jppfs_cor:RepaymentsOfLeaseObligationsFinCF"
NCI = "jppfs_cor:DividendsPaidToNonControllingInterestsFinCF"
FIELDS = (
    "financing_lease_repayments_cash_effect",
    "financing_noncontrolling_dividends_cash_effect",
)


def parse(
    rows=(),
    *,
    kind="160",
    context="InterimDuration",
    consolidated="true",
    standard="Japan GAAP",
    core=True,
):
    data = [
        (CONSOLIDATED, "FilingDateInstant", "－", consolidated),
        (STANDARD, "FilingDateInstant", "－", standard),
    ]
    if core:
        data.extend(
            [
                (OCF, context, "JPY", "100"),
                (
                    "jppfs_cor:PurchaseOfPropertyPlantAndEquipmentAndIntangibleAssetsInvCF",
                    context,
                    "JPY",
                    "-20",
                ),
            ]
        )
    data.extend(rows)
    return parse_csv_zip_metric_record(
        ticker="0000",
        doc_id="S100TEST",
        doc_type_code=kind,
        content=zipped(
            "要素ID\tコンテキストID\tユニットID\t値\n" + "\n".join("\t".join(row) for row in data)
        ),
    )


@pytest.mark.parametrize(
    ("kind", "context"),
    [
        ("120", "CurrentYearDuration"),
        ("130", "CurrentYearDuration"),
        ("160", "InterimDuration"),
        ("170", "InterimDuration"),
    ],
)
def test_cash_effects_do_not_change_any_previous_metric(kind, context):
    before = parse(kind=kind, context=context)
    after = parse(
        [(LEASE, context, "JPY", "-12"), (NCI, context, "JPY", "-3")], kind=kind, context=context
    )
    assert {k: v for k, v in asdict(before).items() if k not in FIELDS} == {
        k: v for k, v in asdict(after).items() if k not in FIELDS
    }
    assert (after.fcf_ttm, after.capex_ttm) == (80, 20)
    assert tuple(getattr(after, k) for k in FIELDS) == (-12, -3)
    flows = _reported_flows(after)
    assert flows["amounts"]["free_cash_flow"] == 80
    assert flows["measurement_period"]["reason"] == "fact_dates_not_retained"
    assert all(flows["amounts"][k] == getattr(after, k) for k in FIELDS)


@pytest.mark.parametrize(
    ("tag", "context", "unit", "value"),
    [
        (LEASE, "Prior1InterimDuration", "JPY", "-12"),
        (LEASE, "CurrentYearDuration", "JPY", "-12"),
        (LEASE, "InterimInstant", "JPY", "-12"),
        (LEASE, "InterimDuration_NonConsolidatedMember", "JPY", "-12"),
        (LEASE, "InterimDuration_SegmentMember", "JPY", "-12"),
        (LEASE, "InterimDuration", "USD", "-12"),
        (LEASE, "InterimDuration", "JPY", "－"),
        ("issuer:RepaymentsOfLeaseObligationsFinCF", "InterimDuration", "JPY", "-12"),
        ("jpigp_cor:RepaymentsOfLeaseLiabilitiesFinCFIFRS", "InterimDuration", "JPY", "-12"),
    ],
)
def test_no_cross_scope_currency_balance_or_guessed_alias(tag, context, unit, value):
    rec = parse([(tag, context, unit, value)])
    assert all(getattr(rec, k) is None for k in FIELDS)


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (["-12", "-12"], -12),
        (["-12", "-13"], None),
        (["-12", "－"], None),
        (["-"], 0),
        (["12"], 12),
        (["0"], 0),
    ],
)
def test_sign_zero_and_conflict_are_preserved(values, expected):
    rec = parse([(LEASE, "InterimDuration", "JPY", v) for v in values])
    assert getattr(rec, FIELDS[0]) == expected
    assert getattr(rec, FIELDS[1]) is None
    assert rec.fcf_ttm == 80


@pytest.mark.parametrize("kind", ["140", "150"])
def test_quarter_scope_not_expanded(kind):
    rec = parse(
        [(LEASE, "CurrentYTDDuration", "JPY", "-12")], kind=kind, context="CurrentYTDDuration"
    )
    assert getattr(rec, FIELDS[0]) is None


def test_new_details_never_establish_or_switch_the_existing_basis():
    rec = parse([(LEASE, "InterimDuration", "JPY", "-12")], core=False)
    assert rec.consolidation_basis is None
    assert getattr(rec, FIELDS[0]) is None
    individual = "InterimDuration_NonConsolidatedMember"
    rec = parse(
        [
            (LEASE, "InterimDuration", "JPY", "-999"),
            (LEASE, individual, "JPY", "-12"),
            (NCI, individual, "JPY", "-3"),
        ],
        context=individual,
        consolidated="false",
    )
    assert rec.consolidation_basis == "non_consolidated"
    assert getattr(rec, FIELDS[0]) == -12
    assert getattr(rec, FIELDS[1]) is None
    assert rec.fcf_ttm == 80


@pytest.mark.parametrize("standard", ["IFRS", "US GAAP"])
def test_standard_selection_does_not_borrow_jp_cash_details(standard):
    rec = parse([(LEASE, "InterimDuration", "JPY", "-12")], standard=standard)
    assert getattr(rec, FIELDS[0]) is None


def test_details_survive_owner_storage_and_decode(tmp_path):
    rec = parse([(LEASE, "InterimDuration", "JPY", "-12"), (NCI, "InterimDuration", "JPY", "-3")])
    p = tmp_path / "market.sqlite"
    day = date(2026, 11, 16)
    store_edinet_metrics(
        p,
        day,
        [
            _metric_record_payload(
                rec, extractor_revision="synthetic", source_document_revision="source"
            )
        ],
    )
    saved = read_edinet_metrics(p, day)["0000"]
    assert tuple(getattr(saved, k) for k in FIELDS) == (-12, -3)
    assert saved.fcf_ttm == 80
