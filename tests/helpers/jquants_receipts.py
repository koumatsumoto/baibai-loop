"""合成J-Quants原本を保持するfixture。test moduleを共有fixtureの入口にしない。"""

import json
from datetime import UTC, datetime, timedelta

import requests

from baibai_engine.data_collection.providers.jquants.acquisition import acquire_request
from baibai_engine.data_collection.providers.jquants.http import JQuantsHTTP
from baibai_engine.storage.sqlite.source_cache.schema import open_connection

DAY = "2026-10-16"
FIRST_AT = datetime(2026, 10, 16, 16, 30, tzinfo=UTC)


class Session(requests.Session):
    def __init__(self, *replies):
        super().__init__()
        self.replies = iter(replies)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        response = requests.Response()
        response.status_code = 200
        response._content = reply
        response._content_consumed = True
        return response


def body(records, *, cursor=None):
    payload = {"data": records}
    if cursor is not None:
        payload["pagination_key"] = cursor
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def receive(db_path, source, query, raw_body, received_at):
    session = Session(raw_body)
    client = JQuantsHTTP("synthetic-test-key", session=session, clock=lambda: received_at)
    conn = open_connection(db_path)
    try:
        return acquire_request(conn, client, source, query)
    finally:
        conn.close()


def source_record(source, on_date):
    if source == "financial_summaries":
        return {"Code": "13010", "DiscDate": on_date, "DiscNo": on_date, "CurPerType": "FY"}
    if source == "short_sale_reports":
        return {
            "Code": "13010",
            "DiscDate": on_date,
            "CalcDate": on_date,
            "SSName": "Reporter A",
            "ShrtPosToSO": 0.006,
        }
    if source == "margin_interest":
        return {
            "Code": "13010",
            "Date": on_date,
            "LongVol": 5000,
            "ShrtVol": 1000,
            "LongStdVol": 3000,
            "LongNegVol": 2000,
            "ShrtStdVol": 800,
            "ShrtNegVol": 200,
            "IssType": "2",
        }
    if source == "margin_alerts":
        return {
            "Code": "13010",
            "PubDate": on_date,
            "AppDate": on_date,
            "PubReason": "designation",
            "ShrtOut": 1000,
            "ShrtOutChg": 0,
            "ShrtOutRatio": 0.1,
            "LongOut": 2000,
            "LongOutChg": 0,
            "LongOutRatio": 0.2,
            "SLRatio": 0.5,
            "ShrtNegOut": 100,
            "ShrtNegOutChg": 0,
            "ShrtStdOut": 900,
            "ShrtStdOutChg": 0,
            "LongNegOut": 200,
            "LongNegOutChg": 0,
            "LongStdOut": 1800,
            "LongStdOutChg": 0,
            "TSEMrgnRegCls": "none",
        }
    raise AssertionError(f"unexpected source: {source}")


def calendar(path, first, last, division, received):
    from datetime import date

    start, end = date.fromisoformat(first), date.fromisoformat(last)
    rows = [
        {"Date": (start + timedelta(days=i)).isoformat(), "HolDiv": str(division)}
        for i in range((end - start).days + 1)
    ]
    return receive(path, "market_calendar", {"from": first, "to": last}, body(rows), received)
