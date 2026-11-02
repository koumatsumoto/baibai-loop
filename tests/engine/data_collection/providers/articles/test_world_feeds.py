"""Synthetic world feeds: native publication is not a policy or full-text claim."""

import pytest

from baibai_engine.data_collection.providers.articles.contracts import SOURCES, WORLD_NEWS_SOURCES
from baibai_engine.data_collection.providers.articles.native import is_index_locator, parse_index
from baibai_engine.machine_analysis.observations.articles.projection import native_time

FEED = b'''<rss><channel><item><guid>same-native-id</guid>
<link>https://example.invalid/speech</link><title>Speech</title>
<description>&lt;p&gt;Quoted summary&lt;/p&gt;</description>
<pubDate>Mon, 02 Nov 2026 00:30:00 GMT</pubDate>
<category>Speech</category><category>Policy</category></item></channel></rss>'''


@pytest.mark.parametrize("source", sorted(WORLD_NEWS_SOURCES))
def test_world_feed_keeps_native_identity_clock_labels_and_scope(source):
    (item,) = parse_index(source, FEED, url=SOURCES[source].entry_url)
    assert item.article_id == f"{source}:same-native-id"
    assert item.content_scope == "summary"
    assert item.text == "Quoted summary"
    assert item.metadata["categories"] == ["Speech", "Policy"]
    assert item.metadata["feed_url"] == SOURCES[source].entry_url
    assert native_time(item.published_raw, source=source).value == "2026-11-02T00:30:00+00:00"
    assert is_index_locator(source, item.locator)
    assert SOURCES[source].max_bodies == 0
    assert SOURCES[source].response_limit("index") == 1024 * 1024


def test_bbc_overlap_preserves_two_feed_provenances_without_two_article_identities():
    world, business = SOURCES["bbc_news"].entry_urls
    first = parse_index("bbc_news", FEED, url=world)[0]
    second = parse_index("bbc_news", FEED, url=business)[0]
    assert first.article_id == second.article_id
    assert first.metadata["source_item_sha256"] == second.metadata["source_item_sha256"]
    assert first.metadata["feed_url"] != second.metadata["feed_url"]
    corrected = parse_index(
        "bbc_news", FEED.replace(b"Speech</category>", b"Interview</category>"), url=world
    )[0]
    assert corrected.article_id == first.article_id
    assert corrected.metadata["source_item_sha256"] != first.metadata["source_item_sha256"]


@pytest.mark.parametrize("source", sorted(WORLD_NEWS_SOURCES))
def test_empty_feed_is_distinct_from_invalid_feed(source):
    assert parse_index(source, b"<rss><channel/></rss>", url=SOURCES[source].entry_url) == ()
    for body in (b"<rss/>", b"<html/>", b"broken", b"<!DOCTYPE rss><rss><channel/></rss>"):
        with pytest.raises(ValueError):
            parse_index(source, body, url=SOURCES[source].entry_url)
