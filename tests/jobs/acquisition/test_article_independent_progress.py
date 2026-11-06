"""Acquisition progresses without L2, using only retained immutable handoffs."""

import json
from contextlib import closing
from datetime import datetime, timedelta

from tests.helpers.article_acquisition import SLOT, Response, Session
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.receipts.articles.store import read_body, read_receipt
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_jobs.acquisition import articles
from baibai_jobs.acquisition.article_planning import (
    load_planning,
    publish_planning_input,
    source_key,
)
from baibai_jobs.acquisition.receipts import available_handoffs, download_fixed, seal_integrated
from baibai_jobs.integration.article_handoff import import_handoff


def test_sixty_four_unsealed_cycles_keep_raw_then_catch_up_and_rebuild(tmp_path, monkeypatch):
    class Clock(datetime):
        current = SLOT

        @classmethod
        def now(cls, _tz):
            return cls.current

    monkeypatch.setattr(articles, "datetime", Clock)
    monkeypatch.setattr("baibai_engine.data_collection.transport.articles.datetime", Clock)
    monkeypatch.setattr(
        "baibai_engine.data_collection.transport.articles.wait_for_read_retry", lambda _: None
    )
    store = MemoryR2Store()
    publish_planning_input(store, "boj_news", {})
    originals = {}
    expected = {}
    lengths = []
    same_clock = []
    feed_bodies = []
    for index in range(64):
        slot = SLOT + timedelta(hours=index * 4)
        # Two different 200s with the same receipt clock are both kept.
        Clock.current = slot + timedelta(hours=5 if index == 32 else 1)
        title = f"Synthetic version {index}"
        feed = (
            "<rss><channel><item><link>https://www.boj.or.jp/synthetic.htm</link>"
            f"<title>{title}</title></item></channel></rss>"
        ).encode()
        status = 200 if index in {0, 32, 33} else 403 if index == 20 else 304
        response = Response(status, feed if status == 200 else b"")
        response.headers["ETag"] = f'"feed-{min(index, 33)}"'
        session = Session([response, Response(200, b"<main>Synthetic original body</main>")])
        packet = tmp_path / f"slot-{index}"
        report = articles.acquire_source(
            store,
            source="boj_news",
            slot=slot,
            identity=f"run-{index}",
            directory=packet,
            session=session,
        )
        assert report["status"] == ("partial" if status == 403 else "completed")
        assert session.urls[0] == articles.SOURCES["boj_news"].entry_url
        with closing(connect_read_only(packet / "receipts.sqlite")) as conn:
            for (encoded,) in conn.execute("SELECT receipt_json FROM article_source_receipts"):
                value = json.loads(encoded)
                receipt = read_receipt(conn, value["receipt_id"])
                body = read_body(conn, receipt.body_ref) if receipt.body_ref else None
                first_seen = receipt.receipt_id not in expected
                if receipt.receipt_id in expected:
                    assert expected[receipt.receipt_id] == (receipt, body)
                expected[receipt.receipt_id] = (receipt, body)
                if (
                    receipt.requested_url == articles.SOURCES["boj_news"].entry_url
                    and first_seen
                    and status == 200
                    and receipt.received_at == Clock.current
                ):
                    feed_bodies.append(body)
                    if index in {32, 33}:
                        same_clock.append(receipt)
        state, _ = load_planning(store, "boj_news")
        assert state.last_slot == slot.isoformat()
        assert state.last_stage["stage_id"] == report["stage_id"]
        lengths.append(len(state.encoded()))
        for key, value in store.values.items():
            if key.startswith("coordination/article-sources/"):
                if key in originals:
                    assert originals[key] == value.body
                originals[key] = value.body

    # No L2 operation or acknowledgement occurred during these 64 slots.
    assert not any(key.endswith("integrated.json") for key in store.values)
    assert max(lengths) < 8_000
    assert len(same_clock) == 2
    assert same_clock[0].received_at == same_clock[1].received_at
    assert same_clock[0].body_ref != same_clock[1].body_ref
    assert len(set(feed_bodies)) == 3
    assert {receipt.status for receipt, _ in expected.values()} == {
        "ok",
        "not_modified",
        "http_error",
    }
    store.get_keys.clear()
    ready = available_handoffs(store, market=True, namespace="article-sources")
    assert len(ready) == 64
    assert not any(key.endswith(".sqlite") for key in store.get_keys)
    planning = store.values[source_key("boj_news")]

    database = tmp_path / "canonical.sqlite"
    for index, entry in enumerate(ready):
        packet = tmp_path / f"catch-up-{index}"
        download_fixed(store, entry, packet)
        result = import_handoff("boj_news", database, packet)
        assert result.complete
        seal_integrated(store, entry, generations={}, recovery=False)
    assert available_handoffs(store, market=True, namespace="article-sources") == []
    assert store.values[source_key("boj_news")] == planning
    assert all(store.values[key].body == value for key, value in originals.items())
    with closing(connect_read_only(database)) as conn:
        observations = conn.execute(
            "SELECT * FROM article_observations ORDER BY observation_id"
        ).fetchall()
        artifacts = conn.execute(
            "SELECT artifact_id FROM article_extraction_artifacts ORDER BY artifact_id"
        ).fetchall()
        semantics = sorted(
            json.dumps(
                {key: value for key, value in json.loads(row[0]).items() if key != "generated_at"},
                sort_keys=True,
            )
            for row in conn.execute("SELECT artifact_json FROM article_extraction_artifacts")
        )
        assert observations
        assert artifacts

    # The processing marker changes neither retention nor cold readability.
    rebuilt = tmp_path / "rebuilt.sqlite"
    for index, entry in enumerate(
        available_handoffs(store, market=True, namespace="article-sources", include_integrated=True)
    ):
        packet = tmp_path / f"rebuild-{index}"
        download_fixed(store, entry, packet)
        assert import_handoff("boj_news", rebuilt, packet).complete
        assert import_handoff("boj_news", database, packet).complete
    for path in (database, rebuilt):
        with closing(connect_read_only(path)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM article_source_receipts").fetchone()[
                0
            ] == len(expected)
            for identity, (receipt, body) in expected.items():
                assert read_receipt(conn, identity) == receipt
                if receipt.body_ref:
                    assert read_body(conn, receipt.body_ref) == body
            assert (
                conn.execute(
                    "SELECT * FROM article_observations ORDER BY observation_id"
                ).fetchall()
                == observations
            )
            if path == database:
                assert (
                    conn.execute(
                        "SELECT artifact_id FROM article_extraction_artifacts ORDER BY artifact_id"
                    ).fetchall()
                    == artifacts
                )
            # A fresh extraction has its own generation clock/ID; the native
            # observations and extracted claims are still reproduced exactly.
            assert (
                sorted(
                    json.dumps(
                        {
                            key: value
                            for key, value in json.loads(row[0]).items()
                            if key != "generated_at"
                        },
                        sort_keys=True,
                    )
                    for row in conn.execute(
                        "SELECT artifact_json FROM article_extraction_artifacts"
                    )
                )
                == semantics
            )
    assert store.values[source_key("boj_news")] == planning
