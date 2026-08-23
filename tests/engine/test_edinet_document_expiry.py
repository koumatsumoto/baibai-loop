"""EDINET stops serving a filing's description once its inspection period ends.

The day's list still returns the entry, so a re-list of an old day carries the same rows
with their company, form, timestamp, title and parent nulled. The ingest has to treat
that as an observation it can no longer make rather than as the filing saying nothing.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from baibai_engine.market.sqlite.schema import (
    EDINET_DOCUMENT_DESCRIPTIVE_COLUMNS,
    EDINET_DOCUMENT_IDENTITY_COLUMNS,
    EDINET_DOCUMENT_LIFECYCLE_COLUMNS,
    EDINET_DOCUMENT_RETAINED_COLUMNS,
)
from baibai_engine.screening.sqlite_cache import open_connection, store_edinet_documents

_DAY = date(2026, 5, 1)

_SERVED = {
    "seqNumber": 1,
    "docID": "S100AAAA",
    "secCode": "72030",
    "docTypeCode": "220",
    "parentDocID": "S100PARENT",
    "submitDateTime": "2026-05-01 15:49",
    "docDescription": "自己株券買付状況報告書",
    "csvFlag": "1",
    "xbrlFlag": "1",
    "legalStatus": "1",
    "disclosureStatus": "0",
    "withdrawalStatus": "0",
    "edinetCode": "E00001",
    "issuerEdinetCode": "E00002",
    "subjectEdinetCode": "E00003",
}

# What the same entry looks like once the inspection period has run out: the five
# descriptive fields are gone and the lifecycle fields have moved to the expired state.
_EXPIRED = {
    "seqNumber": 1,
    "docID": "S100AAAA",
    "secCode": None,
    "docTypeCode": None,
    "parentDocID": None,
    "submitDateTime": None,
    "docDescription": None,
    "csvFlag": "0",
    "xbrlFlag": "0",
    "legalStatus": "0",
    "disclosureStatus": "0",
    "withdrawalStatus": "0",
    "edinetCode": None,
    "issuerEdinetCode": None,
    "subjectEdinetCode": None,
}


def _read(path: Path) -> dict[str, str | None]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM edinet_documents").fetchone()
        return dict(zip(row.keys(), row, strict=True))
    finally:
        conn.close()


def _empty_store(path: Path) -> Path:
    open_connection(path).close()
    return path


def test_a_relisted_day_keeps_the_description_the_expired_response_no_longer_carries(
    tmp_path: Path,
) -> None:
    store = _empty_store(tmp_path / "market.sqlite")
    store_edinet_documents(store, _DAY, [_SERVED])

    store_edinet_documents(store, _DAY, [_EXPIRED])

    row = _read(store)
    assert row["sec_code"] == "72030"
    assert row["doc_type_code"] == "220"
    assert row["parent_doc_id"] == "S100PARENT"
    assert row["submit_datetime"] == "2026-05-01 15:49"
    assert row["doc_description"] == "自己株券買付状況報告書"


def test_a_relisted_day_takes_the_new_lifecycle_answer(tmp_path: Path) -> None:
    """The expiry is the whole point of reading the day again."""

    store = _empty_store(tmp_path / "market.sqlite")
    store_edinet_documents(store, _DAY, [_SERVED])

    store_edinet_documents(store, _DAY, [_EXPIRED])

    row = _read(store)
    assert row["legal_status"] == "0"
    assert row["csv_flag"] == "0"
    assert row["xbrl_flag"] == "0"


def test_a_relisted_day_keeps_the_identity_the_expired_response_no_longer_carries(
    tmp_path: Path,
) -> None:
    store = _empty_store(tmp_path / "market.sqlite")
    store_edinet_documents(store, _DAY, [_SERVED])

    store_edinet_documents(store, _DAY, [_EXPIRED])

    row = _read(store)
    assert row["edinet_code"] == "E00001"
    assert row["issuer_edinet_code"] == "E00002"
    assert row["subject_edinet_code"] == "E00003"


def test_a_withdrawal_observed_on_a_later_list_replaces_the_stored_status(
    tmp_path: Path,
) -> None:
    store = _empty_store(tmp_path / "market.sqlite")
    store_edinet_documents(store, _DAY, [_SERVED])

    store_edinet_documents(store, _DAY, [{**_SERVED, "withdrawalStatus": "2"}])

    assert _read(store)["withdrawal_status"] == "2"


def test_a_served_response_fills_a_row_first_stored_after_its_period_ended(
    tmp_path: Path,
) -> None:
    """A store that first read the day late holds nulls no fetch can fill later."""

    store = _empty_store(tmp_path / "market.sqlite")
    store_edinet_documents(store, _DAY, [_EXPIRED])

    store_edinet_documents(store, _DAY, [_SERVED])

    row = _read(store)
    assert row["doc_type_code"] == "220"
    assert row["legal_status"] == "1"


def test_a_description_is_not_carried_across_a_different_document(tmp_path: Path) -> None:
    """A position the response fills with another filing keeps that filing's answer."""

    store = _empty_store(tmp_path / "market.sqlite")
    store_edinet_documents(store, _DAY, [_SERVED])

    store_edinet_documents(store, _DAY, [{**_EXPIRED, "docID": "S100BBBB"}])

    row = _read(store)
    assert row["doc_id"] == "S100BBBB"
    assert row["sec_code"] is None
    assert row["doc_type_code"] is None


