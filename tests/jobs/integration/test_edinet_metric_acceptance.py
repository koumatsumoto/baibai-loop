"""Only explicitly compared EDINET revisions/results block the requested publication."""

import hashlib
import json
import sqlite3
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from tests.helpers.edinet_metrics import _edinet_csv_zip, _edinet_document
from tests.helpers.edinet_native import native_edinet_store

from baibai_engine.data_collection.providers.edinet.archive_native import NativeArchive
from baibai_engine.data_collection.providers.edinet.models import EdinetResponse
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.edinet_inputs import (
    read_edinet_input,
    retain_edinet_input,
)
from baibai_engine.machine_analysis.observations.financials.edinet_integration import (
    extract_saved_edinet,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.revision import (
    EDINET_METRIC_REVISION,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.store import (
    read_edinet_metric_baseline,
)
from baibai_jobs.integration.edinet_list_native import materialize_lists
from baibai_jobs.integration.edinet_metric_acceptance import (
    decode_acceptance,
    metric_result_sha256,
    verify_acceptance,
)
from baibai_jobs.integration.workflow_inputs import WorkflowInputError, validate_daily_input

DAY = date(2026, 4, 1)
AT = datetime(2026, 4, 1, 12, tzinfo=UTC)


@pytest.fixture
def compared(tmp_path, monkeypatch):
    document = _edinet_document(ticker="72030", doc_id="S100CASE")
    store, list_owner, archive_owner = native_edinet_store(tmp_path, monkeypatch, {DAY: [document]})
    body = _edinet_csv_zip()
    lists = list_owner.freeze()
    archives = archive_owner.publish(
        [NativeArchive.from_response("S100CASE", 5, EdinetResponse(AT, body))]
    )
    saved = retain_edinet_input(store, lists, archives, selected_at=AT)
    database = tmp_path / "market.sqlite"
    materialize_lists(lists, database)
    extractions = extract_saved_edinet(
        database, asof=DAY, lookback_days=0, metric_archives=archives, fact_archives=archives
    )
    baseline = read_edinet_metric_baseline(database, DAY)
    row = baseline.rows["7203"]
    expected = {
        "as_of": DAY.isoformat(),
        "extractor_revision": EDINET_METRIC_REVISION,
        "list_sha256": lists.identity,
        "archive_sha256": archives.identity,
        "samples": [
            {
                "ticker": "7203",
                "source_doc_id": "S100CASE",
                "source_document_revision": row.source_document_revision,
                "archive_sha256": hashlib.sha256(body).hexdigest(),
                "metric_sha256": metric_result_sha256(row.record),
            }
        ],
    }
    report = {
        "asof": DAY.isoformat(),
        "edinet_input": saved.model_dump(mode="json"),
        "extractions": extractions,
    }
    store.get_keys.clear()
    store.put_keys.clear()
    return store, database, expected, report


def verify(compared):
    store, database, expected, report = compared
    return verify_acceptance(
        raw=json.dumps(expected), report=report, database=database, store=store
    )


@pytest.mark.parametrize("status", ["completed", "unchanged"])
def test_exact_saved_results_and_sources_are_read_only(compared, status):
    store, database, _expected, report = compared
    report["extractions"]["edinet-metrics"]["status"] = status
    before = database.read_bytes(), dict(store.values)
    result = verify(compared)
    assert result["status"] == "accepted"
    assert result["sample_count"] == result["metric_count"] == 1
    assert result["extractor_revision"] == EDINET_METRIC_REVISION
    assert database.read_bytes() == before[0]
    assert store.values == before[1]
    assert store.put_keys == []
    assert len(store.get_keys) == 3  # Fixed metadata only; no new source acquisition.
    assert "samples" not in result
    assert "cash" not in result


@pytest.mark.parametrize("status", ["deferred_input", "partial", "failed", "prepared", None])
def test_noncomplete_integration_never_reaches_remote_read(compared, status):
    store, _, _, report = compared
    report["extractions"]["edinet-metrics"]["status"] = status
    with pytest.raises(ValueError, match=r"EDINET|Expecting"):
        verify(compared)
    assert not store.get_keys
    assert not store.put_keys


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("exit_code", 1),
        ("exit_code", False),
        ("deferred_inputs", ["csv_archives_unavailable"]),
        ("snapshot_asof", "2026-03-31"),
        ("input_asof", "2026-03-31"),
    ],
)
def test_status_does_not_hide_unfinished_or_different_input(compared, field, value):
    compared[3]["extractions"]["edinet-metrics"][field] = value
    with pytest.raises(ValueError, match=r"EDINET|Expecting"):
        verify(compared)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("as_of", "2026-03-31"),
        ("extractor_revision", "edinet-metrics-v15-commercial-paper-debt"),
        ("list_sha256", "0" * 64),
        ("archive_sha256", "0" * 64),
    ],
)
def test_changed_date_revision_or_fixed_pair_is_rejected(compared, field, value):
    compared[2][field] = value
    with pytest.raises(ValueError, match=r"EDINET|Expecting"):
        verify(compared)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ticker", "9999"),
        ("source_doc_id", "S100OTHER"),
        ("source_document_revision", "0" * 64),
        ("archive_sha256", "0" * 64),
        ("metric_sha256", "0" * 64),
    ],
)
def test_compared_sample_identity_original_and_result_are_all_required(compared, field, value):
    compared[2]["samples"][0][field] = value
    with pytest.raises(ValueError, match=r"EDINET|Expecting"):
        verify(compared)


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE edinet_metrics SET extractor_revision='edinet-metrics-v15-commercial-paper-debt'",
        "UPDATE edinet_metrics SET cash=cash+1",
        "UPDATE edinet_metrics SET source_document_revision=NULL",
        "UPDATE source_coverage SET record_count=2 WHERE source='edinet_metrics'",
        "DELETE FROM edinet_metrics",
    ],
)
def test_saved_baseline_is_revalidated_instead_of_trusting_report(compared, change):
    with sqlite3.connect(compared[1]) as connection:
        connection.execute(change)
    with pytest.raises((ValueError, RuntimeError)):
        verify(compared)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda x: x.update(extra="ignored"),
        lambda x: x.update(as_of="20260401"),
        lambda x: x.update(samples=[]),
        lambda x: x.update(samples=x["samples"] * 2),
        lambda x: x["samples"][0].update(metric_sha256="bad"),
        lambda x: x["samples"][0].update(ticker="$(exit 99)"),
        lambda x: x["samples"][0].update(command="ignored"),
    ],
)
def test_conditions_have_no_freeform_commands_or_silent_missing_fields(compared, mutation):
    conditions = deepcopy(compared[2])
    mutation(conditions)
    with pytest.raises(ValueError, match=r"EDINET|Expecting"):
        decode_acceptance(json.dumps(conditions))


