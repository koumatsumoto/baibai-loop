"""Daily plan discovers immutable article originals independently of planning."""

import json

from tests.helpers.article_acquisition import SLOT, _packet
from tests.helpers.edinet_native import empty_edinet_input
from tests.helpers.r2_store import MemoryR2Store

from baibai_jobs.acquisition.article_planning import (
    commit_stage,
    load_planning,
    publish_planning_input,
    source_key,
)
from baibai_jobs.integration import sources


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
