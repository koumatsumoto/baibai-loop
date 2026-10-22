"""Fiscal-year metadata must not turn an interim revision into newer annual facts."""

from datetime import date

import pytest

from baibai_engine.market.edinet_metrics.planning import make_metric_extraction_plan
from baibai_engine.market.edinet_metrics.revision import EDINET_METRIC_REVISION
from baibai_engine.market.edinet_selection import select_document_candidates


def _document(doc_id: str, kind: str, submitted: str, year: int = 2025) -> dict[str, str]:
    return {
        "docID": doc_id,
        "secCode": "00000",
        "docTypeCode": kind,
        "csvFlag": "1",
        "xbrlFlag": "1",
        "legalStatus": "1",
        "disclosureStatus": "0",
        "withdrawalStatus": "0",
        "periodStart": f"{year}-01-01",
        "periodEnd": f"{year}-12-31",
        "submitDateTime": submitted,
    }


@pytest.mark.parametrize("annual_kind", ["120", "130"])
def test_annual_facts_survive_later_interim_filing_with_same_fiscal_metadata(
    annual_kind: str,
) -> None:
    annual = _document("S100ANNUAL", annual_kind, "2026-03-25 10:00")
    interim = _document("S100INTERIM", "160", "2025-08-07 10:00")
    later = _document("S100CORR", "170", "2026-04-01 10:00")
    later["parentDocID"] = "S100INTERIM"
    documents = [interim, annual, later]
    if annual_kind == "130":
        annual["parentDocID"] = "S100ORIGINAL"
        documents.append(_document("S100ORIGINAL", "120", "2026-03-24 10:00"))
    selected = select_document_candidates(documents).candidates["0000"]

    assert selected.doc_id == "S100ANNUAL"
    assert selected.doc_type_code == annual_kind
    assert selected.period_end == date(2025, 12, 31)


def test_annual_precedence_preserves_newer_period_and_within_family_corrections() -> None:
    annual = _document("S100ANNUAL", "120", "2026-03-25 10:00")
    annual_correction = _document("S100ANNCORR", "130", "2026-04-01 10:00")
    annual_correction["parentDocID"] = "S100ANNUAL"
    documents = [annual, annual_correction]
    assert select_document_candidates(documents).candidates["0000"].doc_id == "S100ANNCORR"

    interim = _document("S100INTERIM", "160", "2026-08-07 10:00", year=2026)
    interim_correction = _document("S100INTCORR", "170", "2026-09-01 10:00", year=2026)
    interim_correction["parentDocID"] = "S100INTERIM"
    documents.extend([interim, interim_correction])
    assert select_document_candidates(documents).candidates["0000"].doc_id == "S100INTCORR"


@pytest.mark.parametrize(
    "missing_fields", [("periodStart",), ("periodEnd",), ("periodStart", "periodEnd")]
)
def test_unknown_metadata_does_not_add_annual_precedence(missing_fields) -> None:
    annual = _document("S100ANNUAL", "120", "2026-03-25 10:00")
    interim = _document("S100INTERIM", "160", "2026-04-01 10:00")
    for document in (annual, interim):
        for field in missing_fields:
            document.pop(field)
    assert select_document_candidates([annual, interim]).candidates["0000"].doc_id == "S100INTERIM"


@pytest.mark.parametrize("field", ["periodStart", "periodEnd"])
@pytest.mark.parametrize("missing_from", ["annual", "interim"])
def test_asymmetric_metadata_keeps_existing_known_period_order(field, missing_from) -> None:
    annual = _document("S100ANNUAL", "120", "2026-03-25 10:00")
    interim = _document("S100INTERIM", "160", "2026-04-01 10:00")
    documents = {"annual": annual, "interim": interim}
    documents[missing_from].pop(field)
    expected = "S100INTERIM" if missing_from == "annual" else "S100ANNUAL"
    assert (
        select_document_candidates(list(documents.values())).candidates["0000"].doc_id == expected
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("csvFlag", "0"),
        ("xbrlFlag", "0"),
        ("legalStatus", "0"),
        ("withdrawalStatus", "2"),
        ("disclosureStatus", "2"),
    ],
)
def test_unusable_annual_cannot_enter_candidate_ranking(field: str, value: str) -> None:
    annual = _document("S100ANNUAL", "120", "2026-03-25 10:00")
    annual[field] = value
    assert select_document_candidates([annual]).candidates == {}


def test_orphan_quarantine_still_excludes_the_ranked_annual_from_extraction() -> None:
    annual = _document("S100ANNUAL", "120", "2026-03-25 10:00")
    interim = _document("S100INTERIM", "160", "2026-04-01 10:00")
    orphan = _document("S100MISSING", "120", "2026-04-02 10:00")
    orphan.update(
        doc_date="2026-04-02",
        docInfoEditStatus="1",
        opeDateTime="2026-04-02 12:00",
    )
    selection = select_document_candidates([annual, interim, orphan])
    assert selection.candidates["0000"].doc_id == "S100ANNUAL"
    assert tuple(event.doc_id for event in selection.quarantined_events) == ("S100MISSING",)
    plan = make_metric_extraction_plan(
        selection=selection, baseline=None, extractor_revision=EDINET_METRIC_REVISION
    )
    assert "0000" in plan.events_by_ticker
    assert plan.archive_candidates == ()
