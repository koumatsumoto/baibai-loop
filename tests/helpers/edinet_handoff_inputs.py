"""EDINET原受領handoffの合成入力と投影状態の読取。"""

import json
from contextlib import closing
from datetime import UTC, date, datetime

from baibai_engine.data_collection.providers.edinet.receipts import retain_list_response
from baibai_engine.storage.sqlite.source_cache.schema import open_connection

DAY = date(2026, 10, 15)
STAMP = datetime(2026, 10, 15, 7, tzinfo=UTC)


def receive(path, doc_id, stamp, *, day=DAY):
    payload = {
        "metadata": {"resultset": {"count": 1}},
        "results": [{"seqNumber": 1, "docID": doc_id, "secCode": "72030"}],
    }
    with closing(open_connection(path)) as conn:
        return retain_list_response(
            conn, doc_date=day, received_at=stamp, body=json.dumps(payload).encode()
        )


def projection_state(path):
    with closing(open_connection(path)) as conn:
        return (
            conn.execute(
                "SELECT * FROM edinet_documents ORDER BY doc_date, sequence_number"
            ).fetchall(),
            conn.execute("SELECT * FROM edinet_document_lists ORDER BY doc_date").fetchall(),
            conn.execute(
                "SELECT * FROM source_coverage WHERE source = 'edinet_documents' "
                "ORDER BY coverage_key"
            ).fetchall(),
        )
