import json
from datetime import UTC, datetime, timedelta

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.storage.objects.r2 import LakeCASConflict, LakePublishError
from baibai_jobs.acquisition.article_planning import (
    MAX_PLANNING_BYTES,
    ArticlePlanning,
    acknowledge_stage,
    commit_stage,
    is_due,
    load_planning,
    pending_handoffs,
    publish_planning_input,
    slot_at,
    source_key,
)
from baibai_jobs.maintenance.article_recovery import exact_entry

SLOT = datetime(2026, 10, 29, 13, 13, tzinfo=UTC)


def test_six_slots_and_daily_tsr_do_not_duplicate_before_daily():
    news = ArticlePlanning("boj_news", {})
    tsr = ArticlePlanning("tsr_data", {})
    slots = [SLOT.replace(hour=hour) for hour in (1, 5, 9, 13, 17, 21)]
    assert all(is_due(news, slot=slot, now=slot) for slot in slots)
    assert [slot.hour for slot in slots if is_due(tsr, slot=slot, now=slot)] == [13]
    assert slot_at(SLOT - timedelta(seconds=1)) == SLOT - timedelta(hours=4)
    assert slot_at(SLOT + timedelta(hours=2)) == SLOT
    assert not is_due(
        ArticlePlanning("boj_news", {}, last_slot=SLOT.isoformat()), slot=SLOT, now=SLOT
    )


def test_missing_input_is_not_an_empty_success_and_cooldown_survives_slots():
    with pytest.raises(LakePublishError, match="input missing"):
        load_planning(MemoryR2Store(), "boj_news")
    state = ArticlePlanning("boj_news", {}, not_before=(SLOT + timedelta(hours=6)).isoformat())
    assert not is_due(state, slot=SLOT + timedelta(hours=4), now=SLOT + timedelta(hours=4))
    assert is_due(state, slot=SLOT + timedelta(hours=8), now=SLOT + timedelta(hours=8))


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


def test_private_raw_precedes_planning_and_summary_never_reads_past_stages(tmp_path):
    remote = MemoryR2Store()
    publish_planning_input(remote, "boj_news", {"lake_release_id": "initial"})
    state, etag = load_planning(remote, "boj_news")
    entry = commit_stage(
        remote,
        state=state,
        expected_etag=etag,
        slot=SLOT,
        stage_id="boj_news-slot",
        directory=_packet(tmp_path),
        requests={"request": {"etag": "validator"}},
        cursor={"page": 2},
        not_before=None,
    )
    assert remote.put_keys.index(source_key("boj_news")) == 0  # initial input
    assert remote.put_keys[-1] == source_key("boj_news")
    assert remote.put_keys[-2].endswith("/handoff.json")
    remote.get_keys.clear()
    assert pending_handoffs(remote) == [entry]
    assert all(key.startswith("coordination/article-planning/") for key in remote.get_keys)
    acknowledge_stage(remote, entry)
    state, _ = load_planning(remote, "boj_news")
    assert state.pending == ()
    assert state.cursor == {"page": 2}
    assert state.requests == {"request": {"etag": "validator"}}
    assert state.last_slot == SLOT.isoformat()


def test_daily_input_refresh_keeps_acquisition_cas_and_pending_stage(tmp_path):
    remote = MemoryR2Store()
    publish_planning_input(remote, "boj_news", {"lake_release_id": "initial"})
    state, etag = load_planning(remote, "boj_news")
    before = remote.head(source_key("boj_news"))
    writes = len(remote.put_keys)
    publish_planning_input(remote, "boj_news", {"lake_release_id": "new"})
    assert remote.head(source_key("boj_news")) == before
    assert len(remote.put_keys) == writes
    entry = commit_stage(
        remote,
        state=state,
        expected_etag=etag,
        slot=SLOT,
        stage_id="boj_news-stage",
        directory=_packet(tmp_path),
        requests={},
        cursor={"page": 2},
        not_before=None,
    )
    assert entry in pending_handoffs(remote)
    saved, _ = load_planning(remote, "boj_news")
    assert saved.cursor == {"page": 2}
    assert saved.generations == {"lake_release_id": "initial"}


def test_unrelated_planning_writer_conflict_keeps_exact_stage_recoverable(tmp_path, monkeypatch):
    from baibai_jobs.acquisition import article_planning
    from baibai_jobs.acquisition.external_assessments.staging import put_private

    remote = MemoryR2Store()
    publish_planning_input(remote, "boj_news", {"lake_release_id": "initial"})
    state, etag = load_planning(remote, "boj_news")
    original = article_planning.save_handoff

    def save_then_race(store, stage_id, directory, *, namespace):
        original(store, stage_id, directory, namespace=namespace)
        put_private(
            remote,
            source_key("boj_news"),
            ArticlePlanning("boj_news", {"lake_release_id": "other"}).encoded(),
            previous_etag=etag,
        )

    monkeypatch.setattr(article_planning, "save_handoff", save_then_race)
    with pytest.raises(LakeCASConflict):
        commit_stage(
            remote,
            state=state,
            expected_etag=etag,
            slot=SLOT,
            stage_id="boj_news-orphan",
            directory=_packet(tmp_path),
            requests={},
            cursor={"page": 2},
            not_before=None,
        )
    saved, _ = load_planning(remote, "boj_news")
    assert saved.cursor == {}
    assert saved.pending == ()
    assert "coordination/article-sources/boj_news-orphan/handoff.json" in remote.values
    stage = json.loads((tmp_path / "boj_news" / "stage.json").read_text())
    recovery = exact_entry(
        remote,
        source="boj_news",
        stage_id=stage["stage_id"],
        manifest_sha256=stage["manifest_sha256"],
    )
    assert recovery["explicit_article_recovery"] is True