# EDINET returns one document id at two positions in a day with different descriptions:
# 113 days in the store do it, and 2026-01-29 `S100XCUA` even differs in form code
# between its two rows. Retained values therefore cannot be held under the document id.
_TWO_POSITIONS = (
    {
        "seqNumber": 204,
        "docID": "S100XCUA",
        "docTypeCode": "350",
        "parentDocID": None,
        "submitDateTime": "2026-01-29 09:00",
        "docDescription": "大量保有報告書",
        "legalStatus": "1",
        "withdrawalStatus": "0",
    },
    {
        "seqNumber": 206,
        "docID": "S100XCUA",
        "docTypeCode": "360",
        "parentDocID": "S100X9JM",
        "submitDateTime": "2026-01-29 09:05",
        "docDescription": "変更報告書",
        "legalStatus": "1",
        "withdrawalStatus": "0",
    },
)


def _rows_by_position(path: Path) -> dict[int, dict[str, str | None]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM edinet_documents").fetchall()
        return {
            int(row["sequence_number"]): dict(zip(row.keys(), row, strict=True)) for row in rows
        }
    finally:
        conn.close()


def test_two_positions_sharing_a_document_id_keep_their_own_descriptions(
    tmp_path: Path,
) -> None:
    """Re-listing an unchanged day must not move one position's answer to the other."""

    store = _empty_store(tmp_path / "market.sqlite")
    store_edinet_documents(store, _DAY, list(_TWO_POSITIONS))

    store_edinet_documents(store, _DAY, list(_TWO_POSITIONS))

    rows = _rows_by_position(store)
    assert rows[204]["doc_type_code"] == "350"
    assert rows[204]["parent_doc_id"] is None
    assert rows[206]["doc_type_code"] == "360"
    assert rows[206]["parent_doc_id"] == "S100X9JM"


def test_two_positions_sharing_a_document_id_expire_without_mixing(tmp_path: Path) -> None:
    store = _empty_store(tmp_path / "market.sqlite")
    store_edinet_documents(store, _DAY, list(_TWO_POSITIONS))

    store_edinet_documents(
        store,
        _DAY,
        [
            {**_EXPIRED, "seqNumber": 204, "docID": "S100XCUA"},
            {**_EXPIRED, "seqNumber": 206, "docID": "S100XCUA"},
        ],
    )

    rows = _rows_by_position(store)
    assert rows[204]["doc_type_code"] == "350"
    assert rows[204]["parent_doc_id"] is None
    assert rows[206]["doc_type_code"] == "360"
    assert rows[206]["parent_doc_id"] == "S100X9JM"


def test_a_column_outside_the_two_classifications_takes_the_new_answer(
    tmp_path: Path,
) -> None:
    """Only the five columns EDINET withdraws on expiry survive a blanking response."""

    store = _empty_store(tmp_path / "market.sqlite")
    store_edinet_documents(store, _DAY, [{**_SERVED, "opeDateTime": "2026-05-01 15:50"}])

    store_edinet_documents(store, _DAY, [_EXPIRED])

    assert _read(store)["operation_datetime"] is None


def test_the_retained_columns_are_the_ones_the_schema_classifies(tmp_path: Path) -> None:
    """The ingest reads a fixed column list; this pins it to the classification."""

    store = _empty_store(tmp_path / "market.sqlite")
    store_edinet_documents(store, _DAY, [_SERVED])
    store_edinet_documents(store, _DAY, [_EXPIRED])

    row = _read(store)
    retained = {column for column in EDINET_DOCUMENT_RETAINED_COLUMNS if row[column] is not None}
    assert retained == set(EDINET_DOCUMENT_RETAINED_COLUMNS)
    assert set(EDINET_DOCUMENT_RETAINED_COLUMNS) == (
        set(EDINET_DOCUMENT_DESCRIPTIVE_COLUMNS) | set(EDINET_DOCUMENT_IDENTITY_COLUMNS)
    )
    assert set(EDINET_DOCUMENT_RETAINED_COLUMNS) & set(EDINET_DOCUMENT_LIFECYCLE_COLUMNS) == set()
