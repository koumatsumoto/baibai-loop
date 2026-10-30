import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_jobs.acquisition import articles
from baibai_jobs.acquisition.article_planning import load_planning, publish_planning_input
from baibai_jobs.acquisition.receipts import download_fixed

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


def test_second_slot_304_carries_unintegrated_200_closure(tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, _tz):
            return SLOT + timedelta(hours=5)

    monkeypatch.setattr(articles, "datetime", Clock)
    store = MemoryR2Store()
    publish_planning_input(store, "boj_news", {"lake_release_id": "before"})
    session = Session([Response(200, b"<rss><channel /></rss>"), Response(304, b"")])
    for index in range(2):
        report = articles.acquire_source(
            store,
            source="boj_news",
            slot=SLOT + timedelta(hours=index * 4),
            identity=f"run-{index}",
            directory=tmp_path / f"slot-{index}",
            session=session,
        )
        assert report["status"] == "completed"
    assert "If-None-Match" not in session.headers[0]
    assert session.headers[1]["If-None-Match"] == '"feed-v1"'
    planning, _ = load_planning(store, "boj_news")
    assert len(planning.pending) == 2
    downloaded = tmp_path / "downloaded"
    download_fixed(store, planning.pending[-1], downloaded)
    with closing(connect_read_only(downloaded / "receipts.sqlite")) as conn:
        values = [
            json.loads(row[0])
            for row in conn.execute("SELECT receipt_json FROM article_source_receipts")
        ]
    assert sorted(item["status"] for item in values) == ["not_modified", "ok"]
    original = next(item for item in values if item["status"] == "ok")
    unchanged = next(item for item in values if item["status"] == "not_modified")
    assert unchanged["body_ref"] == original["body_ref"]
    assert (
        planning.requests[original["requested_url"]]["stage"]["stage_id"]
        == planning.pending[0]["stage_id"]
    )


def test_failure_in_one_source_does_not_stop_another_private_stage(tmp_path, monkeypatch, capsys):
    calls = []

    def acquire(_store, *, source, **_):
        calls.append(source)
        if source == "tsr_data":
            raise ValueError("synthetic private failure detail")
        return {"source": source, "status": "completed"}

    monkeypatch.setattr(articles, "acquire_source", acquire)
    monkeypatch.setattr(articles, "Boto3R2Store", lambda **_: MemoryR2Store())
    assert (
        articles.main(
            [
                "--group",
                "public",
                "--created-at",
                SLOT.isoformat(),
                "--identity",
                "test",
                "--directory",
                str(tmp_path),
            ]
        )
        == 1
    )
    assert calls == list(articles.GROUPS["public"])
    assert "private failure detail" not in capsys.readouterr().out


def test_nhk_four_feeds_keep_each_summary_and_fetch_shared_guid_once(tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, _tz):
            return SLOT + timedelta(minutes=1)

    monkeypatch.setattr(articles, "datetime", Clock)
    monkeypatch.setattr(
        "baibai_engine.data_collection.transport.articles.wait_for_read_retry", lambda _: None
    )
    store = MemoryR2Store()
    publish_planning_input(store, "nhk_news", {"lake_release_id": "before"})
    feeds = [
        Response(
            200,
            f"<rss><channel><item><guid>synthetic-same-guid</guid><link>https://news.web.nhk/news/example</link><title>Synthetic title</title><description>synthetic summary {index}</description></item></channel></rss>".encode(),
        )
        for index in range(4)
    ]
    session = Session(
        [*feeds, Response(200, b"<html><body>Unsupported synthetic shell</body></html>")]
    )
    report = articles.acquire_source(
        store,
        source="nhk_news",
        slot=SLOT,
        identity="test",
        directory=tmp_path / "nhk",
        session=session,
    )
    assert report["pages"] == 4
    assert report["bodies"] == 1
    assert len(set(session.urls)) == 5
    with closing(connect_read_only(tmp_path / "nhk/receipts.sqlite")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM article_source_receipts").fetchone()[0] == 5
    planning, _ = load_planning(store, "nhk_news")
    assert len(planning.pending) == 1
