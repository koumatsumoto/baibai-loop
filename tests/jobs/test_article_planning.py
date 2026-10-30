from datetime import UTC, datetime, timedelta

import pytest

from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.storage.objects.r2 import LakeCASConflict, LakePublishError
from baibai_jobs.acquisition.article_planning import (
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
    import json

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


def test_cas_failure_leaves_recoverable_raw_but_no_cursor_advance(tmp_path):
    remote = MemoryR2Store()
    publish_planning_input(remote, "boj_news", {"lake_release_id": "initial"})
    state, etag = load_planning(remote, "boj_news")
    publish_planning_input(remote, "boj_news", {"lake_release_id": "new"})
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


def test_pending_bound_does_not_silently_drop_unsaved_range():
    state = ArticlePlanning("boj_news", {}, pending=tuple({} for _ in range(42)))
    with pytest.raises(ValueError, match="pending stage limit"):
        is_due(state, slot=SLOT, now=SLOT)
