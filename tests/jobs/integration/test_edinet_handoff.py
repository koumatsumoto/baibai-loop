import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from tests.helpers.edinet_handoff_inputs import DAY, STAMP, projection_state, receive
from tests.helpers.edinet_saved_inputs import BASE, RECORDED, TARGET, _seed

from baibai_engine.data_collection.providers.edinet.archives import (
    archive_keys,
    retain_source_archive,
)
from baibai_engine.data_collection.providers.edinet.receipts import retain_list_response
from baibai_engine.machine_analysis.observations.disclosures.edinet_document_store import (
    read_edinet_documents,
    read_observed_documents,
)
from baibai_engine.machine_analysis.observations.disclosures.edinet_list_rebuild import (
    rebuild_edinet_document_day,
)
from baibai_engine.machine_analysis.observations.financials.edinet_integration import (
    extract_saved_edinet,
)
from baibai_engine.machine_analysis.observations.financials.edinet_reader import EDINETReader
from baibai_engine.storage.sqlite.source_cache.coverage import range_covered
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.integration.edinet_handoff import export_handoff, import_handoff


def test_late_edinet_list_uses_all_retained_receipts_without_custom_lww(tmp_path):
    source, target, stage = (
        tmp_path / name for name in ("source.sqlite", "target.sqlite", "stage.sqlite")
    )
    receive(source, "S100OLD", STAMP)
    receive(target, "S100NEW", STAMP + timedelta(hours=1))
    rebuild_edinet_document_day(target, DAY)
    export_handoff(source, stage, since=STAMP, archives=set())
    result = import_handoff(stage, target)
    assert result.complete is True
    assert result.report["failed_days"] == []
    assert [item["docID"] for item in read_edinet_documents(target, DAY)] == ["S100NEW"]
    with closing(open_connection(target)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM edinet_list_responses").fetchone()[0] == 2
    with closing(open_connection(stage)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM edinet_document_lists").fetchone()[0] == 0
    assert import_handoff(stage, target).report["raw_rows_added"] == 0


def test_edinet_archive_first_retained_bytes_are_preserved_and_conflict_reported(tmp_path):
    source, target, stage = (
        tmp_path / name for name in ("source.sqlite", "target.sqlite", "stage.sqlite")
    )
    with closing(open_connection(source)) as conn:
        retain_source_archive(conn, "S100SAME", 5, b"older-copy", stored_on=DAY)
        retain_source_archive(conn, "S100NEW", 1, b"new-xbrl", stored_on=DAY)
    with closing(open_connection(target)) as conn:
        retain_source_archive(conn, "S100SAME", 5, b"canonical", stored_on=DAY)
        retain_source_archive(conn, "S100OTHER", 5, b"unrelated", stored_on=DAY)
    with closing(open_connection(source)) as connection:
        keys = archive_keys(connection)
    export_handoff(source, stage, since=STAMP, archives=keys)
    result = import_handoff(stage, target)
    assert result.complete is False
    assert result.report["archive_conflicts"] == [["S100SAME", 5]]
    with closing(open_connection(target)) as conn:
        assert conn.execute(
            "SELECT doc_id, zip_content FROM edinet_source_archives ORDER BY doc_id"
        ).fetchall() == [
            ("S100NEW", b"new-xbrl"),
            ("S100OTHER", b"unrelated"),
            ("S100SAME", b"canonical"),
        ]


@pytest.mark.parametrize("new_day", [False, True], ids=["old-correction", "missing-day"])
def test_malformed_day_preserves_prior_projection_without_hiding_later_good_day(tmp_path, new_day):
    target, bad_source = tmp_path / "target.sqlite", tmp_path / "bad.sqlite"
    receive(target, "S100OLD", STAMP)
    rebuild_edinet_document_day(target, DAY)
    before = projection_state(target)
    bad_day = DAY + timedelta(days=int(new_day))
    good_day = bad_day + timedelta(days=1)
    bad_stamp = STAMP + timedelta(days=1, hours=1)
    with closing(open_connection(bad_source)) as conn:
        bad_receipt = retain_list_response(
            conn, doc_date=bad_day, received_at=bad_stamp, body=b"{invalid JSON"
        )
    bad_packet = tmp_path / "bad-packet.sqlite"
    export_handoff(bad_source, bad_packet, since=STAMP, archives=set())
    result = import_handoff(bad_packet, target)
    assert result.complete is False
    assert result.report["failed_days"] == [bad_day.isoformat()]
    assert bad_day.isoformat() not in result.report["replayed_days"]
    if new_day:
        initial = projection_state(target)
        assert initial[0] == before[0]
        assert initial[1][0] == before[1][0]
        assert initial[2][0] == before[2][0]
        with closing(open_connection(target)) as conn:
            assert conn.execute(
                "SELECT selected_receipt_id, latest_received_at_utc FROM edinet_document_lists "
                "WHERE doc_date = ?",
                (bad_day.isoformat(),),
            ).fetchone() == (None, bad_stamp.isoformat(timespec="microseconds"))
            assert conn.execute(
                "SELECT record_count, status FROM source_coverage "
                "WHERE source = 'edinet_documents' AND coverage_key = ?",
                (bad_day.isoformat(),),
            ).fetchone() == (0, "failed")
        assert read_edinet_documents(target, bad_day) is None
    else:
        assert projection_state(target) == before

    good_source, good_packet = tmp_path / "good.sqlite", tmp_path / "good-packet.sqlite"
    good_stamp = STAMP + timedelta(days=2)
    good_receipt = receive(good_source, "S100GOOD", good_stamp, day=good_day)
    export_handoff(good_source, good_packet, since=STAMP, archives=set())
    delayed = import_handoff(good_packet, target)
    assert delayed.complete is False
    assert delayed.report["failed_days"] == [bad_day.isoformat()]
    assert good_day.isoformat() in delayed.report["replayed_days"]
    assert bad_day.isoformat() not in delayed.report["replayed_days"]
    # The valid later day is not blocked or relabeled by another day's bad raw.
    after = projection_state(target)
    assert all(actual[0] == original[0] for actual, original in zip(after, before, strict=True))
    with closing(open_connection(target)) as conn:
        # Partial publication never turns an unconfirmed day into readable coverage.
        assert range_covered(conn, "edinet_documents", DAY, good_day) is (not new_day)
        assert conn.execute(
            "SELECT receipt_id FROM edinet_list_responses WHERE receipt_id IN (?, ?) "
            "ORDER BY receipt_id",
            (bad_receipt, good_receipt),
        ).fetchall() == [(value,) for value in sorted((bad_receipt, good_receipt))]
    # A subsequent integration still reports invalid raw without a new handoff.
    pending = extract_saved_edinet(target, asof=good_day, lookback_days=0)
    assert pending["edinet-list"]["failed_days"] == [bad_day.isoformat()]
    assert pending["edinet-metrics"]["input_asof"] == good_day.isoformat()

    repaired_source, repaired_packet = (
        tmp_path / "repaired.sqlite",
        tmp_path / "repaired-packet.sqlite",
    )
    repaired_stamp = good_stamp + timedelta(hours=1)
    repaired_receipt = receive(repaired_source, "S100NEW", repaired_stamp, day=bad_day)
    export_handoff(repaired_source, repaired_packet, since=STAMP, archives=set())
    assert import_handoff(repaired_packet, target).report["failed_days"] == []
    # Replaying the existing good receipt must keep its original clock.
    assert import_handoff(good_packet, target).report["failed_days"] == []
    assert [item["docID"] for item in read_edinet_documents(target, bad_day)] == ["S100NEW"]
    assert [item["docID"] for item in read_edinet_documents(target, good_day)] == ["S100GOOD"]
    with closing(open_connection(target)) as conn:
        assert conn.execute(
            "SELECT doc_date, selected_receipt_id, selected_received_at_utc, latest_received_at_utc "
            "FROM edinet_document_lists WHERE doc_date IN (?, ?) ORDER BY doc_date",
            (bad_day.isoformat(), good_day.isoformat()),
        ).fetchall() == [
            (
                day.isoformat(),
                receipt,
                stamp.isoformat(timespec="microseconds"),
                stamp.isoformat(timespec="microseconds"),
            )
            for day, receipt, stamp in (
                (bad_day, repaired_receipt, repaired_stamp),
                (good_day, good_receipt, good_stamp),
            )
        ]
    assert "edinet-list" not in extract_saved_edinet(target, asof=good_day, lookback_days=0)


@pytest.mark.parametrize(
    "initial_empty", [False, True], ids=["selected-evidence", "selected-empty"]
)
def test_initial_failed_projection_retains_evidence_then_recovers_with_normal_empty(
    tmp_path, initial_empty
):
    source, target, packet = (
        tmp_path / name for name in ("source.sqlite", "target.sqlite", "packet.sqlite")
    )
    records = [] if initial_empty else [{"seqNumber": 1, "docID": "S100OLD", "secCode": "72030"}]
    failed_stamp = STAMP + timedelta(hours=1)
    with closing(open_connection(source)) as conn:
        selected_receipt = retain_list_response(
            conn,
            doc_date=DAY,
            received_at=STAMP,
            body=json.dumps(
                {"metadata": {"resultset": {"count": len(records)}}, "results": records}
            ).encode(),
        )
        retain_list_response(conn, doc_date=DAY, received_at=failed_stamp, body=b"{invalid JSON")
    export_handoff(source, packet, since=STAMP, archives=set())
    outcome = import_handoff(packet, target)
    assert outcome.complete is False
    assert outcome.report["failed_days"] == [DAY.isoformat()]
    assert outcome.report["replayed_days"] == []
    assert read_edinet_documents(target, DAY) is None
    with closing(open_connection(target)) as conn:
        assert [item["docID"] for item in read_observed_documents(conn, through=DAY)] == (
            [] if initial_empty else ["S100OLD"]
        )
        assert conn.execute(
            "SELECT selected_receipt_id, selected_received_at_utc, latest_received_at_utc "
            "FROM edinet_document_lists WHERE doc_date = ?",
            (DAY.isoformat(),),
        ).fetchone() == (
            selected_receipt,
            STAMP.isoformat(timespec="microseconds"),
            failed_stamp.isoformat(timespec="microseconds"),
        )
        assert conn.execute(
            "SELECT record_count, status, fetched_at_utc FROM source_coverage "
            "WHERE source = 'edinet_documents' AND coverage_key = ?",
            (DAY.isoformat(),),
        ).fetchone() == (len(records), "failed", failed_stamp.isoformat(timespec="microseconds"))
    failed_state = projection_state(target)
    assert import_handoff(packet, target).report["raw_rows_added"] == 0
    assert projection_state(target) == failed_state
    assert extract_saved_edinet(target, asof=DAY, lookback_days=0)["edinet-list"][
        "failed_days"
    ] == [DAY.isoformat()]

    recovered_stamp = STAMP + timedelta(hours=2)
    with closing(open_connection(source)) as conn:
        empty_receipt = retain_list_response(
            conn,
            doc_date=DAY,
            received_at=recovered_stamp,
            body=b'{"metadata":{"resultset":{"count":0}},"results":[]}',
        )
    recovered_packet = tmp_path / "recovered.sqlite"
    export_handoff(source, recovered_packet, since=STAMP, archives=set())
    assert import_handoff(recovered_packet, target).report["failed_days"] == []
    assert read_edinet_documents(target, DAY) == []
    with closing(open_connection(target)) as conn:
        assert range_covered(conn, "edinet_documents", DAY, DAY)
        assert conn.execute(
            "SELECT selected_receipt_id, latest_received_at_utc FROM edinet_document_lists "
            "WHERE doc_date = ?",
            (DAY.isoformat(),),
        ).fetchone() == (empty_receipt, recovered_stamp.isoformat(timespec="microseconds"))
    recovered_state = projection_state(target)
    assert import_handoff(recovered_packet, target).report["raw_rows_added"] == 0
    assert projection_state(target) == recovered_state
    assert "edinet-list" not in extract_saved_edinet(target, asof=DAY, lookback_days=0)


def test_bad_old_list_does_not_suppress_a_good_withdrawal_that_invalidates_saved_metrics(tmp_path):
    target, source, packet = (
        tmp_path / name for name in ("target.sqlite", "source.sqlite", "packet.sqlite")
    )
    documents = _seed(target)
    old_stamp = datetime(2026, 4, 24, 9, tzinfo=UTC)
    with closing(open_connection(target)) as conn:
        retain_list_response(
            conn,
            doc_date=BASE,
            received_at=old_stamp,
            body=json.dumps(
                {"metadata": {"resultset": {"count": 2}}, "results": documents}
            ).encode(),
        )
    rebuild_edinet_document_day(target, BASE)
    assert set(EDINETReader(target).load_metric_records(TARGET)) == {"7203", "8306"}
    withdrawal = {**documents[0], "withdrawalStatus": "1", "opeDateTime": "2026-04-27 12:00"}
    with closing(open_connection(source)) as conn:
        retain_list_response(
            conn, doc_date=BASE, received_at=old_stamp + timedelta(days=3), body=b"{invalid JSON"
        )
        retain_list_response(
            conn,
            doc_date=TARGET,
            received_at=old_stamp + timedelta(days=3, hours=1),
            body=json.dumps(
                {"metadata": {"resultset": {"count": 1}}, "results": [withdrawal]}
            ).encode(),
        )
    export_handoff(source, packet, since=old_stamp, archives=set())
    outcome = import_handoff(packet, target)
    assert outcome.complete is False
    assert outcome.report["failed_days"] == [BASE.isoformat()]
    assert TARGET.isoformat() in outcome.report["replayed_days"]
    assert read_edinet_documents(target, TARGET)[0]["withdrawalStatus"] == "1"
    eligible = EDINETReader(target).load_metric_records(TARGET)
    assert set(eligible) == {"8306"}
    assert eligible["8306"].snapshot_asof == BASE
    assert eligible["8306"].snapshot_recorded_at_utc == RECORDED
