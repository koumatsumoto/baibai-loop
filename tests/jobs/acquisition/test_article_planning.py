import json
from datetime import timedelta

import pytest
from tests.helpers.article_acquisition import SLOT, _packet
from tests.helpers.edinet_native import empty_edinet_input
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.storage.objects.r2 import LakeCASConflict, LakePublishError
from baibai_jobs.acquisition.article_planning import (
    MAX_PLANNING_BYTES,
    ArticlePlanning,
    commit_stage,
    index_poll_due,
    is_due,
    load_planning,
    pending_handoffs,
    publish_planning_input,
    slot_at,
    source_key,
)
from baibai_jobs.acquisition.receipts import seal_integrated
from baibai_jobs.maintenance.article_recovery import prepare


def test_six_wakeups_do_not_assume_a_fixed_tsr_launch_hour():
    news = ArticlePlanning("boj_news", {})
    tsr = ArticlePlanning("tsr_data", {})
    slots = [SLOT.replace(hour=hour) for hour in (1, 5, 9, 13, 17, 21)]
    assert all(is_due(news, slot=slot, now=slot) for slot in slots)
    assert all(is_due(tsr, slot=slot, now=slot) for slot in slots)
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


@pytest.mark.parametrize("source", ["boj_news", "minkabu_news", "rakuten_news"])
def test_private_raw_precedes_planning_and_seal_never_mutates_acquisition(tmp_path, source):
    remote = MemoryR2Store()
    publish_planning_input(remote, source, {"lake_release_id": "initial"})
    state, etag = load_planning(remote, source)
    entry = commit_stage(
        remote,
        state=state,
        expected_etag=etag,
        slot=SLOT,
        stage_id=f"{source}-slot",
        directory=_packet(tmp_path, source),
        requests={"request": {"etag": "validator"}},
        cursor={"page": 2},
        not_before=None,
    )
    assert remote.put_keys.index(source_key(source)) == 0  # initial input
    assert remote.put_keys[-1] == source_key(source)
    assert remote.put_keys[-2].endswith("/handoff.json")
    remote.get_keys.clear()
    (available,) = pending_handoffs(remote)
    assert all(available[key] == value for key, value in entry.items())
    assert all(not key.endswith(".sqlite") for key in remote.get_keys)
    original_etag = remote.head(source_key(source)).etag
    seal_integrated(remote, entry, generations={}, recovery=False)
    assert pending_handoffs(remote) == []
    assert remote.head(source_key(source)).etag == original_etag
    state, _ = load_planning(remote, source)
    assert state.last_stage == entry
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
    assert any(item["stage_id"] == entry["stage_id"] for item in pending_handoffs(remote))
    saved, _ = load_planning(remote, "boj_news")
    assert saved.cursor == {"page": 2}
    assert saved.generations == {"lake_release_id": "initial"}


def test_unrelated_planning_writer_conflict_keeps_exact_stage_recoverable(tmp_path, monkeypatch):
    from baibai_engine.storage.objects.json import put_private
    from baibai_jobs.acquisition import article_planning

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
    assert saved.last_stage is None
    assert [item["stage_id"] for item in pending_handoffs(remote)] == ["boj_news-orphan"]
    assert "coordination/article-sources/boj_news-orphan/handoff.json" in remote.values
    stage = json.loads((tmp_path / "boj_news" / "stage.json").read_text())
    empty_edinet_input(remote, tmp_path)
    prepare(
        remote,
        tmp_path / "recovery",
        source="boj_news",
        stage_id=stage["stage_id"],
        manifest_sha256=stage["manifest_sha256"],
    )
    recovery = json.loads((tmp_path / "recovery/plan.json").read_text())["handoffs"][0]
    assert recovery["explicit_article_recovery"] is True


def test_legacy_backlog_does_not_stop_acquisition_and_keeps_only_latest_reference():
    entries = [
        {
            "source": "boj_news",
            "namespace": "article-sources",
            "market": True,
            "stage_id": f"boj_news-{number}",
        }
        for number in range(42)
    ]
    legacy = {
        **ArticlePlanning("boj_news", {}).to_json(),
        "format": "article-planning-v1",
        "pending": entries,
    }
    legacy.pop("last_stage")
    state = ArticlePlanning.decode(json.dumps(legacy).encode(), "boj_news")
    assert is_due(state, slot=SLOT, now=SLOT)
    assert state.last_stage == entries[-1]
    assert "pending" not in state.to_json()
    assert state.to_json()["format"] == "article-planning-v3"