@pytest.mark.parametrize("raw", ["", "null", "[]", "{", " " * 16_385])
def test_invalid_or_unbounded_conditions_are_rejected(raw):
    with pytest.raises(ValueError, match=r"EDINET|Expecting"):
        decode_acceptance(raw)


def test_dispatch_validation_requires_explicit_date_and_history_retention(compared):
    raw = json.dumps(compared[2])
    validate_daily_input(
        asof=DAY.isoformat(), preserve_existing_generations="true", edinet_metrics_acceptance=raw
    )
    for asof, preserve in (("", "true"), (DAY.isoformat(), "false")):
        with pytest.raises(WorkflowInputError):
            validate_daily_input(
                asof=asof, preserve_existing_generations=preserve, edinet_metrics_acceptance=raw
            )
    validate_daily_input(asof="", preserve_existing_generations="")


def test_new_selection_clock_is_not_a_different_original_pair(compared):
    store, _, _, report = compared
    fixed = read_edinet_input(store, InputSnapshot.model_validate(report["edinet_input"]))
    replacement = retain_edinet_input(
        store, fixed.lists, fixed.archives, selected_at=AT + timedelta(seconds=1)
    )
    assert replacement.sha256 != fixed.identity.sha256
    report["edinet_input"] = replacement.model_dump(mode="json")
    assert verify(compared)["status"] == "accepted"


def test_acceptance_validates_with_only_the_system_standard_library(compared):
    source = Path(__file__).resolve().parents[3] / "jobs/src"
    completed = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            (
                "import sys; sys.path.insert(0, sys.argv[1]); "
                "from baibai_jobs.integration.workflow_inputs import main; "
                "raise SystemExit(main(sys.argv[2:]))"
            ),
            str(source),
            "daily",
            "--asof",
            DAY.isoformat(),
            "--preserve-existing-generations",
            "true",
            "--edinet-metrics-acceptance",
            json.dumps(compared[2]),
        ],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
