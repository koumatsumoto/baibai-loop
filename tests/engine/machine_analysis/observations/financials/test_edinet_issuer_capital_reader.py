"""固定一覧と原本だけを読む。訂正・受領・hashの不明を古い数値で埋めない。"""

import io
import json
import zipfile
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from tests.helpers.edinet_capital_notes import block, source_zip, table
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.edinet.archive_native import (
    PREFIX as ARCHIVE_PREFIX,
)
from baibai_engine.data_collection.providers.edinet.archive_native import (
    FixedEdinetArchives,
    NativeArchive,
)
from baibai_engine.data_collection.providers.edinet.list_native import PREFIX as LIST_PREFIX
from baibai_engine.data_collection.providers.edinet.list_native import (
    FixedEdinetLists,
    NativeListReceipt,
)
from baibai_engine.data_collection.providers.edinet.models import EdinetResponse
from baibai_engine.machine_analysis.observations.financials.edinet_issuer_capital_reader import (
    read_issuer_capital_notes,
)
from baibai_engine.storage.objects.r2 import LakePublishError

AT = datetime(2026, 6, 12, 4, tzinfo=UTC)
KNOWN = AT + timedelta(days=1)
DAY = AT.date()
DOC_ID = "S100NOTE"


def document(**changes):
    return {
        "docID": DOC_ID,
        "seqNumber": 1,
        "secCode": "12340",
        "edinetCode": "E12345",
        "docTypeCode": "120",
        "parentDocID": None,
        "xbrlFlag": "1",
        "csvFlag": "1",
        "legalStatus": "1",
        "disclosureStatus": "0",
        "withdrawalStatus": "0",
        "docInfoEditStatus": "0",
        "periodStart": "2025-04-01",
        "periodEnd": "2026-03-31",
        "submitDateTime": "2026-06-12 10:00",
        **changes,
    }


def fixed(*, documents=None, bodies=None, received=AT, archived=AT, coarse=False):
    store = MemoryR2Store()
    documents = [document()] if documents is None else documents
    body = json.dumps(
        {"metadata": {"resultset": {"count": len(documents)}}, "results": documents}
    ).encode()
    receipt = NativeListReceipt.from_response(DAY, EdinetResponse(received, body), UUID(int=1).hex)
    entry = receipt._entry()
    store.store_object(LIST_PREFIX + "objects/" + entry["sha256"] + ".body.gz", receipt.body_gzip)
    lists = FixedEdinetLists(store, {"format": "edinet-list-native-v1", "receipts": [entry]})
    entries = []
    for doc_id, content in ({DOC_ID: source_zip()} if bodies is None else bodies).items():
        native = NativeArchive.from_response(doc_id, 1, EdinetResponse(archived, content))
        metadata = native.metadata.model_dump(mode="json")
        if coarse:
            metadata["received_at_utc"] = None
        entries.append(metadata)
        store.store_object(ARCHIVE_PREFIX + "objects/" + metadata["sha256"] + ".zip", content)
    archives = FixedEdinetArchives(
        store,
        {
            "format": "edinet-archive-native-v1",
            "archives": entries,
            "acquisition_scope": {"since": None, "doc_ids": [], "legacy_origins": []},
        },
    )
    store.get_keys.clear()
    store.head_keys.clear()
    return lists, archives, store


def read(lists, archives, **changes):
    return read_issuer_capital_notes(
        lists,
        archives,
        **{"tickers": ("1234",), "as_of": KNOWN.date(), "known_at": KNOWN, **changes},
    )


def test_fixed_original_pair_provenance_and_finite_scope_without_writes():
    lists, archives, store = fixed(
        documents=[document(), document(docID="S100OTHER", seqNumber=2, secCode="56780")],
        bodies={DOC_ID: source_zip(), "S100OTHER": b"not a zip"},
    )
    before = (lists.value(), archives.value())
    result = read(lists, archives, tickers=("1234", "9999", "1234"))
    assert list(result) == ["1234", "9999"]
    saved = result["1234"]
    assert saved.document_id == DOC_ID
    assert saved.archive == archives.metadata(DOC_ID, 1)
    assert saved.list_identity == lists.identity
    assert saved.archive_identity == archives.identity
    assert saved.submitted_at.isoformat() == "2026-06-12T10:00:00+09:00"
    (note,) = saved.extraction.observations
    assert (note.issued_display_count, note.treasury_display_count) == (100000, 500)
    assert result["9999"].extraction.reasons == ("capital_note_annual_family_unavailable",)
    assert not any(key.endswith("head.json") for key in store.get_keys + store.head_keys)
    assert len([key for key in store.get_keys if key.endswith(".zip")]) == 1
    assert store.put_keys == []
    assert before == (lists.value(), archives.value())


def test_latest_correction_uses_parent_period_without_resurrecting_parent_values():
    correction = document(
        docID="S100EDIT",
        seqNumber=2,
        docTypeCode="130",
        parentDocID=DOC_ID,
        periodStart=None,
        periodEnd=None,
        submitDateTime="2026-06-12 11:00",
    )
    lists, archives, _ = fixed(
        documents=[document(), correction],
        bodies={DOC_ID: source_zip(), "S100EDIT": source_zip(blocks=block(table(treasury="0")))},
    )
    result = read(lists, archives)["1234"]
    assert result.document_id == "S100EDIT"
    assert result.extraction.observations[0].treasury_display_count == 0
    lists, archives, _ = fixed(
        documents=[document(), correction],
        bodies={DOC_ID: source_zip(), "S100EDIT": source_zip(blocks="")},
    )
    result = read(lists, archives)["1234"]
    assert not result.extraction.observations
    assert result.extraction.reasons == ("capital_note_not_observed",)