def test_pending_bound_does_not_silently_drop_unsaved_range():
    state = ArticlePlanning("boj_news", {}, pending=tuple({} for _ in range(42)))
    with pytest.raises(ValueError, match="pending stage limit"):
        is_due(state, slot=SLOT, now=SLOT)


def test_planning_budgets_utf8_without_dropping_japanese_evidence():
    state = ArticlePlanning(
        "nhk_news",
        {},
        cursor={"pending_evidence": {str(i): {"title": "あ" * 200} for i in range(128)}},
    )
    escaped_size = len(json.dumps(state.to_json(), sort_keys=True, allow_nan=False).encode())
    assert escaped_size > MAX_PLANNING_BYTES
    payload = state.encoded()
    assert len(payload) <= MAX_PLANNING_BYTES
    assert "あ".encode() in payload
    assert ArticlePlanning.decode(payload, "nhk_news").to_json() == state.to_json()
    legacy = ArticlePlanning("nhk_news", {}, cursor={"title": "以前の形式"})
    assert ArticlePlanning.decode(json.dumps(legacy.to_json()).encode(), "nhk_news") == legacy


def test_compact_planning_still_refuses_actual_byte_overflow():
    with pytest.raises(ValueError, match="bytes budget"):
        ArticlePlanning("nhk_news", {}, cursor={"title": "あ" * 45_000}).encoded()


def test_frontier_is_exact_and_outlives_integration_acknowledgement(tmp_path):
    from baibai_jobs.acquisition.article_frontier import FRONTIER_FORMAT, FRONTIER_REF_FORMAT
    from baibai_jobs.acquisition.article_planning import load_frontier_cursor

    remote = MemoryR2Store()
    source = "nhk_news"
    publish_planning_input(remote, source, {})
    state, etag = load_planning(remote, source)
    stage_id = "nhk_news-frontier"
    cursor = {
        "format": FRONTIER_FORMAT,
        "indexes": [{"stage": {"stage_id": stage_id}, "index_receipt_id": "index"}],
        "observed": {"https://news.web.nhk/index": 0},
        "pending": [["https://news.web.nhk/article", [[0, "/item/0"]]]],
        "legacy_omitted": 190,
        "next_page": None,
        "capacity_blocked_indexes": [],
    }
    entry = commit_stage(
        remote,
        state=state,
        expected_etag=etag,
        slot=SLOT,
        stage_id=stage_id,
        directory=_packet(tmp_path, source),
        requests={},
        cursor=cursor,
        not_before=None,
    )
    saved, _ = load_planning(remote, source)
    assert saved.to_json()["format"] == "article-planning-v2"
    assert saved.cursor == {"format": FRONTIER_REF_FORMAT, "stage": entry}
    resolved = load_frontier_cursor(remote, saved)
    assert resolved["indexes"][0]["stage"] == entry
    assert resolved["legacy_omitted"] == 190
    acknowledge_stage(remote, entry)
    after, _ = load_planning(remote, source)
    assert after.pending == ()
    assert load_frontier_cursor(remote, after) == resolved
    bad = dict(entry, manifest_sha256="0" * 64)
    invalid = ArticlePlanning(source, {}, cursor={"format": FRONTIER_REF_FORMAT, "stage": bad})
    with pytest.raises(LakePublishError, match="manifest differs"):
        load_frontier_cursor(remote, invalid)


@pytest.mark.parametrize(
    "failures,bodies,recoverable",
    [
        (["pending_cursor_overflow"], 8, True),
        (["http_error", "pending_cursor_overflow"], 8, False),
        (["pending_cursor_overflow"], 1, False),
    ],
)
def test_legacy_omission_requires_exact_successful_window_even_after_ack(
    tmp_path, failures, bodies, recoverable
):
    from baibai_jobs.acquisition.article_planning import legacy_omission_evidence

    source = "tsr_data"
    remote = MemoryR2Store()
    publish_planning_input(remote, source, {})
    state, etag = load_planning(remote, source)
    stage_id = "tsr_data-20261029T1313-legacy"
    packet = _packet(tmp_path, source)
    report = json.loads((packet / "result.json").read_text())
    report.update(
        bodies=bodies,
        pages=1,
        status="partial",
        continuation_only=False,
        failures=failures,
        omitted_url_count=1,
        receipt_ids=["index"],
        projection_kinds={"index": "index"},
    )
    (packet / "result.json").write_text(json.dumps(report))
    cursor = {"pending_urls": [], "pending_evidence": {}, "omitted_url_count": 1}
    entry = commit_stage(
        remote,
        state=state,
        expected_etag=etag,
        slot=SLOT,
        stage_id=stage_id,
        directory=packet,
        requests={"index-url": {"stage": {"stage_id": stage_id}, "receipt": {"id": "index"}}},
        cursor=cursor,
        not_before=None,
    )
    acknowledge_stage(remote, entry)
    saved, _ = load_planning(remote, source)
    result = legacy_omission_evidence(remote, saved)
    if recoverable:
        assert result == (entry, [{"stage": entry, "index_receipt_id": "index"}])
        from dataclasses import replace

        assert legacy_omission_evidence(
            remote, replace(saved, cursor={**cursor, "pending_urls": ["changed"]})
        ) is None
    else:
        assert result is None