@pytest.mark.parametrize("format_version", ["article-planning-v1", "article-planning-v2"])
@pytest.mark.parametrize("damage", [None, "missing", "changed"])
def test_legacy_transition_keeps_all_unprocessed_original_references(
    tmp_path, format_version, damage
):
    from baibai_jobs.acquisition.article_discovery import DISCOVERY_REF_FORMAT

    remote = MemoryR2Store()
    publish_planning_input(remote, "boj_news", {})
    entries = []
    for index in range(3):
        root = tmp_path / str(index)
        root.mkdir()
        state, etag = load_planning(remote, "boj_news")
        entries.append(
            commit_stage(
                remote,
                state=state,
                expected_etag=etag,
                slot=SLOT + timedelta(hours=index * 4),
                stage_id=f"boj_news-legacy-{index}",
                directory=_packet(root),
                requests={},
                cursor={},
                not_before=None,
            )
        )
    state, _ = load_planning(remote, "boj_news")
    value = {**state.to_json(), "format": format_version, "pending": entries}
    value.pop("last_stage")
    if format_version == "article-planning-v2":
        value["cursor"] = {"format": DISCOVERY_REF_FORMAT, "stage": entries[-1]}
    remote.store_object(source_key("boj_news"), json.dumps(value).encode())
    saved = remote.values[source_key("boj_news")]
    key = f"coordination/article-sources/{entries[0]['stage_id']}/handoff.json"
    if damage == "missing":
        del remote.values[key]
    elif damage == "changed":
        remote.store_object(key, b"changed original")
    state, etag = load_planning(remote, "boj_news")
    assert remote.values[source_key("boj_news")] == saved
    assert state.last_stage == entries[-1]
    assert len(pending_handoffs(remote)) == 3
    root = tmp_path / "new"
    root.mkdir()
    commit_stage(
        remote,
        state=state,
        expected_etag=etag,
        slot=SLOT + timedelta(hours=12),
        stage_id="boj_news-after-legacy",
        directory=_packet(root),
        requests={},
        cursor={},
        not_before=None,
    )
    assert "pending" not in json.loads(remote.values[source_key("boj_news")].body)
    found = pending_handoffs(remote)
    assert len(found) == 4
    assert len([entry for entry in found if entry.get("invalid")]) == int(damage is not None)


def test_discovery_does_not_need_planning_or_a_source_watermark(tmp_path):
    remote = MemoryR2Store()
    publish_planning_input(remote, "boj_news", {})
    entries = []
    for index in range(2):
        root = tmp_path / str(index)
        root.mkdir()
        packet = _packet(root)
        report = json.loads((packet / "result.json").read_text())
        report["started_at"] = (SLOT + timedelta(hours=index * 4)).isoformat()
        (packet / "result.json").write_text(json.dumps(report))
        state, etag = load_planning(remote, "boj_news")
        entries.append(
            commit_stage(
                remote,
                state=state,
                expected_etag=etag,
                slot=SLOT + timedelta(hours=index * 4),
                stage_id=f"boj_news-late-{index}",
                directory=packet,
                requests={},
                cursor={},
                not_before=None,
            )
        )
    seal_integrated(remote, entries[-1], generations={}, recovery=False)
    for missing_planning in (True, False):
        if missing_planning:
            del remote.values[source_key("boj_news")]
        else:
            remote.store_object(source_key("boj_news"), b"broken mutable planning")
        remote.get_keys.clear()
        found = pending_handoffs(remote)
        (old,) = [entry for entry in found if not entry.get("invalid")]
        assert old["stage_id"] == entries[0]["stage_id"]
        assert old["late_before_integrated"] is False
        assert (source_key("boj_news") in remote.get_keys) is not missing_planning


