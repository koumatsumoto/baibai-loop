from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from baibai_engine.collection.edinet.receipts import EdinetListReceipt
from baibai_engine.market.edinet_list_projection import project_document_list

_DAY = date(2026, 10, 15)
_START = datetime(2026, 10, 15, 0, 0, tzinfo=UTC)
_NATIVE = {
    "secCode": "20000",
    "docTypeCode": "120",
    "parentDocID": "S100PARENT",
    "submitDateTime": "2026-10-15T09:00",
    "docDescription": "有価証券報告書",
    "edinetCode": "E12345",
    "issuerEdinetCode": "E54321",
    "subjectEdinetCode": "E11111",
}


def _payload(rows: list[dict[str, object]], **metadata: object) -> dict[str, object]:
    return {
        "metadata": {"resultset": {"count": len(rows)}, **metadata},
        "results": rows,
        "futureField": {"null": None},
    }


def _receipt(
    receipt_id: str,
    seconds: int,
    payload: object,
    *,
    doc_date: date = _DAY,
    received_at: datetime | None = None,
) -> EdinetListReceipt:
    body = (
        payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
    )
    return EdinetListReceipt(
        receipt_id=receipt_id,
        doc_date=doc_date,
        received_at=received_at or _START + timedelta(seconds=seconds),
        body=body,
    )


def _origin(**overrides: object) -> dict[str, object]:
    return {"seqNumber": 1, "docID": "S100TEST", "legalStatus": "1", **_NATIVE, **overrides}


def _unavailable(**overrides: object) -> dict[str, object]:
    return {"seqNumber": 1, "docID": "S100TEST", "legalStatus": "0", **overrides}


def test_shuffled_receipts_carry_only_unavailable_origin_metadata() -> None:
    first = _receipt("first", 1, _payload([_origin(csvFlag="1", xbrlFlag="1")]))
    last_row = _unavailable(
        secCode="",
        docDescription=None,
        csvFlag=None,
        legalStatus="0",
        withdrawalStatus="2",
        disclosureStatus="2",
    )
    last = _receipt("last", 2, _payload([last_row], parameter={"date": _DAY.isoformat()}))

    projection = project_document_list([last, first])

    assert projection.selected_receipt == last
    assert projection.latest_received_at == last.received_at
    assert projection.error is None
    assert projection.documents == (
        {**last_row, **_NATIVE, "doc_date": _DAY.isoformat(), "csvFlag": None},
    )
    assert "xbrlFlag" not in projection.documents[0]
    assert projection.documents[0]["legalStatus"] == "0"
    assert projection.documents[0]["withdrawalStatus"] == "2"
    assert projection.documents[0]["disclosureStatus"] == "2"
    assert projection.native_payload == json.loads(last.body)
    assert projection.native_payload["results"] == [last_row]
    assert "doc_date" not in projection.native_payload["results"][0]


@pytest.mark.parametrize(
    "latest",
    [
        {"seqNumber": 1, "docID": "S100TEST", "legalStatus": "1", "docDescription": None},
        {
            "seqNumber": 1,
            "docID": "S100TEST",
            "legalStatus": "1",
            "disclosureStatus": "2",
            "docDescription": None,
        },
        {
            "seqNumber": 1,
            "docID": "S100TEST",
            "legalStatus": "0",
            "docInfoEditStatus": "1",
            "docDescription": None,
        },
        {
            "seqNumber": 1,
            "docID": "S100TEST",
            "legalStatus": "0",
            "withdrawalStatus": "1",
            "docDescription": None,
        },
        {
            "seqNumber": 1,
            "docID": "S100TEST",
            "legalStatus": "0",
            "disclosureStatus": "3",
            "docDescription": None,
        },
    ],
    ids=["normal", "nondisclosure", "edit", "withdrawal", "disclosure"],
)
def test_present_null_in_normal_origin_or_operation_is_not_inherited(
    latest: dict[str, object],
) -> None:
    projection = project_document_list(
        [_receipt("first", 1, _payload([_origin()])), _receipt("last", 2, _payload([latest]))]
    )
    assert projection.error is None
    assert projection.documents[0]["docDescription"] is None
    assert "secCode" not in projection.documents[0]


@pytest.mark.parametrize(
    "latest",
    [
        {"seqNumber": 1, "docID": "S100OTHER", "legalStatus": "0"},
        {"seqNumber": 2, "docID": "S100TEST", "legalStatus": "0"},
    ],
    ids=["different-document-at-same-position", "same-document-at-different-position"],
)
def test_metadata_does_not_cross_receipt_row_identity(latest: dict[str, object]) -> None:
    projection = project_document_list(
        [_receipt("first", 1, _payload([_origin()])), _receipt("last", 2, _payload([latest]))]
    )
    assert projection.error is None
    assert projection.documents == ({**latest, "doc_date": _DAY.isoformat()},)


def test_valid_empty_list_resets_observed_history() -> None:
    projection = project_document_list(
        [
            _receipt("first", 1, _payload([_origin()])),
            _receipt("empty", 2, _payload([])),
            _receipt("last", 3, _payload([_unavailable()])),
        ]
    )
    assert projection.error is None
    assert projection.selected_receipt is not None
    assert projection.selected_receipt.receipt_id == "last"
    assert projection.documents == ({**_unavailable(), "doc_date": _DAY.isoformat()},)


