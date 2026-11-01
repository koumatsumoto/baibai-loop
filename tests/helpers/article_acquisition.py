"""Shared article transport doubles and exact handoff builders, never collected tests."""

import hashlib
import json
from datetime import UTC, datetime

from baibai_engine.data_collection.receipts.articles.contracts import ArticleReceipt, BodyRef

SLOT = datetime(2026, 10, 29, 13, 13, tzinfo=UTC)


class Response:
    def __init__(self, status, body):
        self.status_code = status
        self.body = body
        self.headers = {"ETag": '"feed-v1"', "Content-Type": "application/xml"}
        self.encoding = "utf-8"

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def iter_content(self, **_):
        yield self.body


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.headers = []
        self.urls = []

    def get(self, _url, **kwargs):
        assert kwargs["allow_redirects"] is False
        self.headers.append(kwargs["headers"])
        self.urls.append(_url)
        return next(self.responses)


def _packet(tmp_path, source="boj_news"):
    from baibai_engine.storage.sqlite.source_cache.schema import open_connection

    directory = tmp_path / source
    directory.mkdir()
    open_connection(directory / "receipts.sqlite").close()
    (directory / "result.json").write_text(
        json.dumps(
            {"source": source, "started_at": SLOT.isoformat(), "asof": SLOT.date().isoformat()}
        )
    )
    return directory


def receipt(identity="original", body=b"synthetic"):
    return ArticleReceipt(
        receipt_id=identity,
        source="mof_news",
        requested_url="https://www.mof.go.jp/insideLink/example.html",
        received_at=datetime(2026, 11, 1, tzinfo=UTC),
        status="ok",
        http_status=200,
        encoding="utf-8",
        body_ref=BodyRef(receipt_id=identity, sha256=hashlib.sha256(body).hexdigest()),
    )
