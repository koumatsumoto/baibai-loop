"""旧SQL receipt reader用の合成入力。通常取得はnative L1へ保存する。"""

from baibai_engine.data_collection.providers.edinet.list_native import NativeListReceipt
from baibai_engine.data_collection.providers.edinet.models import EdinetResponse


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
