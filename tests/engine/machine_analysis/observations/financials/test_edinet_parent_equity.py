"""Retain the reported IFRS parent claim without changing v17 metrics or PBR."""

from __future__ import annotations

import zipfile
from contextlib import closing
from dataclasses import asdict
from datetime import date
from hashlib import sha256
from io import BytesIO
from pathlib import Path

import pytest

from baibai_engine.machine_analysis.models.company_financials.metrics.financial_snapshot import (
    build_financial_snapshot,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_financials.reported_flows import _reported_flows
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.csv import (
    parse_csv_zip_metric_record,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.revision import (
    EDINET_METRIC_REVISION,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.service import (
    _metric_record_payload,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.store import (
    read_edinet_metric_baseline,
    read_edinet_metrics,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.write import (
    store_edinet_metrics,
)
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.integration.edinet_metric_acceptance import metric_result_sha256

FIXTURES = Path(__file__).parents[4] / "fixtures" / "edinet"
ELEMENT = "jpigp_cor:EquityIFRS"
MEMBER = "EquityAttributableToOwnersOfParentIFRSMember"
CONTEXT = f"InterimInstant_{MEMBER}"
SEP = "\t"
STANDARD = "jpdei_cor:AccountingStandardsDEI"
CONS = "jpdei_cor:WhetherConsolidatedFinancialStatementsArePreparedDEI"
HEADER = "要素ID\tコンテキストID\t期間・時点\tユニットID\t値\n"
AS_OF = date(2026, 11, 18)
NEXT = date(2026, 11, 19)


def _zipped(text: str, *other: str) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        zipped.writestr(zipfile.ZipInfo("XBRL_TO_CSV/facts.csv"), text.encode("utf-16"))
        for index, extra in enumerate(other, start=1):
            zipped.writestr(
                zipfile.ZipInfo(f"XBRL_TO_CSV/other{index}.csv"), extra.encode("utf-16")
            )
    return buffer.getvalue()


def _parse(text: str, doc_type: str = "160", *, doc_id: str = "S100TEST", ticker="1605", **meta):
    return parse_csv_zip_metric_record(
        ticker=ticker, doc_id=doc_id, doc_type_code=doc_type, content=_zipped(text), **meta
    )


def _csv(*rows: tuple[str, str, str, str]) -> str:
    return HEADER + "\n".join(
        SEP.join((element, context, "期間" if "Duration" in context else "時点", unit, value))
        for element, context, unit, value in rows
    )


def _interim(*facts: tuple[str, str, str, str], standard="IFRS", consolidated="true"):
    rows = [
        (STANDARD, "FilingDateInstant", "－", standard),
        (CONS, "FilingDateInstant", "－", consolidated),
        (ELEMENT, "InterimInstant", "JPY", "10000"),
        *facts,
    ]
    return _parse(_csv(*rows))


@pytest.mark.parametrize(
    ("doc_id", "ticker", "doc_type", "instant", "owner", "net_assets"),
    [
        ("S100XU9L", "1605", "120", "CurrentYearInstant", 4_747_158_000_000, 5_022_903_000_000),
        ("S100YV6Y", "1605", "160", "InterimInstant", 5_102_570_000_000, 5_350_952_000_000),
        ("S100YTCC", "2914", "160", "InterimInstant", 4_393_958_000_000, 4_423_050_000_000),
    ],
)
def test_current_exact_ifrs_context_in_retained_fixture(
    doc_id, ticker, doc_type, instant, owner, net_assets
):
    raw = (FIXTURES / f"{doc_id}.tsv").read_text()
    record = _parse(raw, doc_type, doc_id=doc_id, ticker=ticker)
    fact = record.parent_attributable_equity
    assert fact is not None
    assert asdict(fact) == {
        "amount_yen": owner,
        "source_context": f"{instant}_{MEMBER}",
        "source_element": ELEMENT,
        "source_csv_member": "XBRL_TO_CSV/facts.csv",
        "archive_sha256": sha256(_zipped(raw)).hexdigest(),
        "observation_version": 1,
    }
    assert record.equity == net_assets
    assert record.consolidation_basis == "consolidated"


@pytest.mark.parametrize(
    "facts",
    [
        ((ELEMENT, f"Prior1{CONTEXT}", "JPY", "9"),),
        ((ELEMENT, f"InterimDuration_{MEMBER}", "JPY", "9"),),
        ((ELEMENT, "InterimInstant_NonControllingInterestsIFRSMember", "JPY", "9"),),
        ((ELEMENT, f"{CONTEXT}_NonConsolidatedMember", "JPY", "9"),),
        ((ELEMENT, f"{CONTEXT}_IssuerSegmentMember", "JPY", "9"),),
        (("issuer_cor:EquityIFRS", CONTEXT, "JPY", "9"),),
        ((ELEMENT, CONTEXT, "USD", "9"),),
        ((ELEMENT, CONTEXT, "JPY", ""),),
        ((ELEMENT, CONTEXT, "JPY", "1.5"),),
        ((ELEMENT, CONTEXT, "JPY", str(2**63)),),
        ((ELEMENT, CONTEXT, "JPY", "9"), (ELEMENT, CONTEXT, "JPY", "10")),
        ((ELEMENT, CONTEXT, "JPY", "9"), (ELEMENT, CONTEXT, "USD", "9")),
    ],
)
def test_wrong_or_ambiguous_fact_never_falls_back_to_group_equity(facts):
    record = _interim(*facts)
    assert record.equity == 10_000
    assert record.parent_attributable_equity is None


def test_explicit_ifrs_and_consolidation_are_required():
    parent = (ELEMENT, CONTEXT, "JPY", "9")
    assert _interim(parent, standard="Japan GAAP").parent_attributable_equity is None
    assert _interim(parent, consolidated="false").parent_attributable_equity is None
    assert (
        _interim(parent, (CONS, "FilingDateInstant", "－", "false")).parent_attributable_equity
        is None
    )
    text = _csv((ELEMENT, "InterimInstant", "JPY", "10000"), parent)
    assert _parse(text).parent_attributable_equity is None


def test_missing_period_column_leaves_only_parent_equity_unobserved():
    text = _csv(
        (STANDARD, "FilingDateInstant", "－", "IFRS"),
        (CONS, "FilingDateInstant", "－", "true"),
        (ELEMENT, "InterimInstant", "JPY", "10000"),
        (ELEMENT, CONTEXT, "JPY", "9800"),
    )
    complete = _parse(text)
    without_period = _parse(text.replace("\t期間・時点", "").replace("\t時点", ""))
    assert complete.parent_attributable_equity is not None
    assert complete.parent_attributable_equity.amount_yen == 9800
    assert without_period.parent_attributable_equity is None
    assert without_period.equity == complete.equity == 10_000
    assert metric_result_sha256(without_period) == metric_result_sha256(complete)


@pytest.mark.parametrize("competing", [False, True])
def test_parent_fact_does_not_cross_zip_csv_members(competing):
    current_group = (
        (STANDARD, "FilingDateInstant", "－", "IFRS"),
        (CONS, "FilingDateInstant", "－", "true"),
        (ELEMENT, "InterimInstant", "JPY", "10000"),
    )
    own = (ELEMENT, CONTEXT, "JPY", "9800")
    # The first test tries to borrow the consolidated fact and DEIs from CSV A.
    # The second makes both members look valid; even equal facts cannot prove one source.
    source = _csv(*current_group, own) if competing else _csv(*current_group)
    other = _csv(*current_group, own) if competing else _csv(own)
    record = parse_csv_zip_metric_record(
        ticker="1605",
        doc_id="S100TEST",
        doc_type_code="160",
        content=_zipped(source, other),
    )
    assert record.equity == 10_000
    assert record.parent_attributable_equity is None

    # An unrelated CSV is not an obstacle when exactly one complete member establishes the fact.
    complete = parse_csv_zip_metric_record(
        ticker="1605",
        doc_id="S100TEST",
        doc_type_code="160",
        content=_zipped(_csv((ELEMENT, f"Prior1{CONTEXT}", "JPY", "4")), _csv(*current_group, own)),
    ).parent_attributable_equity
    assert complete is not None
    assert complete.amount_yen == 9800
    assert complete.source_csv_member == "XBRL_TO_CSV/other1.csv"


@pytest.mark.parametrize(
    ("first_total", "second_context", "second_total", "certified"),
    [
        ("10000", "InterimInstant", "23000", False),
        (str(2**53), "InterimInstant", str(2**53 + 1), False),
        ("10000", "InterimInstant", "10000", True),
        ("10000", "Prior1InterimInstant", "23000", True),
    ],
)
def test_exact_current_group_equity_must_agree_across_native_members(
    first_total, second_context, second_total, certified
):
    primary = _csv(
        (STANDARD, "FilingDateInstant", "－", "IFRS"),
        (CONS, "FilingDateInstant", "－", "true"),
        (ELEMENT, "InterimInstant", "JPY", first_total),
        (ELEMENT, CONTEXT, "JPY", "9800"),
    )
    secondary = _csv((ELEMENT, second_context, "JPY", second_total))
    record = parse_csv_zip_metric_record(
        ticker="1605", doc_id="S100TEST", doc_type_code="160", content=_zipped(primary, secondary)
    )
    fact = record.parent_attributable_equity
    assert (fact.amount_yen if fact else None) == (9800 if certified else None)


@pytest.mark.parametrize(
    ("raw", "expected"), [("-", 0), ("-510", -510), (str(2**53 + 1), 2**53 + 1)]
)
def test_exact_integer_survives_own_storage_and_older_day_stays_unknown(tmp_path, raw, expected):
    record = _interim((ELEMENT, CONTEXT, "JPY", raw), (ELEMENT, CONTEXT, "JPY", raw))
    fact = record.parent_attributable_equity
    assert fact is not None
    assert fact.amount_yen == expected
    payload = _metric_record_payload(
        record, extractor_revision=EDINET_METRIC_REVISION, source_document_revision="f" * 64
    )
    previous = {k: v for k, v in payload.items() if k != "parent_attributable_equity_json"}
    db = tmp_path / "market.sqlite"
    assert store_edinet_metrics(db, AS_OF, [previous]) == 1
    assert store_edinet_metrics(db, NEXT, [payload]) == 1

    assert read_edinet_metrics(db, AS_OF)["1605"].parent_attributable_equity is None
    saved = read_edinet_metrics(db, NEXT)["1605"]
    assert saved.parent_attributable_equity == fact
    baseline = read_edinet_metric_baseline(db, NEXT)
    assert baseline.rows["1605"].extractor_revision == EDINET_METRIC_REVISION
    assert baseline.rows["1605"].source_document_revision == "f" * 64

    invalid = {**payload, "consolidation_basis": "non_consolidated"}
    with pytest.raises(ValueError, match="invalid EDINET parent-attributable equity"):
        store_edinet_metrics(db, NEXT, [invalid])
    assert read_edinet_metrics(db, NEXT)["1605"] == saved
    invalid = {**payload, "source_document_revision": None}
    with pytest.raises(ValueError, match="invalid EDINET parent-attributable equity"):
        store_edinet_metrics(db, NEXT, [invalid])

    # A malformed ancillary column is unavailable independently of legacy metrics.
    with closing(open_connection(db)) as connection, connection:
        connection.execute(
            "UPDATE edinet_metrics SET parent_attributable_equity_json='{}' WHERE asof_date=?",
            (NEXT.isoformat(),),
        )
    read_without_fact = read_edinet_metrics(db, NEXT)["1605"]
    assert read_without_fact.parent_attributable_equity is None
    assert read_without_fact.equity == saved.equity == 10_000
    assert metric_result_sha256(read_without_fact) == metric_result_sha256(saved)


def test_manual_rows_without_native_revision_cannot_certify_parent_observation(tmp_path):
    record = _interim((ELEMENT, CONTEXT, "JPY", "9800"))
    payload = _metric_record_payload(
        record, extractor_revision=EDINET_METRIC_REVISION, source_document_revision=None
    )
    db = tmp_path / "market.sqlite"
    assert (
        store_edinet_metrics(db, AS_OF, [{**payload, "parent_attributable_equity_json": None}]) == 1
    )
    with closing(open_connection(db)) as conn, conn:
        conn.execute(
            "UPDATE edinet_metrics SET parent_attributable_equity_json=? WHERE asof_date=?",
            (payload["parent_attributable_equity_json"], AS_OF.isoformat()),
        )
    baseline = read_edinet_metric_baseline(db, AS_OF)
    assert baseline.rows["1605"].record.equity == 10_000
    assert baseline.rows["1605"].record.parent_attributable_equity is None
    assert read_edinet_metrics(db, AS_OF)["1605"].parent_attributable_equity is None


def test_source_observation_cannot_change_cf_debt_or_financial_book_selection():
    rows = (
        (STANDARD, "FilingDateInstant", "－", "IFRS"),
        (CONS, "FilingDateInstant", "－", "true"),
        (ELEMENT, "InterimInstant", "JPY", "5650"),
        ("jpigp_cor:AssetsIFRS", "InterimInstant", "JPY", "10000"),
        ("jpigp_cor:CashAndCashEquivalentsIFRS", "InterimInstant", "JPY", "1000"),
        ("jpigp_cor:BondsAndBorrowingsCLIFRS", "InterimInstant", "JPY", "20"),
        ("jpigp_cor:BondsAndBorrowingsNCLIFRS", "InterimInstant", "JPY", "30"),
        ("jpigp_cor:LeaseLiabilitiesCLIFRS", "InterimInstant", "JPY", "40"),
        ("jpigp_cor:LeaseLiabilitiesNCLIFRS", "InterimInstant", "JPY", "50"),
        (
            "jpigp_cor:NetCashProvidedByUsedInOperatingActivitiesIFRS",
            "InterimDuration",
            "JPY",
            "500",
        ),
        (
            "jpigp_cor:PurchaseOfPropertyPlantAndEquipmentAndIntangibleAssetsInvCFIFRS",
            "InterimDuration",
            "JPY",
            "-100",
        ),
    )
    old = _parse(_csv(*rows))
    first, second = [
        _parse(_csv(*rows, (ELEMENT, CONTEXT, "JPY", raw))) for raw in ("5401", "5399")
    ]
    for parsed in (first, second):
        assert parsed.parent_attributable_equity is not None
        assert metric_result_sha256(parsed) == metric_result_sha256(old)
        assert _reported_flows(parsed) == _reported_flows(old)
        assert (parsed.ocf_ttm, parsed.debt, parsed.net_cash, parsed.capex_ttm) == (
            500,
            140,
            860,
            100,
        )
    assert first.parent_attributable_equity != second.parent_attributable_equity
    summary = JQuantsFinancialSummary(
        ticker="1605",
        disclosed_at=date(2026, 11, 11),
        disclosure_number="q2",
        document_type="2QFinancialStatements_Consolidated_IFRS",
        period_start=date(2026, 4, 1),
        period_end=date(2026, 9, 30),
        fiscal_year_end=date(2027, 3, 31),
        fiscal_period="2Q",
        shares_outstanding=105,
        treasury_shares=5,
        total_assets=10_000,
        equity_to_asset_ratio=0.54,
        bps=54,
        equity=5650,
    )
    before, after = (
        build_financial_snapshot(
            50, (summary,), record, DEFAULT_TTM_RULES, adjustment_events=(), asof_date=AS_OF
        )
        for record in (old, first)
    )
    assert before == after
    assert before.pbr == pytest.approx(5000 / 5400)
    assert before.book_equity["method"] == "assets_times_equity_ratio"
