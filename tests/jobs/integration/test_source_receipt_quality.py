"""Only the known failed replay gets a bounded marker; it is never a successful check."""

import json
from copy import deepcopy

import pytest

from baibai_jobs.integration.sources import main


def _report(day, n, external):
    ids = [f"receipt-{i}" for i in range(n)]
    ready = {"market": True, "integration": None, "late_before_integrated": False, "asof": day}
    return {
        "asof": day,
        "external": external,
        "external_failed": [],
        "extractions": {
            name: {"exit_code": 0, "status": "completed"}
            for name in ("edinet-facts", "edinet-metrics", "native_lists", "native_archives")
        },
        "handoffs": [
            {
                "entry": {**ready, "source": "tradingview", "stage_id": f"tradingview-{day}"},
                "applied": True,
                "failed": True,
                "recovery": True,
                "result": {
                    "snapshot_date": day,
                    "rows": 0,
                    "rows_before": 0,
                    "rows_added": 0,
                    "unresolved": 0,
                    "expected_universe": 3689,
                    "remaining": 3689,
                    "receipt_ids": ids,
                    "rejected_receipts": [
                        {"reason": "provider_response", "receipt_id": i} for i in ids
                    ],
                },
            },
            {
                "entry": {**ready, "source": "nhk_news", "stage_id": f"nhk_news-{day}"},
                "applied": True,
                "failed": False,
                "recovery": False,
                "result": {"acquisition_issues": ["nonfatal source gap"]},
            },
        ],
    }


@pytest.mark.parametrize(
    ("day", "n", "external"),
    [
        ("2026-11-19", 3, None),  # manual 44564236173
        ("2026-11-18", 1, {"failed": []}),  # scheduled 44402847233
    ],
)
def test_only_the_marker_changes_for_the_two_observed_failure_shapes(
    tmp_path, capsys, day, n, external
):
    path = tmp_path / "applied.json"
    original = json.dumps(_report(day, n, external))
    path.write_text(original)
    args = ["check", "--directory", str(tmp_path)]
    assert main(args) == 1
    before = capsys.readouterr()
    summary = json.loads(before.out)
    assert summary["integration_status"] == "failed"
    assert summary["source_gaps"][0]["source"] == "nhk_news"
    output = tmp_path / "output"
    assert main([*args, "--quality-output", str(output)]) == 1
    assert capsys.readouterr() == before
    assert path.read_text() == original
    assert output.read_text().splitlines() == [f"tv_quality_asof={day}"]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("external", {"failed": [1]}),
        ("external", {}),
        ("external_failed", [1]),
        ("extractions", {}),
        ("extractions.edinet-facts.exit_code", 1),
        ("extractions.edinet-metrics.exit_code", False),
        ("extractions.native_lists.status", "partial"),
        ("extractions.edinet-metrics.deferred_inputs", ["missing"]),
        ("handoffs.1.failed", True),
        ("handoffs.1.applied", False),
        ("handoffs.1.recovery", True),
        ("handoffs.0.applied", False),
        ("handoffs.0.recovery", False),
        ("handoffs.0.entry.invalid", "ValueError"),
        ("handoffs.0.entry.late_before_integrated", True),
        ("handoffs.0.entry.integration", {"status": "needs_explicit_recovery"}),
        ("handoffs.0.entry.market", False),
        ("handoffs.0.entry.source", "other"),
        ("handoffs.0.entry.asof", "2026-11-17"),
        ("asof", "2026-11-19\ninvalid=1"),
        ("handoffs.0.result.reason", "SQLiteSchemaError"),
        ("handoffs.0.result.snapshot_date", "2026-11-17"),
        ("handoffs.0.result.rows", 1),
        ("handoffs.0.result.rows_added", 1),
        ("handoffs.0.result.expected_universe", 0),
        ("handoffs.0.result.remaining", 1),
        ("handoffs.0.result.rejected_receipts", []),
        ("handoffs.0.result.receipt_ids", []),
        ("handoffs.0.result.rejected_receipts.1.reason", "time_guard"),
        ("handoffs.0.result.rejected_receipts.1.reason", "unknown"),
        ("handoffs.0.result.rejected_receipts.1.receipt_id", "not-fixed"),
        ("handoffs.0.result.receipt_ids.1", "receipt-0"),
    ],
)
def test_other_or_ambiguous_failure_never_emits_output(tmp_path, path, value):
    report = _report("2026-11-19", 3, None)
    node = report
    *parents, key = [int(v) if v.isdigit() else v for v in path.split(".")]
    for part in parents:
        node = node[part]
    node[key] = value
    (tmp_path / "applied.json").write_text(json.dumps(report))
    output = tmp_path / "output"
    assert main(["check", "--directory", str(tmp_path), "--quality-output", str(output)]) == 1
    assert not output.exists()


def test_neither_success_nor_multiple_failed_stages_are_the_one_known_quality_failure(tmp_path):
    report = _report("2026-11-19", 3, None)
    report["handoffs"].append(deepcopy(report["handoffs"][0]))
    path, output = tmp_path / "applied.json", tmp_path / "output"
    args = ["check", "--directory", str(tmp_path), "--quality-output", str(output)]
    path.write_text(json.dumps(report))
    assert main(args) == 1
    assert not output.exists()
    report["handoffs"] = [report["handoffs"][1]]
    path.write_text(json.dumps(report))
    assert main(args) == 0
    assert not output.exists()
