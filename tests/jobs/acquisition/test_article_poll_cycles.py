import json
from datetime import datetime, timedelta

import pytest
from tests.helpers.article_acquisition import SLOT, Response, Session
from tests.helpers.r2_store import MemoryR2Store

import baibai_engine.data_collection.transport.articles as transport
from baibai_jobs.acquisition import articles
from baibai_jobs.acquisition.article_planning import publish_planning_input, slot_at


@pytest.fixture
def clock(monkeypatch):
    now = [SLOT + timedelta(hours=4, minutes=1)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now[0].astimezone(tz)

    monkeypatch.setattr(articles, "datetime", Clock)
    monkeypatch.setattr(transport, "datetime", Clock)
    monkeypatch.setattr(transport, "wait_for_read_retry", lambda _: None)
    return now


def feed(count):
    return json.dumps(
        [
            {"url": f"/data/detail/{index}.html", "title": f"Synthetic {index}"}
            for index in range(count)
        ]
    ).encode()


def test_late_tsr_poll_then_same_cycle_body_resume_and_complete_skip(tmp_path, clock):
    remote = MemoryR2Store()
    publish_planning_input(remote, "tsr_data", {})
    body = b'<div class="entry">Synthetic body</div>'
    first = Session([Response(200, feed(9)), *(Response(200, body) for _ in range(8))])
    result = articles.acquire_source(
        remote,
        source="tsr_data",
        slot=slot_at(clock[0]),
        identity="late",
        directory=tmp_path / "first",
        session=first,
    )
    assert result["pages"] == 1
    assert result["bodies"] == 8
    assert result["index_reused"] is False
    assert result["pending_url_count"] == 1
    second = Session([Response(200, body)])
    resumed = articles.acquire_source(
        remote,
        source="tsr_data",
        slot=slot_at(clock[0]),
        identity="resume",
        directory=tmp_path / "second",
        session=second,
    )
    assert resumed["pages"] == 0
    assert resumed["bodies"] == 1
    assert resumed["index_reused"] is True
    assert second.urls == ["https://www.tsr-net.co.jp/data/detail/8.html"]
    assert resumed["pending_url_count"] == 0
    puts = list(remote.put_keys)
    third = Session([])
    complete = articles.acquire_source(
        remote,
        source="tsr_data",
        slot=slot_at(clock[0]),
        identity="complete",
        directory=tmp_path / "third",
        session=third,
    )
    assert complete["reason"] == "index_and_queue_complete"
    assert third.urls == []
    assert remote.put_keys == puts

    clock[0] = SLOT + timedelta(days=1, minutes=1)
    next_cycle = Session([Response(304, b""), Response(304, b""), Response(304, b"")])
    new = articles.acquire_source(
        remote,
        source="tsr_data",
        slot=slot_at(clock[0]),
        identity="next-cycle",
        directory=tmp_path / "next",
        session=next_cycle,
    )
    assert new["pages"] == 1
    assert new["index_reused"] is False
    assert next_cycle.urls[0] == articles.SOURCES["tsr_data"].entry_url


def test_failed_tsr_poll_does_not_become_completed_by_last_slot(tmp_path, clock):
    remote = MemoryR2Store()
    publish_planning_input(remote, "tsr_data", {})
    first = Session([Response(403, b"denied")])
    failed = articles.acquire_source(
        remote,
        source="tsr_data",
        slot=slot_at(clock[0]),
        identity="failed",
        directory=tmp_path / "failed",
        session=first,
    )
    assert failed["failures"] == ["http_error"]
    second = Session([Response(200, feed(1)), Response(200, b'<div class="entry">Body</div>')])
    recovered = articles.acquire_source(
        remote,
        source="tsr_data",
        slot=slot_at(clock[0]),
        identity="retry",
        directory=tmp_path / "retry",
        session=second,
    )
    assert recovered["pages"] == 1
    assert recovered["bodies"] == 1
    assert recovered["failures"] == []
