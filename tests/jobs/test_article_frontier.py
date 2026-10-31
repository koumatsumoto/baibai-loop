"""Finite scheduling examples, not evidence of production coverage or time to drain."""

from collections import OrderedDict

import pytest

from baibai_engine.data_collection.providers.articles.contracts import NativeArticle
from baibai_jobs.acquisition.article_frontier import ArticleFrontier


def entries(count, *, prefix="old", title="Synthetic", revision="one"):
    return tuple(
        NativeArticle(
            article_id=f"guid:{prefix}:{number}",
            locator=f"/{number}",
            url=f"https://synthetic.invalid/{prefix}/{number}",
            title=title,
            text="",
            content_scope="headline",
            metadata={"source_item_sha256": f"{prefix}:{number}:{revision}"},
        )
        for number in range(count)
    )


class Scenario:
    def __init__(self, cursor=None):
        self.cursor = cursor or {}
        self.raw = {}
        self.cache = OrderedDict()
        self.succeeded = set()
        self.sequence = 0

    def resolve(self, reference):
        return self.raw[reference["index_receipt_id"]]

    def step(self, items, *, failure=None):
        frontier = ArticleFrontier(self.cursor, self.resolve)
        self.sequence += 1
        identity = str(self.sequence)
        reference = {"stage": {"stage_id": identity}, "index_receipt_id": identity}
        self.raw[identity] = items
        frontier.observe("https://synthetic.invalid/index", reference, items)
        known = [item.url for item in items if item.url in self.cache][:2]
        selected = frontier.queue(known)[:8]
        for url in selected:
            assert frontier.evidence(url) is not None
            ok = url != failure
            frontier.complete(url, success=ok)
            if ok:
                self.succeeded.add(url)
                self.cache.pop(url, None)
                self.cache[url] = True
        self.cache = OrderedDict(list(self.cache.items())[-128:])
        self.cursor = frontier.wire()
        return frontier, selected


def test_static_index_larger_than_both_old_bounds_finishes_with_revalidation():
    scenario = Scenario()
    raw = entries(800)
    for _ in range(133):
        scenario.step(raw)
    assert len(scenario.succeeded) == 800
    assert scenario.cursor["pending"] == []
    assert len(scenario.cache) == 128
    _, selected = scenario.step(raw)
    assert len(selected) == 2  # Only the finite revalidation lane remains.


def test_new_arrivals_and_fifo_both_progress_with_the_same_body_budget():
    scenario = Scenario()
    old = entries(800)
    scenario.step(old)
    for number in range(1, 200):
        new = entries(1, prefix=f"new{number}")
        _, selected = scenario.step((*new, *old))
        assert new[0].url in selected
        assert len(selected) <= 8
    assert {item.url for item in old} <= scenario.succeeded
    assert len(scenario.succeeded) == 999


def test_failed_work_rotates_and_survives_when_it_leaves_the_feed():
    scenario = Scenario()
    raw = entries(20)
    bad = raw[0].url
    scenario.step(raw, failure=bad)
    for _ in range(5):
        scenario.step((), failure=bad)
    assert len(scenario.succeeded) == 19
    assert [url for url, _ in scenario.cursor["pending"]] == [bad]
    scenario.step(())
    assert len(scenario.succeeded) == 20


def test_reordered_indexes_are_not_new_but_native_revision_is():
    scenario = Scenario()
    raw = entries(3)
    scenario.step(raw)
    reordered = tuple(item.model_copy(update={"locator": f"/{i}"})
                      for i, item in enumerate(reversed(raw)))
    frontier, _ = scenario.step(reordered)
    assert frontier.new == []
    changed = entries(3, revision="two")
    frontier, selected = scenario.step(changed)
    assert len(frontier.new) == 3
    assert set(selected) == {item.url for item in changed}


def test_long_titles_stay_in_the_original_not_the_workset():
    scenario = Scenario()
    frontier, _ = scenario.step(entries(800, title="あ" * 1500))
    assert len(frontier.pending) == 792
    assert frontier.encoded_size() < 100_000
    assert "あ" not in str(frontier.wire())


def test_cross_feed_guid_conflict_is_not_bound_to_arbitrary_first_item():
    raw = entries(1)
    second = (raw[0].model_copy(update={"article_id": "different-guid"}),)
    snapshots = {"first": raw, "second": second}
    frontier = ArticleFrontier({}, lambda ref: snapshots[ref["index_receipt_id"]])
    for name in snapshots:
        frontier.observe(name, {"stage": {"stage_id": name}, "index_receipt_id": name},
                         snapshots[name])
    assert frontier.queue([]) == [raw[0].url]
    assert frontier.evidence(raw[0].url) is None
    frontier.complete(raw[0].url, success=False)
    assert len(frontier.wire()["pending"]) == 1


def test_capacity_keeps_exact_nonadmitted_indexes_and_stops_next_http(monkeypatch):
    monkeypatch.setattr("baibai_jobs.acquisition.article_frontier.MAX_FRONTIER_BYTES", 200)
    scenario = Scenario()
    frontier, selected = scenario.step(entries(20))
    assert selected == []
    assert frontier.capacity_blocked
    assert len(frontier.wire()["indexes"]) == 1
    with pytest.raises(ValueError, match="capacity requires exact recovery"):
        ArticleFrontier(frontier.wire(), scenario.resolve)


def test_legacy_omission_does_not_disappear_when_current_work_finishes():
    scenario = Scenario({"pending_urls": [], "omitted_url_count": 190})
    frontier, _ = scenario.step(entries(1))
    assert frontier.pending == {}
    assert frontier.wire()["legacy_omitted"] == 190