@pytest.mark.parametrize("missing_archive", [True, False])
def test_unavailable_correction_never_falls_back(missing_archive):
    correction = document(
        docID="S100EDIT",
        seqNumber=2,
        docTypeCode="130",
        parentDocID=DOC_ID,
        submitDateTime="2026-06-12 11:00",
        xbrlFlag="1" if missing_archive else "0",
    )
    lists, archives, store = fixed(documents=[document(), correction])
    result = read(lists, archives)["1234"]
    assert result.document_id == "S100EDIT"
    assert not result.extraction.observations
    assert not any(key.endswith(".zip") for key in store.get_keys)


def test_orphan_correction_and_withdrawal_remain_unknown():
    orphan = document(
        docID="S100EDIT",
        seqNumber=2,
        docTypeCode="130",
        parentDocID="S100LOST",
        submitDateTime="2026-06-12 11:00",
    )
    withdrawn = document(
        docID="S100WITH",
        seqNumber=2,
        docTypeCode="130",
        parentDocID=DOC_ID,
        withdrawalStatus="1",
        submitDateTime="2026-06-12 11:00",
    )
    for changed in (orphan, withdrawn):
        lists, archives, _ = fixed(documents=[document(), changed])
        assert not read(lists, archives)["1234"].extraction.observations


def test_correction_period_conflict_is_not_replaced_by_parent_period():
    correction = document(
        docID="S100EDIT",
        seqNumber=2,
        docTypeCode="130",
        parentDocID=DOC_ID,
        periodEnd="2026-04-30",
        submitDateTime="2026-06-12 11:00",
    )
    lists, archives, store = fixed(documents=[document(), correction])
    assert read(lists, archives)["1234"].extraction.reasons == (
        "capital_note_revision_period_conflict",
    )
    assert not any(key.endswith(".zip") for key in store.get_keys)


def test_future_annual_period_is_not_a_current_capital_observation():
    lists, archives, store = fixed(documents=[document(periodEnd="2027-03-31")])
    result = read(lists, archives)["1234"]
    assert result.extraction.reasons == ("capital_note_period_or_issuer_unavailable",)
    assert not any(key.endswith(".zip") for key in store.get_keys)


def test_list_clock_mismatch_fails_before_reading_bodies():
    lists, archives, store = fixed(received=KNOWN + timedelta(seconds=1))
    with pytest.raises(ValueError, match="after the knowledge cutoff"):
        read(lists, archives)
    assert store.get_keys == []


@pytest.mark.parametrize(
    ("coarse", "archived"), [(False, KNOWN + timedelta(seconds=1)), (True, KNOWN)]
)
def test_late_or_same_day_coarse_archive_receipt_cannot_certify_cutoff(coarse, archived):
    lists, archives, store = fixed(coarse=coarse, archived=archived)
    result = read(lists, archives)["1234"]
    assert result.extraction.reasons == ("capital_note_archive_receipt_unconfirmed",)
    assert not any(key.endswith(".zip") for key in store.get_keys)


def test_older_coarse_receipt_remains_day_precision():
    lists, archives, _ = fixed(coarse=True)
    result = read(lists, archives)["1234"]
    assert result.archive.received_at_utc is None
    assert result.archive.stored_on == DAY
    assert result.extraction.observations


def test_same_day_edit_does_not_certify_the_retained_body_revision():
    edit = document(seqNumber=2, docInfoEditStatus="1")
    lists, archives, store = fixed(documents=[document(), edit])
    result = read(lists, archives)["1234"]
    assert result.extraction.reasons == ("capital_note_archive_revision_unconfirmed",)
    assert not any(key.endswith(".zip") for key in store.get_keys)


@pytest.mark.parametrize("failure", ["missing", "changed"])
def test_retained_original_integrity_failure_is_not_missing_disclosure(failure):
    lists, archives, store = fixed()
    key = next(key for key in store.values if key.endswith(".zip"))
    if failure == "missing":
        del store.values[key]
    else:
        store.store_object(key, b"changed")
    with pytest.raises(LakePublishError):
        read(lists, archives)


def test_unsupported_archive_is_a_per_issuer_reason_and_empty_scope_has_no_reads():
    lists, archives, store = fixed(bodies={DOC_ID: b"not a zip"})
    assert read(lists, archives, tickers=()) == {}
    assert not store.get_keys
    result = read(lists, archives)["1234"]
    assert result.extraction.reasons == ("zip_invalid",)
    assert not {"shares_outstanding", "rank", "market_cap"} & asdict(result).keys()


def test_retained_zip_crc_failure_stays_local_to_its_issuer():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as bundle:
        bundle.writestr("XBRL/PublicDoc/broken.xbrl", "<root>retained</root>")
    broken = bytearray(output.getvalue())
    broken[broken.index(b"retained")] ^= 1
    lists, archives, store = fixed(
        documents=[document(), document(docID="S100OTHER", seqNumber=2, secCode="56780")],
        bodies={DOC_ID: source_zip(), "S100OTHER": bytes(broken)},
    )
    results = read(lists, archives, tickers=("1234", "5678"))
    assert results["1234"].extraction.observations
    assert results["5678"].extraction.reasons == ("zip_member_invalid",)
    assert len([key for key in store.get_keys if key.endswith(".zip")]) == 2
