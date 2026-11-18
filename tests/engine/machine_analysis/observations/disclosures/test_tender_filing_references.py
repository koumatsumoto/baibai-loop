from datetime import UTC, date, datetime

import pytest

from baibai_engine.machine_analysis.observations.disclosures.tender_filing_references import (
    resolve_tender_filing_references,
)

DAY = "2026-06-01"
AT = datetime(2026, 6, 1, 3, tzinfo=UTC)  # noon JST
TARGET, OFFEROR = "E91001", "E91002"
TICKER, OFFEROR_TICKER = "991A", "992A"


def doc(doc_id, kind, *, parent=None, filer=OFFEROR, subject=TARGET, clock="10:00", **fields):
    return {
        "docID": doc_id,
        "docTypeCode": kind,
        "doc_date": DAY,
        "seqNumber": 1,
        "submitDateTime": f"{DAY} {clock}",
        "edinetCode": filer,
        "subjectEdinetCode": subject,
        "parentDocID": parent,
        "legalStatus": "1",
        "disclosureStatus": "0",
        "withdrawalStatus": "0",
        "docInfoEditStatus": "0",
        **fields,
    }


def anchor():
    return doc("SANCHOR", "120", filer=TARGET, subject=None, secCode=TICKER + "0", clock="09:00")


def base():
    return doc("SROOT", "240", secCode=OFFEROR_TICKER + "0")


def resolve(documents, *tickers, cutoff=AT):
    return resolve_tender_filing_references(
        documents, tickers=tickers or (TICKER,), known_at=cutoff
    )


def ids(documents, cutoff=AT):
    return {row.doc_id for row in resolve(documents, cutoff=cutoff)[TICKER]}


def test_registration_result_amendments_and_reverse_opinion_are_targeted_not_filer_matches():
    docs = [
        anchor(),
        base(),
        doc("SREGEDIT", "250", parent="SROOT"),
        doc("SRESULT", "270", parent="SREGEDIT", clock="11:00"),
        doc("SRESEDIT", "280", parent="SRESULT", clock="11:30"),
        doc("SOPINION", "290", parent="SROOT", filer=TARGET, subject=OFFEROR, clock="10:30"),
        doc("SBIGHOLD", "350", issuerEdinetCode=TARGET),
        doc("SBADOPINION", "290", parent="SROOT", subject=TARGET),
    ]
    result = resolve(list(reversed(docs)), OFFEROR_TICKER, TICKER)
    refs = {row.doc_id: row for row in result[TICKER]}
    assert set(refs) == {"SROOT", "SREGEDIT", "SRESULT", "SRESEDIT", "SOPINION"}
    assert result[OFFEROR_TICKER] == ()
    assert {k: v.model_dump(mode="json")["document_label"] for k, v in refs.items()} == {
        "SROOT": "公開買付届出書",
        "SREGEDIT": "訂正公開買付届出書",
        "SRESULT": "公開買付報告書",
        "SRESEDIT": "訂正公開買付報告書",
        "SOPINION": "意見表明報告書",
    }
    assert all(
        x.registration_doc_id == "SROOT" and x.source_date == date(2026, 6, 1)
        for x in refs.values()
    )
    assert (
        refs["SRESULT"].role,
        refs["SRESULT"].filer_edinet_code,
        refs["SRESULT"].subject_edinet_code,
    ) == ("offeror_filing", OFFEROR, TARGET)
    assert (
        refs["SOPINION"].role,
        refs["SOPINION"].filer_edinet_code,
        refs["SOPINION"].subject_edinet_code,
    ) == ("target_opinion", TARGET, OFFEROR)
    assert refs["SRESULT"].submitted_at.astimezone(UTC) == datetime(2026, 6, 1, 2, tzinfo=UTC)


def test_kind_and_parties_must_agree_at_each_parent_even_for_a_single_observed_registration():
    docs = [
        anchor(),
        base(),
        doc("SNOPARENT", "270"),
        doc("SOUTSIDE", "250", parent="SUNKNOWN"),
        doc("SWRONGKIND", "280", parent="SROOT"),
        doc("SOTHERBID", "270", parent="SROOT", filer="E91003"),
        doc("SOTHERTARGET", "270", parent="SROOT", subject="E91004"),
        doc("SNOACTOR", "270", parent="SROOT", filer=None),
        doc("SNOSUBJECT", "270", parent="SROOT", subject=None),
        doc("SEARLY", "270", parent="SROOT", clock="09:59"),
        doc("SNOTIME", "270", parent="SROOT", submitDateTime=None),
        doc("SBADANCESTOR", "280", parent="SOTHERBID", filer="E91003"),
    ]
    assert ids(docs) == {"SROOT"}


