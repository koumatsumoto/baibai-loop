"""Retired J-Quants SQL handoffs must not come back via manual staging or delayed seals."""

import json
import sqlite3
from contextlib import closing

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_jobs.acquisition.receipts import available_handoffs, seal_integrated
from baibai_jobs.integration import source_handoff


@pytest.mark.parametrize(
    ("stage", "packet_source", "allowed"),
    [
        ("jquants-market-2026-10-26-40340781831-1", "jpx-earnings", False),
        ("jquants-financial-2024-02-29-12-2", "jpx-earnings", False),
        ("manual", "jquants-market", False),
        ("manual", "jquants-financial", False),
        ("manual", "jpx-regulations", False),
        ("jquants-market-2026-10-26-40340781831-1-manual", "jpx-earnings", True),
        ("jquants-financial-2026-02-29-12-2", "jpx-earnings", True),
    ],
)
def test_stage_uses_packet_source_and_only_reserves_old_scheduled_ids(
    tmp_path, monkeypatch, stage, packet_source, allowed
):
    store = MemoryR2Store()
    monkeypatch.setattr(store, "close", lambda: None, raising=False)
    monkeypatch.setattr(source_handoff, "Boto3R2Store", lambda **_: store)
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    (tmp_path / "result.json").write_text(
        json.dumps(
            {
                "source": packet_source,
                "asof": "2026-10-26",
                "started_at": "2026-10-26T10:00:00Z",
                "bundle_ids": ["fixture-original"],
            }
        )
    )
    with closing(sqlite3.connect(tmp_path / "receipts.sqlite")) as db:
        db.execute("CREATE TABLE receipt (id TEXT)")

    rc = source_handoff.main(
        ["stage", "--source", "jpx-earnings", "--stage-id", stage, "--directory", str(tmp_path)]
    )

    if allowed:
        assert rc == 0
        assert output.read_text() == f"handoff={stage}\n"
        (saved,) = available_handoffs(store, market=True)
        assert (saved["stage_id"], saved["source"]) == (stage, packet_source)
    else:
        assert rc == 1
        assert not output.exists()
        assert store.head_keys == store.get_keys == store.put_keys == []
        assert not store.values


@pytest.mark.parametrize(
    ("market", "stage", "source", "allowed"),
    [
        (True, "manual", "jquants-financial", False),
        (True, "jquants-market-2026-10-26-40340781831-1", "jpx-regulations", False),
        (False, "jquants-market-2026-10-26-40340781831-1", "economic-jquants", True),
        (True, "jquants-market-2026-10-26-40340781831-1-manual", "jpx-regulations", True),
    ],
)
def test_delayed_seal_cannot_recreate_retired_market_stage(market, stage, source, allowed):
    store = MemoryR2Store()
    entry = {
        "market": market,
        "stage_id": stage,
        "source": source,
        "manifest_sha256": "a" * 64,
        "started_at": "2026-10-26T10:00:00Z",
    }
    if allowed:
        seal_integrated(store, entry, generations={}, recovery=True)
        namespace = "market-sources" if market else "economic-sources"
        assert store.put_keys == [f"coordination/{namespace}/{stage}/integrated.json"]
    else:
        with pytest.raises(ValueError, match="J-Quants"):
            seal_integrated(store, entry, generations={}, recovery=True)
        assert store.head_keys == store.get_keys == store.put_keys == []
        assert not store.values