def test_malformed_latest_retains_selected_older_payload_with_error() -> None:
    first = _receipt("first", 1, _payload([_origin()]))
    bad = _receipt("bad", 2, b"{not JSON")
    projection = project_document_list([bad, first])
    assert projection.selected_receipt == first
    assert projection.latest_received_at == bad.received_at
    assert projection.native_payload == json.loads(first.body)
    assert projection.documents[0]["docDescription"] == _NATIVE["docDescription"]
    assert projection.error is not None
    assert "JSONDecodeError" in projection.error


def test_malformed_intermediate_receipt_does_not_clear_prior_history() -> None:
    first = _receipt("first", 1, _payload([_origin()]))
    bad = _receipt("bad", 2, _payload([], status="500"))
    last = _receipt("last", 3, _payload([_unavailable()]))
    projection = project_document_list([last, bad, first])
    assert projection.error is None
    assert projection.selected_receipt == last
    assert projection.documents[0]["docDescription"] == _NATIVE["docDescription"]


def test_distinct_bodies_at_same_time_are_ambiguous_without_uuid_chronology() -> None:
    first = _receipt("first", 1, _payload([_origin()]))
    stamp = _START + timedelta(seconds=2)
    tied = [
        _receipt("z", 2, _payload([_unavailable()]), received_at=stamp),
        _receipt("a", 2, _payload([]), received_at=stamp),
    ]
    for receipts in ([first, *tied], [tied[1], first, tied[0]]):
        projection = project_document_list(receipts)
        assert projection.selected_receipt == first
        assert projection.native_payload == json.loads(first.body)
        assert projection.latest_received_at == stamp
        assert projection.error == "EDINET receipt order is ambiguous at the same received time"


def test_ambiguous_intermediate_time_stops_before_later_expired_row() -> None:
    first = _receipt("first", 1, _payload([_origin()]))
    tied = [
        _receipt("b1", 2, _payload([_origin(docDescription="改訂A")])),
        _receipt("b2", 2, _payload([_origin(docDescription="改訂B")])),
    ]
    last = _receipt("last", 3, _payload([_unavailable()]))
    projection = project_document_list([last, tied[1], first, tied[0]])
    assert projection.selected_receipt == first
    assert projection.documents[0]["docDescription"] == _NATIVE["docDescription"]
    assert projection.latest_received_at == last.received_at
    assert projection.error == "EDINET receipt order is ambiguous at the same received time"


def test_identical_bodies_at_same_time_choose_stable_provenance_only() -> None:
    first = _receipt("z", 1, _payload([_origin()]))
    duplicate = _receipt("a", 1, _payload([_origin()]))
    for receipts in ([first, duplicate], [duplicate, first]):
        projection = project_document_list(receipts)
        assert projection.selected_receipt == duplicate
        assert projection.error is None
        assert projection.documents[0]["docDescription"] == _NATIVE["docDescription"]


@pytest.mark.parametrize(
    ("payload", "error_fragment"),
    [
        (_payload([_origin(seqNumber=True)]), "seqNumber"),
        (_payload([_origin(seqNumber=0)]), "seqNumber"),
        (_payload([_origin(seqNumber="１２")]), "seqNumber"),
        (_payload([_origin(), _origin(docID="S100SECOND")]), "seqNumber"),
        (_payload([_origin(legalStatus="9")]), "legal status"),
        ({"metadata": {"resultset": {"count": 2}}, "results": [_origin()]}, "count"),
        ({"metadata": {"resultset": {"count": "invalid"}}, "results": []}, "count"),
        ({"metadata": {"resultset": {"count": 0}}}, "results"),
        (_payload([_origin()], status="500"), "status"),
        (_payload([_origin()], parameter={"date": "2026-10-14"}), "date"),
        (_payload([_origin(docID="invalid")]), "docID"),
    ],
    ids=[
        "boolean-sequence",
        "zero-sequence",
        "non-ascii-sequence",
        "duplicate-sequence",
        "invalid-document-status",
        "count-mismatch",
        "invalid-count",
        "missing-results-even-when-count-zero",
        "metadata-status",
        "requested-date-mismatch",
        "invalid-document-id",
    ],
)
def test_invalid_receipt_is_rejected(payload: object, error_fragment: str) -> None:
    projection = project_document_list([_receipt("invalid", 1, payload)])
    assert projection.selected_receipt is None
    assert projection.native_payload is None
    assert projection.documents == ()
    assert projection.error is not None
    assert error_fragment in projection.error


def test_no_previous_sql_state_is_consulted() -> None:
    receipt = _receipt("only", 1, _payload([_unavailable()]))
    with patch("sqlite3.connect", side_effect=AssertionError("SQL state consulted")):
        projection = project_document_list([receipt])
    assert projection.error is None
    assert projection.documents == ({**_unavailable(), "doc_date": _DAY.isoformat()},)


def test_receipts_must_have_one_day_and_utc_times() -> None:
    first = _receipt("first", 1, _payload([]))
    with pytest.raises(ValueError, match="at least one receipt"):
        project_document_list([])
    with pytest.raises(ValueError, match="one requested day"):
        project_document_list(
            [first, _receipt("other", 2, _payload([]), doc_date=date(2026, 10, 14))]
        )
    local_time = _START.astimezone(timezone(timedelta(hours=9)))
    with pytest.raises(ValueError, match="must be UTC"):
        project_document_list([_receipt("local", 1, _payload([]), received_at=local_time)])
