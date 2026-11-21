"""合成のEDINET一覧受領。固定IDのnative例と明示的なSQLite fixtureを作る。"""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from baibai_engine.data_collection.providers.edinet.list_native import NativeListReceipt
from baibai_engine.data_collection.providers.edinet.models import EdinetResponse

DAY = date(2026, 1, 1)
TIME = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)


def receipt(index=1, body=b"raw malformed {", **kwargs):
    return NativeListReceipt.from_response(
        DAY,
        EdinetResponse(TIME + timedelta(microseconds=index), body),
        UUID(int=index).hex,
        **kwargs,
    )


def seed_list_response(conn, *, doc_date, received_at, body):
    receipt = NativeListReceipt.from_response(doc_date, EdinetResponse(received_at, body))
    with conn:
        conn.execute(
            "INSERT INTO edinet_list_responses VALUES (?, ?, ?, ?)",
            (
                receipt.receipt_id,
                receipt.doc_date.isoformat(),
                receipt.received_at.isoformat(timespec="microseconds"),
                receipt.body_gzip,
            ),
        )
    return receipt.receipt_id
