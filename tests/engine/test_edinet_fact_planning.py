"""Classify each selected Research filing without acquiring source archives."""

import json
from datetime import date

from baibai_engine.machine_analysis.observations.financials.edinet_facts.extract import EXTRACTOR_REVISION
from baibai_engine.machine_analysis.observations.financials.edinet_facts.models import filing_identity
from baibai_engine.machine_analysis.observations.financials.edinet_facts.planning import plan_fact_item


def _document(**changes):
    return {
        "docID": "S100TEST",
        "secCode": "18730",
        "submitDateTime": "2026-06-24 14:00",
        "legalStatus": "1",
        "disclosureStatus": "0",
        "withdrawalStatus": "0",
        "docInfoEditStatus": "0",
        "xbrlFlag": "1",
        **changes,
    }


def test_plan_fact_item_extract_future_and_unknown_submission():
    doc = _document()
    item = plan_fact_item(doc, statuses={}, asof=date(2026, 6, 24))
    assert item.action == "extract"
    assert (item.doc_id, item.submitted) == ("S100TEST", "2026-06-24 14:00")
    assert item.identity == filing_identity("1873", "S100TEST", item.submitted)
    assert item.reason is None

    assert plan_fact_item(doc, statuses={}, asof=date(2026, 6, 23)).action == "future"
    unknown = plan_fact_item(_document(submitDateTime=""), statuses={}, asof=date(2026, 6, 24))
    assert unknown.action == "unknown_submission"
    assert unknown.identity is None


def test_plan_fact_item_unavailable_uses_existing_failure_reasons():
    missing_period = plan_fact_item(
        _document(_research_period_known=False), statuses={}, asof=date(2026, 6, 24)
    )
    assert missing_period.action == "unavailable"
    assert missing_period.reason == "annual_period_unknown"

    missing_xbrl = plan_fact_item(_document(xbrlFlag="0"), statuses={}, asof=date(2026, 6, 24))
    assert missing_xbrl.action == "unavailable"
    assert missing_xbrl.reason == "document_validity_or_xbrl_unknown"


def test_plan_fact_item_reuses_only_matching_success_revision_and_identity():
    doc = _document()
    identity = filing_identity("1873", "S100TEST", doc["submitDateTime"])
    cached = json.dumps({"revision": EXTRACTOR_REVISION, "identity": identity.model_dump()})
    statuses = {"S100TEST": ("ok", cached)}
    assert plan_fact_item(doc, statuses=statuses, asof=date(2026, 6, 24)).action == "reused"
    assert (
        plan_fact_item(
            doc, statuses={"S100TEST": ("failed", cached)}, asof=date(2026, 6, 24)
        ).action
        == "extract"
    )
    changed_revision = json.dumps({"revision": "older", "identity": identity.model_dump()})
    assert (
        plan_fact_item(
            doc, statuses={"S100TEST": ("ok", changed_revision)}, asof=date(2026, 6, 24)
        ).action
        == "extract"
    )
    changed_identity = _document(secCode="99990")
    assert (
        plan_fact_item(changed_identity, statuses=statuses, asof=date(2026, 6, 24)).action
        == "extract"
    )
