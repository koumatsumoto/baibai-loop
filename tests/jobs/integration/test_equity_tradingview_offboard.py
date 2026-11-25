"""Offboarding is not a successful import, replay, or storage deletion."""

import hashlib
import json
from types import SimpleNamespace

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_jobs.acquisition.receipts import available_handoffs, seal_integrated
from baibai_jobs.integration import source_handoff, sources

DAY = "2026-11-25"
CLOCK = DAY + "T11:00:00+00:00"


def _stage(remote: MemoryR2Store, stage: str, source: str, *, terminal: bool = False) -> str:
    key = f"coordination/market-sources/{stage}/handoff.json"
    payload = json.dumps(
        {"result": {"source": source, "asof": DAY, "started_at": CLOCK}},
    ).encode()
    remote.store_object(key, payload)
    if terminal:
        seal_integrated(
            remote,
            {
                "market": True,
                "stage_id": stage,
                "source": source,
                "manifest_sha256": hashlib.sha256(payload).hexdigest(),
                "started_at": CLOCK,
            },
            generations={"lake_release_id": "archived"},
            recovery=False,
        )
    return key


def test_daily_plan_skips_old_equity_tv_inbox_without_consuming_it(tmp_path, monkeypatch):
    remote = MemoryR2Store()
    staged = [_stage(remote, f"tradingview-{DAY}-{n}-1", "tradingview") for n in range(11)]
    for key in staged:
        remote.store_object(key.removesuffix("handoff.json") + "receipts.sqlite", b"old receipt")
    staged.append(_stage(remote, f"tradingview-{DAY}-11-1", "tradingview", terminal=True))
    alternate_key = _stage(remote, "operator-old", "tradingview", terminal=True)
    jpx = _stage(remote, f"tradingview-{DAY}-12-1", "jpx-regulations")
    original = dict(remote.values)
    remote.head_keys.clear()
    remote.get_keys.clear()
    remote.put_keys.clear()
    monkeypatch.setattr(remote, "close", lambda: None, raising=False)
    monkeypatch.setattr(sources, "Boto3R2Store", lambda **_: remote)
    monkeypatch.setattr(sources, "available_ready", lambda _: ([], []))
    monkeypatch.setattr(
        sources,
        "fix_edinet_input",
        lambda *_: SimpleNamespace(
            model_dump=lambda **_: {"fixed": 1},
        ),
    )
    article = {"market": True, "source": "boj_news", "stage_id": "boJ-current"}
    monkeypatch.setattr(sources, "pending_handoffs", lambda _: [article])
    work = tmp_path / "integration"

    assert sources.main(["plan", "--directory", str(work)]) == 0
    handoffs = json.loads((work / "plan.json").read_text())["handoffs"]
    assert {row["source"] for row in handoffs} == {"boj_news", "jpx-regulations"}
    checked = set(remote.head_keys + remote.get_keys)
    assert jpx in checked
    assert set(staged) <= checked
    assert not checked.intersection(
        key.replace("/handoff.json", "/integrated.json") for key in staged
    )
    assert not checked.intersection(
        key.replace("/handoff.json", "/receipts.sqlite") for key in staged
    )
    assert alternate_key in checked
    assert alternate_key.replace("/handoff.json", "/integrated.json") not in checked
    assert remote.put_keys == []
    assert remote.values == original

    # Exact historical inspection is distinct from the live daily plan.
    archived = available_handoffs(
        remote, source="tradingview", market=True, include_integrated=True
    )
    assert {entry["stage_id"] for entry in archived if "invalid" not in entry} >= {
        key.split("/")[-2] for key in staged[:-1]
    }


def test_unreadable_handoff_is_not_hidden_by_an_apparent_tradingview_stage_id():
    remote = MemoryR2Store()
    stage = f"tradingview-{DAY}-199-1"
    key = f"coordination/market-sources/{stage}/handoff.json"
    remote.store_object(key, b"malformed metadata")
    original = dict(remote.values)
    remote.put_keys.clear()

    (entry,) = available_handoffs(remote, market=True, exclude_equity_tradingview=True)
    assert entry["stage_id"] == stage
    assert entry["source"] == "market-sources-unreadable"
    assert entry["invalid"] == "JSONDecodeError"
    assert remote.put_keys == []
    assert remote.values == original


@pytest.mark.parametrize("terminal", ["apply", "seal"])
def test_saved_old_tv_report_cannot_write_or_seal_current_data(tmp_path, monkeypatch, terminal):
    remote = MemoryR2Store()
    _stage(remote, "tradingview-current", "tradingview")
    original = dict(remote.values)
    remote.put_keys.clear()
    monkeypatch.setattr(remote, "close", lambda: None, raising=False)
    monkeypatch.setattr(sources, "Boto3R2Store", lambda **_: remote)
    work = tmp_path / "integration"
    work.mkdir()
    entry = {"market": True, "source": "tradingview", "stage_id": "tradingview-current"}
    report = work / ("plan.json" if terminal == "apply" else "applied.json")
    report.write_text(
        json.dumps({"handoffs": [entry] if terminal == "apply" else [{"entry": entry}]}),
    )
    before = report.read_bytes()
    assert (
        sources.main(
            [
                terminal,
                "--directory",
                str(work),
                "--asof",
                DAY,
                "--lease-handle",
                str(tmp_path / "unused.json"),
            ],
        )
        == 1
    )
    assert report.read_bytes() == before
    assert remote.values == original
    assert remote.head_keys == remote.get_keys == remote.put_keys == []


@pytest.mark.parametrize("command", ["acquire", "export", "stage", "replay-copy"])
def test_only_fixed_read_or_explicit_local_replay_remain(
    tmp_path,
    monkeypatch,
    command,
):
    def forbidden(**_):
        pytest.fail("retired or unsafe command must stop before connecting to the store")

    monkeypatch.setattr(source_handoff, "Boto3R2Store", forbidden)
    assert (
        source_handoff.main(
            [
                command,
                "--source",
                "tradingview",
                "--directory",
                str(tmp_path),
                "--asof",
                DAY,
                "--stage-id",
                "tradingview-current",
            ],
        )
        == 1
    )
