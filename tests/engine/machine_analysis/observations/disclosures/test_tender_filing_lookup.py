"""一覧の原番号・選定受領時計と有限範囲だけで著者向け参照を作る。"""

import json
from contextlib import closing
from datetime import UTC, timedelta
from uuid import uuid4

import pytest
from tests.helpers.proposal import PREPARED

from baibai_engine.machine_analysis.observations.disclosures.tender_filing_lookup import (
    read_tender_filing_references,
)
from baibai_engine.primitives.time import JST
from baibai_engine.storage.sqlite.source_cache.schema import open_connection


def _seed(conn, day, received, *, items=None, count=None, latest=None, source=None):
    items = items or []
    source = source or json.dumps(
        {
            "metadata": {
                "status": "200",
                "parameter": {"date": str(day)},
                "resultset": {"count": len(items)},
            },
            "results": items,
        }
    )

    def stamp(value):
        return value.astimezone(UTC).isoformat(timespec="microseconds")

    conn.execute(
        "INSERT INTO edinet_document_lists(doc_date,result_count,response_json,selected_receipt_id,"
        "selected_received_at_utc,latest_received_at_utc) VALUES(?,?,?,?,?,?)",
        (
            str(day),
            len(items) if count is None else count,
            source,
            str(uuid4()),
            stamp(received),
            stamp(latest or received),
        ),
    )


def _read(conn, at=PREPARED):
    return read_tender_filing_references(conn, tickers=("1234",), known_at=at)


def test_no_jpx_target_never_reads_the_list_and_no_source_is_not_a_negative(tmp_path):
    with closing(open_connection(tmp_path / "market.sqlite")) as conn:
        calls = []
        conn.set_trace_callback(calls.append)
        assert read_tender_filing_references(conn, tickers=(), known_at=PREPARED) is None
        assert calls == []
        assert _read(conn).status == "no_eligible_list"
    assert _read(None).references_by_ticker is None
    with pytest.raises(ValueError, match="timezone"):
        _read(None, PREPARED.replace(tzinfo=None))


def test_scope_is_jst_calendar_90_days_not_a_complete_list_or_full_replay(tmp_path):
    cutoff = PREPARED.replace(hour=0, minute=1)
    today = cutoff.astimezone(JST).date()
    with closing(open_connection(tmp_path / "market.sqlite")) as conn:
        _seed(conn, today - timedelta(days=90), cutoff - timedelta(days=90), source="broken")
        _seed(conn, today - timedelta(days=89), cutoff - timedelta(days=89))
        _seed(conn, today, cutoff)
        check = _read(conn, cutoff)
        assert check.status == "observed_lists"
        assert check.selected_list_days == 2
        assert (check.window_start, check.window_end) == (today - timedelta(days=89), today)
        assert check.latest_list_received_at == cutoff
        assert check.references_by_ticker == {"1234": ()}
        _seed(conn, today - timedelta(days=2), cutoff + timedelta(microseconds=1), source="broken")
        assert _read(conn, cutoff).status == "as_of_history_unavailable"
        assert _read(conn, cutoff + timedelta(microseconds=1)).status == "unreadable_list"


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("response_json", "not JSON"),
        ("result_count", 3),
        ("selected_receipt_id", None),
        ("selected_received_at_utc", None),
        ("response_json", None),
        ("response_json", '{"metadata":{"resultset":{"count":1}},"results":[]}'),
        ("response_json", '{"metadata":{"resultset":{"count":0},"status":"500"},"results":[]}'),
        ("selected_receipt_id", "not-a-receipt"),
    ],
)
def test_damaged_selected_projection_is_unknown_not_an_empty_observed_set(tmp_path, column, value):
    with closing(open_connection(tmp_path / "market.sqlite")) as conn:
        _seed(conn, PREPARED.date(), PREPARED)
        conn.execute(f"UPDATE edinet_document_lists SET {column}=?", (value,))
        check = _read(conn)
        assert check.status == "unreadable_list"
        assert check.references_by_ticker is None


def test_known_failed_refresh_does_not_return_stale_references(tmp_path):
    with closing(open_connection(tmp_path / "market.sqlite")) as conn:
        later = PREPARED + timedelta(seconds=1)
        _seed(conn, PREPARED.date(), PREPARED, latest=later)
        assert _read(conn, PREPARED).status == "as_of_history_unavailable"
        assert _read(conn, later).status == "unreadable_list"


def test_observed_list_with_invalid_submission_clock_stays_unreadable(tmp_path):
    with closing(open_connection(tmp_path / "market.sqlite")) as conn:
        _seed(
            conn,
            PREPARED.date(),
            PREPARED,
            items=[{"docID": "SMALFORM", "seqNumber": 1, "submitDateTime": 123}],
        )
        assert _read(conn).status == "unreadable_list"


@pytest.mark.parametrize("failure", [True, False], ids=["intervening_failure", "withdrawn_parent"])
def test_later_receipt_must_not_resurrect_a_filing_at_the_same_old_cutoff(tmp_path, failure):
    from baibai_engine.data_collection.providers.edinet.list_projection import project_document_list
    from baibai_engine.data_collection.providers.edinet.receipts import EdinetListReceipt
    from baibai_engine.machine_analysis.observations.disclosures.edinet_list_rebuild import (
        store_edinet_document_projection,
    )

    market = tmp_path / "market.sqlite"
    first = PREPARED - timedelta(days=1)
    day = first.date()
    fields = {
        "legalStatus": "1",
        "disclosureStatus": "0",
        "withdrawalStatus": "0",
        "docInfoEditStatus": "0",
        "submitDateTime": f"{day} 09:00",
    }

    def receipt(day, stamp, docs):
        source = (
            b"broken"
            if docs is None
            else json.dumps(
                {
                    "metadata": {"resultset": {"count": len(docs)}},
                    "results": docs,
                }
            ).encode()
        )
        return EdinetListReceipt(str(uuid4()), day, stamp.astimezone(UTC), source)

    def persist(receipts):
        store_edinet_document_projection(
            market, project_document_list(receipts), rechecked_asof=None
        )

    def read():
        with closing(open_connection(market)) as conn:
            return _read(conn, PREPARED)

    original = receipt(
        day,
        first,
        [
            dict(
                fields,
                seqNumber=1,
                docID="SFICTANC",
                docTypeCode="120",
                edinetCode="E90001",
                secCode="12340",
            ),
            dict(
                fields,
                seqNumber=2,
                docID="SFICTREG",
                docTypeCode="240",
                edinetCode="E90002",
                subjectEdinetCode="E90001",
            ),
        ],
    )
    persist([original])
    assert [r.doc_id for r in read().references_by_ticker["1234"]] == ["SFICTREG"]
    if failure:
        history = [original, receipt(day, PREPARED, None)]
        persist(history)
        assert read().status == "unreadable_list"
        persist([*history, receipt(day, PREPARED + timedelta(hours=1), None)])
    else:
        new_day = PREPARED.date()
        operation = [
            {
                "seqNumber": 1,
                "docID": "SFICTWDR",
                "parentDocID": "SFICTREG",
                "withdrawalStatus": "1",
                "opeDateTime": f"{new_day} 09:00",
            }
        ]
        withdrawals = [receipt(new_day, PREPARED, operation)]
        persist(withdrawals)
        assert read().references_by_ticker == {"1234": ()}
        persist([*withdrawals, receipt(new_day, PREPARED + timedelta(hours=1), operation)])
    later = read()
    assert later.status == "as_of_history_unavailable"
    assert later.references_by_ticker is None
