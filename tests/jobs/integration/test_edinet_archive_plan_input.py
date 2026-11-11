"""Fixed native planning retains the existing document-row/key-presence interpretation."""

import sqlite3
from contextlib import closing
from datetime import timedelta

import pytest
from tests.jobs.acquisition.test_edinet_archive_native_acquisition import (
    AT,
    DAY,
    filing,
    save_lists,
    setup,
)

from baibai_engine.data_collection.providers.edinet.archive_input import read_archive_list_input
from baibai_engine.data_collection.providers.edinet.archive_plan import select_holder_archives
from baibai_engine.data_collection.providers.edinet.document_events import (
    canonicalize_document_events,
)
from baibai_engine.data_collection.providers.edinet.list_native import NativeListReceipt
from baibai_engine.data_collection.providers.edinet.models import (
    EDINETProviderError,
    EdinetResponse,
)
from baibai_engine.machine_analysis.observations.disclosures.edinet_document_store import (
    read_observed_documents,
    read_stored_documents,
)
from baibai_jobs.integration.edinet_list_native import materialize_lists


@pytest.mark.parametrize("latest", ["valid", "malformed", "ambiguous"])
def test_source_plan_matches_sql_projection_inputs_and_holder_scope(tmp_path, latest):
    documents = [
        filing("S100HOLDER", docTypeCode="350", secCode=None, docDescription="holder"),
        filing("S100ANNUAL", docDescription="original", issuerEdinetCode="E00001"),
    ]
    _, _, owner, _ = setup(tmp_path, documents)
    # Expired origins carry fields from retained native receipts, not former L2.
    save_lists(
        owner,
        [
            filing("S100HOLDER", docTypeCode=None, secCode=None, legalStatus="0"),
            filing("S100ANNUAL", secCode=None, docTypeCode=None, legalStatus="0"),
        ],
        receipt=2,
    )
    edits = [
        {
            "seqNumber": 5,
            "docID": "S100ANNUAL",
            "docInfoEditStatus": "1",
            "docDescription": None,
            "opeDateTime": "2026-11-06 10:00",
        },
        {
            "seqNumber": 3,
            "docID": "S100HOLDER",
            "docInfoEditStatus": "1",
            "opeDateTime": "2026-11-06 09:00",
        },
    ]
    save_lists(owner, edits, day=DAY + timedelta(days=1), receipt=3)
    if latest != "valid":
        receipts = [
            NativeListReceipt.from_response(
                DAY, EdinetResponse(AT + timedelta(seconds=4), b"{"), "0" * 31 + "4"
            )
        ]
        if latest == "ambiguous":
            receipts.append(
                NativeListReceipt.from_response(
                    DAY, EdinetResponse(AT + timedelta(seconds=4), b"[]"), "0" * 31 + "5"
                )
            )
        owner.publish(receipts)
    fixed = owner.freeze()
    before = fixed.value()
    inputs = read_archive_list_input(fixed)
    database = tmp_path / "comparison.sqlite"
    result = materialize_lists(fixed, database)
    through = DAY + timedelta(days=1)
    with closing(sqlite3.connect(database)) as conn:
        projected = read_stored_documents(conn, through=through)
        holders = read_observed_documents(conn, through=through, holder_only=True)
    assert inputs.stored_documents(through=through) == projected
    assert [day.isoformat() for day in inputs.failed_days] == result["failed_days"]
    folded, _ = canonicalize_document_events(projected, origin_start=None)
    assert next(doc for doc in folded if doc["docID"] == "S100ANNUAL")["docDescription"] is None
    assert next(doc for doc in folded if doc["docID"] == "S100HOLDER")["docDescription"] == "holder"
    assert select_holder_archives(inputs.observed_documents(through=through), retained=set()) == (
        select_holder_archives(holders, retained=set())
    )
    assert owner.freeze().value() == before


def test_unavailable_day_remains_failed_for_annual_planning_but_retains_other_evidence(tmp_path):
    _, _, owner, _ = setup(tmp_path, [filing("S100KNOWN")])
    bad_day = DAY + timedelta(days=1)
    fixed = owner.publish(
        [
            NativeListReceipt.from_response(
                bad_day, EdinetResponse(AT + timedelta(seconds=2), b"{"), "0" * 31 + "2"
            )
        ]
    )
    inputs = read_archive_list_input(fixed)
    assert [doc["docID"] for doc in inputs.observed_documents(through=bad_day)] == ["S100KNOWN"]
    with pytest.raises(EDINETProviderError, match="coverage failed"):
        inputs.stored_documents(through=bad_day)
    assert inputs.available_days == frozenset({DAY})
    assert inputs.failed_days == (bad_day,)


def test_document_identity_checks_follow_date_and_holder_scope(tmp_path):
    _, _, owner, _ = setup(
        tmp_path,
        [
            filing("S100HOLDER", docTypeCode="350", secCode=None),
            filing(" S100OTHER ", docTypeCode="160", seqNumber=2),
        ],
    )
    future_day = DAY + timedelta(days=1)
    fixed = save_lists(owner, [filing(" S100FUTURE ")], day=future_day, receipt=2)
    inputs = read_archive_list_input(fixed)
    database = tmp_path / "comparison.sqlite"
    materialize_lists(fixed, database)
    with closing(sqlite3.connect(database)) as conn:
        holders = read_observed_documents(conn, through=DAY, holder_only=True)
        assert inputs.observed_documents(through=DAY, holder_only=True) == holders
        assert [doc["docID"] for doc in holders] == ["S100HOLDER"]
        for read in (
            lambda: read_observed_documents(conn, through=DAY),
            lambda: inputs.observed_documents(through=DAY),
        ):
            with pytest.raises(EDINETProviderError, match="no matching native row"):
                read()
    # A future mismatching row does not participate in the current-day read.
    _, _, future_owner, _ = setup(tmp_path / "future", [filing("S100CURRENT")])
    fixed = save_lists(future_owner, [filing(" S100FUTURE ")], day=future_day, receipt=2)
    inputs = read_archive_list_input(fixed)
    assert [doc["docID"] for doc in inputs.observed_documents(through=DAY)] == ["S100CURRENT"]
    with pytest.raises(EDINETProviderError, match="no matching native row"):
        inputs.observed_documents(through=future_day)
