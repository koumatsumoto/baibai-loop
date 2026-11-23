"""Daily plan discovers immutable article originals independently of planning."""

import json
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime

from tests.helpers.article_acquisition import SLOT, Response, Session, _packet
from tests.helpers.edinet_native import empty_edinet_input
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.receipts.articles.store import read_receipt
from baibai_engine.machine_analysis.observations.articles.store import read_observations
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition import articles
from baibai_jobs.acquisition.article_planning import (
    commit_stage,
    load_planning,
    publish_planning_input,
    slot_at,
    source_key,
)
from baibai_jobs.acquisition.receipts import seal_integrated
from baibai_jobs.integration import sources
from baibai_jobs.maintenance.article_recovery import prepare
from baibai_jobs.schedule import integration_target_date


def test_daily_plan_fixes_article_handoff_without_acquisition_summary(tmp_path, monkeypatch):
    store = MemoryR2Store()
    publish_planning_input(store, "boj_news", {})
    state, etag = load_planning(store, "boj_news")
    entry = commit_stage(
        store,
        state=state,
        expected_etag=etag,
        slot=SLOT,
        stage_id="boj_news-unacknowledged",
        directory=_packet(tmp_path),
        requests={},
        cursor={},
        not_before=None,
    )
    del store.values[source_key("boj_news")]
    monkeypatch.setattr(store, "close", lambda: None, raising=False)
    monkeypatch.setattr(sources, "Boto3R2Store", lambda **_: store)
    empty_edinet_input(store, tmp_path)
    store.get_keys.clear()
    directory = tmp_path / "integration"
    assert sources.main(["plan", "--directory", str(directory)]) == 0
    plan = json.loads((directory / "plan.json").read_text())
    (frozen,) = plan["handoffs"]
    assert all(frozen[key] == value for key, value in entry.items())
    assert source_key("boj_news") not in store.get_keys
    assert not any(key.endswith(".sqlite") for key in store.get_keys)


def test_current_rss_day_and_exact_304_recovery_keep_archive_out_of_daily(tmp_path, monkeypatch):
    source = "boj_news"
    policy = replace(articles.SOURCES[source], max_bodies=0)
    monkeypatch.setitem(articles.SOURCES, source, policy)
    native_url = "https://www.boj.or.jp/current.htm"
    feed = (
        '<rss xmlns:ext="urn:synthetic:extension"><channel>'
        "<ext:item><ext:link>https://www.boj.or.jp/not-an-article.htm</ext:link></ext:item>"
        f"<item><link>{native_url}</link><title>Current</title></item></channel></rss>"
    ).encode()
    store = MemoryR2Store()
    empty_edinet_input(store, tmp_path)
    publish_planning_input(store, source, {})
    session = Session([Response(200, feed), Response(200, feed), Response(304, b"")])

    class Clock(datetime):
        instant = datetime(2026, 11, 22, 13, 19, tzinfo=UTC)

        @classmethod
        def now(cls, _tz):
            return cls.instant

    monkeypatch.setattr(articles, "datetime", Clock)
    monkeypatch.setattr(store, "close", lambda: None, raising=False)
    monkeypatch.setattr(sources, "Boto3R2Store", lambda **_: store)
    entries = []
    for tag, instant, native in [
        ("archive", datetime(2026, 11, 22, 13, 19, tzinfo=UTC), False),
        ("fresh", datetime(2026, 11, 23, 9, 19, tzinfo=UTC), True),
        ("revalidate", datetime(2026, 11, 23, 13, 19, tzinfo=UTC), True),
    ]:
        Clock.instant = instant
        with monkeypatch.context() as archived:
            if not native:
                archived.setattr(articles, "RSS_SOURCES", articles.RSS_SOURCES - {source})
            report = articles.acquire_source(
                store,
                source=source,
                slot=slot_at(instant),
                identity=tag,
                directory=tmp_path / tag,
                session=session,
            )
        assert report["status"] == "completed"
        entry = load_planning(store, source)[0].last_stage
        assert entry is not None
        assert entry["stage_id"] == report["stage_id"]
        entries.append(entry)
        if not native:
            seal_integrated(
                store, entry, generations={"lake_release_id": "archive"}, recovery=False
            )
    archive, fresh, revalidation = entries
    asof = integration_target_date(datetime(2026, 11, 23, 13, 51, tzinfo=UTC), scheduled=True)
    assert asof.isoformat() == fresh["asof"] == revalidation["asof"] == "2026-11-23"
    assert archive["asof"] == "2026-11-22"
    assert "If-None-Match" not in session.headers[1]
    assert session.headers[2]["If-None-Match"] == '"feed-v1"'

    directory, market = tmp_path / "daily", tmp_path / "market.sqlite"
    with closing(open_connection(market)):
        pass
    store.get_keys.clear()
    assert sources.main(["plan", "--directory", str(directory)]) == 0
    plan = json.loads((directory / "plan.json").read_text())
    assert [item["stage_id"] for item in plan["handoffs"]] == [
        fresh["stage_id"],
        revalidation["stage_id"],
    ]
    applied = sources.apply(store, directory, market=market, asof=asof)
    assert applied["asof"] == "2026-11-23"
    first, latest = applied["handoffs"]
    assert all(item["applied"] and not item["failed"] for item in (first, latest))
    assert not any(
        key.startswith(f"coordination/article-sources/{archive['stage_id']}/receipts-")
        for key in store.get_keys
    )

    recovery, recovered = tmp_path / "recover", tmp_path / "recovered.sqlite"
    # Recovery on the current canonical copy can reuse the same semantic artifact.
    with closing(open_connection(market)) as published, closing(open_connection(recovered)) as copy:
        published.backup(copy)
    prepare(
        store,
        recovery,
        source=source,
        stage_id=revalidation["stage_id"],
        manifest_sha256=revalidation["manifest_sha256"],
    )
    (again,) = sources.apply(store, recovery, market=recovered, asof=asof)["handoffs"]
    assert again["entry"]["explicit_article_recovery"] is True
    assert again["applied"]
    assert not again["failed"]
    assert again["article_delta"] == latest["article_delta"]
    with closing(open_connection(recovered)) as conn:
        (headline,) = read_observations(conn, again["result"]["observation_ids"])
        assert (headline.native_locator, headline.original_url) == ("/item/1", native_url)
        receipt = read_receipt(conn, headline.receipt_id)
        assert receipt.http_status == 304
        assert receipt.rss_index_revision is None
        assert receipt.body_ref is not None
        (original_id,) = first["result"]["receipt_ids"]
        assert read_receipt(conn, original_id).rss_index_revision == "rss-native-v1"
        assert receipt.body_ref.receipt_id == original_id
