import json
from contextlib import closing

import pytest

from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_jobs.jobs import source_integration

pytestmark = pytest.mark.usefixtures("_no_r2")


@pytest.fixture
def _no_r2(monkeypatch):
    for name in (
        "R2_ACCOUNT_ID",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_STORES_BUCKET",
    ):
        monkeypatch.delenv(name, raising=False)

    def unexpected_store(*args, **kwargs):
        pytest.fail("local read-only command must not construct an R2 client")

    monkeypatch.setattr(source_integration, "Boto3R2Store", unexpected_store)


@pytest.mark.parametrize(
    ("failed_part", "expected"),
    [(None, 0), ("handoff", 1), ("edinet", 1), ("external_ready", 1), ("external", 1)],
)
def test_check_without_r2_reports_real_local_completeness(tmp_path, capsys, failed_part, expected):
    report = {
        "handoffs": [],
        "extractions": {"edinet-metrics": {"exit_code": 0}, "edinet-facts": {"exit_code": 0}},
        "external_failed": [],
        "external": None,
    }
    if failed_part == "handoff":
        report["handoffs"] = [{"failed": True}]
    elif failed_part == "edinet":
        # Initial production bootstrap: no handoffs, metrics list still missing.
        report["extractions"]["edinet-metrics"]["exit_code"] = 1
    elif failed_part == "external_ready":
        report["external_failed"] = [{"reason": "invalid_ready"}]
    elif failed_part == "external":
        report["external"] = {"failed": [{"reason": "decode_mismatch"}]}
    path = tmp_path / "applied.json"
    path.write_text(json.dumps(report))
    before = path.read_bytes()
    entries_before = set(tmp_path.iterdir())
    assert source_integration.main(["check", "--directory", str(tmp_path)]) == expected
    assert capsys.readouterr().err == ""
    assert path.read_bytes() == before
    assert set(tmp_path.iterdir()) == entries_before


def test_check_missing_report_does_not_create_a_directory(tmp_path, capsys):
    missing = tmp_path / "absent"
    assert source_integration.main(["check", "--directory", str(missing)]) == 1
    assert "FileNotFoundError" in capsys.readouterr().err
    assert not missing.exists()


def test_verify_without_r2_reads_local_accepted_receipts_without_changing_them(tmp_path):
    economic = tmp_path / "economic.sqlite"
    packet = tmp_path / "receipt-00000"
    packet.mkdir()
    accepted = packet / "accepted.sqlite"
    for database in (economic, accepted):
        with closing(initialize_database(database)):
            pass
    report = {"handoffs": [{"entry": {"market": False}, "applied": True}]}
    path = tmp_path / "applied.json"
    path.write_text(json.dumps(report))
    before = {item: item.read_bytes() for item in (economic, accepted, path)}
    arguments = ["verify", "--directory", str(tmp_path), "--economic", str(economic)]
    assert source_integration.main(arguments) == 0
    assert {item: item.read_bytes() for item in before} == before


def test_deferred_input_is_reported_without_failing_successful_integration(tmp_path, capsys):
    report = {
        "handoffs": [],
        "external_failed": [],
        "external": None,
        "extractions": {
            "edinet-metrics": {
                "exit_code": 0,
                "status": "deferred_input",
                "input_asof": "2026-10-23",
                "deferred_inputs": ["latest_document_list_delayed"],
            }
        },
    }
    path = tmp_path / "applied.json"
    path.write_text(json.dumps(report))
    before = path.read_bytes()
    assert source_integration.main(["check", "--directory", str(tmp_path)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["integration_status"] == "completed_with_deferred_inputs"
    assert summary["deferred_inputs"]["edinet-metrics"]["input_asof"] == "2026-10-23"
    assert path.read_bytes() == before


def test_invalid_retained_list_failure_is_not_hidden_by_another_input_delay(tmp_path, capsys):
    report = {
        "handoffs": [],
        "external_failed": [],
        "external": None,
        "extractions": {
            "edinet-metrics": {"exit_code": 0, "deferred_inputs": ["document_list_unavailable"]},
            "edinet-list": {"exit_code": 1, "status": "failed"},
        },
    }
    (tmp_path / "applied.json").write_text(json.dumps(report))
    assert source_integration.main(["check", "--directory", str(tmp_path)]) == 1
    assert json.loads(capsys.readouterr().out)["integration_status"] == "failed"
