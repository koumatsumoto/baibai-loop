import json
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

from baibai_engine.data_collection.providers.edinet.archives import retain_source_archive
from baibai_engine.data_collection.providers.edinet.receipts import retain_list_response
from baibai_engine.data_collection.providers.edinet.archives import archive_keys
from baibai_jobs.integration.edinet_handoff import export_handoff
from baibai_jobs.integration.edinet_handoff import import_handoff
from baibai_engine.machine_analysis.observations.disclosures.edinet_list_rebuild import rebuild_edinet_document_day
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.store import read_edinet_documents
from baibai_engine.storage.observations.schema import open_connection

DAY = date(2026, 10, 15)
STAMP = datetime(2026, 10, 15, 7, tzinfo=UTC)


def receive(path, doc_id, stamp):
    payload = {
        "metadata": {"resultset": {"count": 1}},
        "results": [{"seqNumber": 1, "docID": doc_id, "secCode": "72030"}],
    }
    with closing(open_connection(path)) as conn:
        retain_list_response(
            conn, doc_date=DAY, received_at=stamp, body=json.dumps(payload).encode()
        )


def test_late_edinet_list_uses_all_retained_receipts_without_custom_lww(tmp_path):
    source, target, stage = (
        tmp_path / name for name in ("source.sqlite", "target.sqlite", "stage.sqlite")
    )
    receive(source, "S100OLD", STAMP)
    receive(target, "S100NEW", STAMP + timedelta(hours=1))
    rebuild_edinet_document_day(target, DAY)
    export_handoff(source, stage, since=STAMP, archives=set())
    result = import_handoff(stage, target)
    assert result["failed_days"] == []
    assert [item["docID"] for item in read_edinet_documents(target, DAY)] == ["S100NEW"]
    with closing(open_connection(target)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM edinet_list_responses").fetchone()[0] == 2
    with closing(open_connection(stage)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM edinet_document_lists").fetchone()[0] == 0
    assert import_handoff(stage, target)["raw_rows_added"] == 0


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
    export_handoff(source, stage, since=STAMP, archives=archive_keys(source))
    result = import_handoff(stage, target)
    assert result["archive_conflicts"] == [["S100SAME", 5]]
    with closing(open_connection(target)) as conn:
        assert conn.execute(
            "SELECT doc_id, zip_content FROM edinet_source_archives ORDER BY doc_id"
        ).fetchall() == [
            ("S100NEW", b"new-xbrl"),
            ("S100OTHER", b"unrelated"),
            ("S100SAME", b"canonical"),
        ]
