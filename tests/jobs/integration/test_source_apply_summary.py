import json
from contextlib import closing

import pytest
from tests.helpers.edinet_native import empty_edinet_input
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.integration import sources


@pytest.mark.parametrize("has_external", [False, True])
def test_apply_summary_changes_only_stdout(tmp_path, monkeypatch, capsys, has_external):
    store = MemoryR2Store()
    monkeypatch.setattr(store, "close", lambda: None, raising=False)
    monkeypatch.setattr(sources, "Boto3R2Store", lambda **_: store)
    edinet_input = empty_edinet_input(store, tmp_path)
    market = tmp_path / "market.sqlite"
    with closing(open_connection(market)):
        pass

    # A daily-sized manifest, including the independently retained plan details.
    digests = {f"synthetic-receipt-{i:05}": f"{i:064x}" for i in range(22_140)}
    plan = {"bundle_receipts": {"synthetic": digests}}
    external = {
        "plans": [plan],
        "failed": [{"reason": "synthetic failure"}],
        "changed": [["external_assessment.valuations", 2026, 11]],
        "selected_digests": digests,
    }
    monkeypatch.setattr(sources, "merge_ready", lambda *_, **__: external)
    monkeypatch.setattr(
        sources, "extract_saved_edinet", lambda *_, **__: {"metrics": {"exit_code": 1}}
    )

    work = tmp_path / "integration"
    work.mkdir()
    (work / "plan.json").write_text(
        json.dumps(
            {
                "edinet_input": edinet_input.model_dump(mode="json"),
                "handoffs": [{"source": "synthetic", "market": True, "invalid": True}],
                "external": [{"source": "synthetic"}] if has_external else [],
                "external_failed": [{"source": "synthetic-unready"}],
            }
        )
    )
    args = ["apply", "--directory", str(work), "--market", str(market), "--asof", "2026-11-24"]
    assert sources.main(args) == 0
    original = (work / "applied.json").read_bytes()
    report = json.loads(original)
    assert json.loads(capsys.readouterr().out) == report
    assert report["external"] == (external if has_external else None)

    assert sources.main([*args, "--summary"]) == 0
    output = capsys.readouterr().out
    assert len(output) < 512
    assert json.loads(output) == {
        "asof": "2026-11-24",
        "handoff_count": 1,
        "applied_handoff_count": 0,
        "failed_handoff_count": 1,
        "extraction_count": 3,
        "failed_extraction_count": 1,
        "external_ready_failure_count": 1,
        "external": {
            "applied_plan_count": 1,
            "failed_plan_count": 1,
            "changed_partition_count": 1,
            "selected_receipt_count": 22_140,
        }
        if has_external
        else None,
    }
    assert (work / "applied.json").read_bytes() == original


def test_apply_summary_rejects_other_commands(tmp_path, capsys):
    assert sources.main(["check", "--directory", str(tmp_path), "--summary"]) == 1
    assert capsys.readouterr().err == "--summary is only valid with apply\n"
