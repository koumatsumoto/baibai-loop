"""資本注記の表示値と価格分母を分け、文脈日だけで株数を組み合わせない。"""

from dataclasses import asdict
from datetime import date

import pytest
from tests.helpers.edinet_capital_notes import BLOCK, block, context, source_zip, table

from baibai_engine.machine_analysis.observations.financials.edinet_issuer_capital import (
    extract_issuer_capital_notes,
)


def extract(**kwargs):
    return extract_issuer_capital_notes(
        source_zip(**kwargs),
        edinet_code="E12345",
        period_start=date(2025, 4, 1),
        period_end=date(2026, 3, 31),
    )


def test_same_table_explicit_end_counts_and_source_coordinates():
    result = extract()
    assert result.reasons == ()
    (note,) = result.observations
    assert (note.issued_display_count, note.treasury_display_count) == (100000, 500)
    assert note.display_unit == "株"
    assert note.reporting_period_end == date(2026, 3, 31)
    assert note.reporting_period_start == date(2025, 4, 1)
    assert note.consolidation_basis == "consolidated"
    assert note.source_context == "CurrentYearDuration"
    assert note.issued_source_locator.endswith("/table[0]/row[2]/cell[4]")
    assert note.treasury_source_locator.endswith("/table[0]/row[5]/cell[4]")
    assert note.basis == "reported_note_display_not_reconciled_to_price_denominator"
    assert not {"shares_outstanding", "market_cap", "rank"} & asdict(note).keys()


@pytest.mark.parametrize("unit", ["株", "千株"])
def test_explicit_zero_and_thousands_keep_display_precision(unit):
    (note,) = extract(blocks=block(table(unit=unit, issued="8,888", treasury="0"))).observations
    assert note.issued_display_count == 8888
    assert note.treasury_display_count == 0
    assert note.display_unit == unit


@pytest.mark.parametrize(
    "value", ["", "-", "－", "―", "該当事項はありません", "0.0", "-1", "1e3", "1,00"]
)
def test_missing_or_unsupported_count_is_not_zero(value):
    result = extract(blocks=block(table(treasury=value)))
    assert not result.observations
    assert result.reasons == ("capital_note_count_unavailable",)


def test_trust_detail_is_not_added_to_reported_treasury():
    html = table(treasury="500") + "<p>自己株式500株には信託口所有の300株を含む。</p>"
    (note,) = extract(blocks=block(html)).observations
    assert note.treasury_display_count == 500
    assert note.basis.endswith("not_reconciled_to_price_denominator")


def test_display_observation_does_not_certify_valid_price_denominator():
    (note,) = extract(
        blocks=block(table(issued="8,888", treasury="8,888", unit="千株"))
    ).observations
    assert note.issued_display_count == note.treasury_display_count == 8888


def test_explicit_period_heading_must_agree_with_the_duration():
    good = "<p>当連結会計年度（自2025年4月1日 至2026年3月31日）</p>"
    assert extract(blocks=block(good + table())).observations
    wrong = "<p>2024年4月1日から2025年3月31日</p>"
    result = extract(blocks=block(wrong + table()))
    assert not result.observations
    assert result.reasons == ("capital_note_period_label_unsupported",)


@pytest.mark.parametrize(
    ("label", "accepted"),
    [
        ("対象期間：2024年4月1日から2025年3月31日", False),
        ("対象期間：2025年4月1日から2026年3月31日", True),
    ],
)
def test_table_caption_is_part_of_the_reporting_period_evidence(label, accepted):
    html = table().replace("<table>", f"<table><caption>{label}</caption>")
    result = extract(blocks=block(html))
    assert bool(result.observations) is accepted
    if not accepted:
        assert result.reasons == ("capital_note_period_label_unsupported",)


def test_non_consolidated_scope_is_separate():
    dimensions = (
        '<x:scenario><d:explicitMember dimension="p:ConsolidatedOrNonConsolidatedAxis">'
        "p:NonConsolidatedMember</d:explicitMember></x:scenario>"
    )
    contexts = context() + context(key="Separate", dimensions=dimensions)
    blocks = block() + block(table(label="当事業年度", treasury="300"), key="Separate")
    result = extract(contexts=contexts, blocks=blocks)
    assert {
        (note.consolidation_basis, note.treasury_display_count) for note in result.observations
    } == {
        ("consolidated", 500),
        ("non_consolidated", 300),
    }


@pytest.mark.parametrize(
    "change",
    [
        {"start": None, "end": "2026-06-12"},
        {"start": "2024-04-01", "end": "2025-03-31"},
        {"start": "2025-10-01"},
        {"issuer": "E99999-000"},
        {
            "dimensions": '<x:scenario><d:explicitMember dimension="p:OtherAxis">p:NonConsolidatedMember</d:explicitMember></x:scenario>'
        },
    ],
)
def test_different_period_issuer_or_dimension_cannot_supply_the_pair(change):
    assert not extract(contexts=context(**change)).observations


def test_filing_context_numeric_tags_and_voting_own_name_are_not_the_note():
    other = (
        "<c:NumberOfIssuedSharesAsOfFiscalYearEndIssuedSharesTotalNumberOfSharesEtc "
        'contextRef="FilingDateInstant">100000</c:NumberOfIssuedSharesAsOfFiscalYearEndIssuedSharesTotalNumberOfSharesEtc>'
        "<c:NumberOfIssuedSharesAsOfFilingDateIssuedSharesTotalNumberOfSharesEtc "
        'contextRef="FilingDateInstant">120000</c:NumberOfIssuedSharesAsOfFilingDateIssuedSharesTotalNumberOfSharesEtc>'
        '<c:NumberOfSharesHeldInOwnNameTreasurySharesEtc contextRef="FilingDateInstant">0</c:NumberOfSharesHeldInOwnNameTreasurySharesEtc>'
    )
    result = extract(
        contexts=context(key="FilingDateInstant", start=None, end="2026-06-12"),
        blocks="",
        other=other,
    )
    assert result.observations == ()
    assert result.reasons == ("capital_note_not_observed",)


@pytest.mark.parametrize(
    "html",
    [
        table() + table(treasury="0"),
        "<p>前連結会計年度</p>" + table(),
        table().replace("<td>", '<td colspan="2">', 1),
        table().replace("当連結会計年度末", "前連結会計年度末"),
        table(unit="円"),
        table().replace("<td>合計</td>", "<td>議決権区分</td>", 1),
        table()
        .replace("<table><tbody>", "<table><tbody><table><tbody>")
        .replace("</tbody></table>", "</tbody></table></tbody></table>"),
    ],
)
def test_ambiguous_or_unsupported_layout_is_not_reinterpreted(html):
    assert not extract(blocks=block(html)).observations


@pytest.mark.parametrize("second", [block(), block(table(treasury="－"))])
def test_duplicate_current_blocks_do_not_select_a_convenient_pair(second):
    result = extract(blocks=block() + second)
    assert not result.observations
    assert "capital_note_multiple_blocks" in result.reasons


def test_custom_namespace_and_nil_do_not_masquerade_as_supported_facts():
    assert not extract(namespace="urn:issuer:custom").observations
    assert extract(blocks=block(attributes='xsi:nil="true"')).reasons == ("capital_note_nil",)
    result = extract(blocks=block().replace(BLOCK, "TreasurySharesEtcTextBlock"))
    assert not result.observations