def test_separate_registrations_and_a_published_withdrawal_notice_remain_document_references():
    docs = [
        anchor(),
        base(),
        doc("SSIBLING", "240", filer="E91003"),
        doc("SNOTICE", "260", parent="SSIBLING", filer="E91003"),
        doc("SWRONGTIER", "270", parent="SSIBLING"),
    ]
    refs = {r.doc_id: r.registration_doc_id for r in resolve(docs)[TICKER]}
    assert refs == {"SROOT": "SROOT", "SSIBLING": "SSIBLING", "SNOTICE": "SSIBLING"}
    assert (
        next(r for r in resolve(docs)[TICKER] if r.doc_id == "SNOTICE").document_label
        == "公開買付撤回届出書"
    )


def test_same_parties_with_two_registration_ids_require_the_actual_parent():
    rows = [
        anchor(),
        base(),
        doc("SOTHER", "240", clock="10:15"),
        doc("STIERONE", "270", parent="SROOT", clock="11:00"),
        doc("STIERTWO", "270", parent="SOTHER", clock="11:05"),
        doc("SNOHINT", "270", clock="11:30"),
    ]
    refs = {r.doc_id: r.registration_doc_id for r in resolve(rows)[TICKER]}
    assert refs == {"SROOT": "SROOT", "SOTHER": "SOTHER", "STIERONE": "SROOT", "STIERTWO": "SOTHER"}


def test_a_later_metadata_edit_is_not_the_origin_date_or_the_submission_clock():
    result = doc("SRESULT", "270", parent="SROOT", clock="11:00")
    event = {
        "docID": "SRESULT",
        "doc_date": "2026-06-02",
        "seqNumber": 2,
        "docInfoEditStatus": "1",
        "opeDateTime": "2026-06-02 10:00",
        "docDescription": "報告書（表記更新）",
    }
    rows = resolve([anchor(), base(), result, event], cutoff=datetime(2026, 6, 2, 3, tzinfo=UTC))[
        TICKER
    ]
    filing = next(r for r in rows if r.doc_id == "SRESULT")
    assert filing.source_date == date(2026, 6, 1)
    assert filing.submitted_at.astimezone(UTC) == datetime(2026, 6, 1, 2, tzinfo=UTC)


@pytest.mark.parametrize("field", ["disclosureStatus", "withdrawalStatus", "legalStatus"])
def test_missing_explicit_usable_status_does_not_authorize_a_filing_or_its_descendants(field):
    assert ids([anchor(), {**base(), field: None}, doc("SRESULT", "270", parent="SROOT")]) == set()


def test_fold_before_lookup_without_folding_an_operation_from_after_the_knowledge_clock():
    result = doc("SRESULT", "270", parent="SROOT", clock="11:00")
    edit = {
        "docID": "SROOT",
        "doc_date": DAY,
        "seqNumber": 3,
        "docInfoEditStatus": "1",
        "opeDateTime": DAY + " 13:00",
        "subjectEdinetCode": None,
    }
    assert ids([anchor(), base(), result, edit]) == {"SROOT", "SRESULT"}
    assert ids([anchor(), base(), result, edit], datetime(2026, 6, 1, 4, tzinfo=UTC)) == set()
    for event in (
        {"docID": "SROOT", "disclosureStatus": "1"},
        {"docID": "SWITHDRAW", "parentDocID": "SROOT", "withdrawalStatus": "1"},
    ):
        visible = {**event, "doc_date": DAY, "seqNumber": 4, "opeDateTime": DAY + " 11:45"}
        assert ids([anchor(), base(), result, visible]) == set()


@pytest.mark.parametrize(
    "extra",
    [
        [doc("SIDENTITY", "120", filer="E91111", secCode=TICKER + "0")],
        [doc("SIDENTITY", "120", filer=TARGET, secCode="993A0")],
    ],
)
def test_identity_must_be_unambiguous_in_both_directions(extra):
    assert ids([anchor(), base(), *extra]) == set()
    assert ids([base()]) == set()


def test_future_identity_result_and_other_day_do_not_leak_into_a_historical_read():
    docs = [anchor(), base(), doc("SRESULT", "270", parent="SROOT", clock="12:01")]
    future = {**doc("SLATER", "120", filer=TARGET, secCode="993A0"), "doc_date": "2026-06-02"}
    assert ids([*docs, future]) == {"SROOT"}
    assert ids([{**anchor(), "submitDateTime": DAY + " 12:01"}, base()]) == set()
    with pytest.raises(ValueError, match="timezone"):
        resolve(docs, cutoff=AT.replace(tzinfo=None))