@pytest.mark.parametrize(
    "damage", [{"manifest_sha256": "0" * 64}, {"source": "mof_news"}, {"status": "unknown"}]
)
def test_incorrect_article_seal_cannot_hide_the_original(tmp_path, damage):
    remote = MemoryR2Store()
    publish_planning_input(remote, "boj_news", {})
    state, etag = load_planning(remote, "boj_news")
    entry = commit_stage(
        remote,
        state=state,
        expected_etag=etag,
        slot=SLOT,
        stage_id="boj_news-seal",
        directory=_packet(tmp_path),
        requests={},
        cursor={},
        not_before=None,
    )
    seal_integrated(remote, entry, generations={}, recovery=False)
    key = "coordination/article-sources/boj_news-seal/integrated.json"
    remote.store_object(key, json.dumps({**json.loads(remote.values[key].body), **damage}).encode())
    (invalid,) = pending_handoffs(remote)
    assert invalid["stage_id"] == entry["stage_id"]
    assert invalid["invalid"] == "ValueError"
    assert invalid["namespace"] == "article-sources"


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
    old_wire = {**legacy.to_json(), "format": "article-planning-v1", "pending": []}
    old_wire.pop("last_stage")
    assert (
        ArticlePlanning.decode(json.dumps(old_wire).encode(), "nhk_news").to_json()
        == legacy.to_json()
    )


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
    assert saved.to_json()["format"] == "article-planning-v3"
    assert saved.cursor == {"format": FRONTIER_REF_FORMAT, "stage": entry}
    resolved = load_frontier_cursor(remote, saved)
    assert resolved["indexes"][0]["stage"] == entry
    assert resolved["legacy_omitted"] == 190
    seal_integrated(remote, entry, generations={}, recovery=False)
    after, _ = load_planning(remote, source)
    assert after.last_stage == entry
    assert load_frontier_cursor(remote, after) == resolved
    bad = dict(entry, manifest_sha256="0" * 64)
    invalid = ArticlePlanning(source, {}, cursor={"format": FRONTIER_REF_FORMAT, "stage": bad})
    with pytest.raises(LakePublishError, match="manifest differs"):
        load_frontier_cursor(remote, invalid)


@pytest.mark.parametrize(
    ("failures", "bodies", "recoverable"),
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
    seal_integrated(remote, entry, generations={}, recovery=False)
    saved, _ = load_planning(remote, source)
    result = legacy_omission_evidence(remote, saved)
    if recoverable:
        assert result == (entry, [{"stage": entry, "index_receipt_id": "index"}])
        from dataclasses import replace

        assert (
            legacy_omission_evidence(
                remote, replace(saved, cursor={**cursor, "pending_urls": ["changed"]})
            )
            is None
        )
    else:
        assert result is None


@pytest.mark.parametrize("source", ["tsr_data", "tsr_flash"])
def test_tsr_eligibility_uses_receipts_later_but_preserves_quota_and_slot_bounds(source):
    slot = SLOT.replace(hour=1)
    assert is_due(ArticlePlanning(source, {}), slot=slot, now=slot)
    assert is_due(ArticlePlanning(source, {}, last_slot=slot.isoformat()), slot=slot, now=slot)
    state = ArticlePlanning(source, {}, not_before=(slot + timedelta(hours=1)).isoformat())
    assert not is_due(state, slot=slot, now=slot)
    with pytest.raises(ValueError, match="invalid article acquisition slot"):
        is_due(ArticlePlanning(source, {}), slot=slot, now=slot - timedelta(seconds=1))


@pytest.mark.parametrize(
    ("now", "received", "expected"),
    [
        (SLOT + timedelta(hours=4), None, True),
        (SLOT + timedelta(hours=4), SLOT + timedelta(minutes=1), False),
        (SLOT + timedelta(days=1, seconds=-1), SLOT + timedelta(minutes=1), False),
        (SLOT + timedelta(days=1), SLOT + timedelta(minutes=1), True),
        (SLOT, SLOT - timedelta(seconds=1), True),
    ],
)
def test_tsr_daily_poll_boundary_uses_actual_native_index_receipt(now, received, expected):
    assert index_poll_due("tsr_data", now=now, received_at=received) is expected
    assert index_poll_due("tsr_flash", now=now, received_at=received) is expected
    assert index_poll_due("nhk_news", now=now, received_at=received) is True
