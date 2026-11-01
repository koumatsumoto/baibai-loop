import json
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from tests.helpers.r2_store import MemoryR2Store

import baibai_engine.data_collection.acquisition.refresh as acquisition
from baibai_engine.data_collection.receipts.native_sources.handoff import export_receipts
from baibai_engine.data_collection.receipts.native_sources.records import (
    read_snapshots,
    store_snapshot,
)
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_engine.storage.objects.r2 import (
    LakePublishError,
)
from baibai_engine.storage.sqlite.merge import MergeError
from baibai_jobs.acquisition.economic_staging import load_handoff, save_handoff
from baibai_jobs.integration.economic_handoff import acquire, receive

DAY = date(2026, 10, 20)
WHEN = datetime(2026, 10, 20, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


def snapshot(path, source, value, when=WHEN):
    with closing(initialize_database(path)) as conn, conn:
        store_snapshot(
            conn, source_id=source, observed_at=DAY, fetched_at=when, records=[{"value": value}]
        )


def handoff(tmp_path, group, source, value):
    database = tmp_path / f"{group}.sqlite"
    snapshot(database, source, value)
    directory = tmp_path / group
    directory.mkdir()
    export_receipts(database, directory / "receipts.sqlite", source_ids=[source], since=WHEN)
    (directory / "result.json").write_text(
        json.dumps({"group": group, "sources": [{"source_id": source, "status": "ok"}]})
    )
    return directory


def test_independent_handoffs_union_into_fresh_canonical_without_reverting_another_source(tmp_path):
    a = handoff(tmp_path, "fred_csv", "fred-payems", "A")
    b = handoff(tmp_path, "mof_jgb", "mof-jgbcm-all", "B")
    canonical = tmp_path / "canonical.sqlite"
    snapshot(canonical, "fed-h15", "new unrelated canonical receipt", WHEN + timedelta(hours=1))
    receive(b, canonical, "mof_jgb", "mof-jgbcm-all", verify=False)
    receive(a, canonical, "fred_csv", "fred-payems", verify=False)
    receive(a, canonical, "fred_csv", "fred-payems", verify=True)
    with closing(initialize_database(canonical)) as conn:
        assert {
            r[0] for r in conn.execute("SELECT DISTINCT source_id FROM economic_source_records")
        } == {"fred-payems", "mof-jgbcm-all", "fed-h15"}


def test_readback_missing_a_received_row_is_a_failure(tmp_path):
    a = handoff(tmp_path, "fred_csv", "fred-payems", "A")
    canonical = tmp_path / "canonical.sqlite"
    with closing(initialize_database(canonical)):
        pass
    before = canonical.read_bytes()
    for _ in range(2):
        with pytest.raises(MergeError, match="readback"):
            receive(a, canonical, "fred_csv", "fred-payems", verify=True)
        assert canonical.read_bytes() == before


def test_handoff_contains_only_new_selected_receipts_and_preserves_a_b_a(tmp_path):
    source = tmp_path / "source.sqlite"
    snapshot(source, "fed-h15", "A")
    snapshot(source, "fed-h15", "A", WHEN + timedelta(hours=1))
    snapshot(source, "fed-h15", "B", WHEN + timedelta(hours=2))
    snapshot(source, "fed-h15", "A", WHEN + timedelta(hours=3))
    snapshot(source, "fred-payems", "unrelated source")
    artifact = tmp_path / "received.sqlite"
    export_receipts(source, artifact, source_ids=["fed-h15"], since=WHEN + timedelta(hours=1))
    with closing(initialize_database(artifact)) as conn:
        rows = tuple(read_snapshots(conn, DAY, DAY, source_id="fed-h15"))
        # read_snapshots selects the latest; the receipt history still holds B then A.
        assert rows[0].records == ({"value": "A"},)
        assert conn.execute("SELECT COUNT(*) FROM economic_source_records").fetchone()[0] == 2
        assert {
            r[0] for r in conn.execute("SELECT DISTINCT source_id FROM economic_source_records")
        } == {"fed-h15"}


def test_failed_source_keeps_prior_data_and_does_not_hide_sibling_success(tmp_path, monkeypatch):
    database = tmp_path / "source.sqlite"
    snapshot(database, "boj-cai", "retained original")

    def fetch(source, **kwargs):
        if source.source_id == "boj-cai":
            raise RuntimeError("incomplete provider response")
        return {DAY: [{"value": "new"}]}

    monkeypatch.setattr(acquisition, "fetch_native_records", fetch)
    directory = tmp_path / "stage"
    assert acquire("boj", "", DAY, directory, database) == 1
    report = json.loads((directory / "result.json").read_text())
    assert {row["status"] for row in report["sources"]} == {"ok", "failed"}
    with closing(initialize_database(database)) as conn:
        assert (
            next(read_snapshots(conn, DAY, DAY, source_id="boj-cai")).records[0]["value"]
            == "retained original"
        )
    with closing(initialize_database(directory / "receipts.sqlite")) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM economic_source_records WHERE source_id='boj-cai'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT status FROM economic_source_runs WHERE source_id='boj-cai'"
            ).fetchone()[0]
            == "failed"
        )


def workflow(name):
    return yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)


def test_each_provider_saves_receipts_without_full_db_or_integration_wait():
    parent = workflow("cloud-economic-sources.yml")
    assert parent["jobs"]["source"]["strategy"]["fail-fast"] == "false"
    source = workflow("cloud-economic-source.yml")
    assert source["concurrency"]["group"] == "economic-source-${{ inputs.group }}"
    assert "publish" not in source["jobs"]
    text = str(source)
    for forbidden in (
        "cloud-publish",
        "pull-indicators",
        "hydrate-market",
        "pull-app",
        "push-machine",
        "publication_lease",
    ):
        assert forbidden not in text
    assert "integration.economic_handoff input --group" in text
    assert "baibai-jobs model-integration" in str(workflow("cloud-daily-batch.yml"))
    assert parent["jobs"]["source"]["with"]["current_only_now"] == (
        "${{ github.event_name == 'schedule' || inputs.asof == '' }}"
    )
    assert source["on"]["workflow_call"]["inputs"]["current_only_now"]["default"] == "false"
    acquire_step = next(
        step for step in source["jobs"]["acquire"]["steps"] if step.get("id") == "acquire"
    )
    assert acquire_step["env"]["CURRENT_ONLY_NOW"] == "${{ inputs.current_only_now }}"
    assert "--current-only-now" in acquire_step["run"]


@pytest.mark.parametrize("failure", [403, 429, 500, "empty", "parse", "message_only"])
def test_pmi_notice_retains_failure_and_only_identifies_actual_http_403(
    tmp_path, monkeypatch, failure
):
    import requests

    import baibai_engine.data_collection.providers.spglobal.pmi as pmi
    from baibai_engine.data_collection.transport.context import (
        SourceWithheldError,
        _raise_for_response,
    )

    database = tmp_path / "source.sqlite"
    with closing(initialize_database(database)):
        pass
    sources = [s for s in acquisition.load_sources().values() if s.provider == "spglobal_pmi"]
    release = pmi.Release(date(2026, 9, 1), date(2026, 9, 1), "https://example.com/pmi.pdf")
    monkeypatch.setattr(
        pmi, "load_manifest", lambda _: {s.provider_series_id: (release,) for s in sources}
    )
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        if failure in {"empty", "message_only"}:
            raise SourceWithheldError("HTTP 403" if failure == "message_only" else "empty response")
        if failure == "parse":
            raise ValueError("bad PDF")
        response = requests.Response()
        response.status_code = failure
        response.url = url
        _raise_for_response(response, url)

    monkeypatch.setattr(pmi, "fetch_pdf_bytes", fetch)
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    directory = tmp_path / "stage"
    assert acquire("spglobal_pmi", "", DAY, directory, database) == 1
    report = json.loads((directory / "result.json").read_text())
    assert all(row["status"] == "failed" for row in report["sources"])
    assert f"expected_unavailable={'true' if failure == 403 else 'false'}" in output.read_text()
    if failure == 403:
        assert len(calls) == 1
        assert all(row["http_status"] == 403 for row in report["sources"])
    with closing(initialize_database(directory / "receipts.sqlite")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM economic_source_documents").fetchone()[0] == 0
        assert (
            conn.execute("SELECT status FROM economic_source_runs LIMIT 1").fetchone()[0]
            == "failed"
        )


def test_economic_workflow_passes_notice_classification_without_hiding_job_failure():
    source = workflow("cloud-economic-source.yml")
    assert (
        source["jobs"]["acquire"]["outputs"]["expected_unavailable"]
        == "${{ steps.acquire.outputs.expected_unavailable }}"
    )
    steps = source["jobs"]["acquire"]["steps"]
    assert "continue-on-error" not in next(step for step in steps if step.get("id") == "acquire")
    notice = source["jobs"]["notify"]["steps"][-1]
    assert (
        notice["env"]["EXPECTED_UNAVAILABLE"] == "${{ needs.acquire.outputs.expected_unavailable }}"
    )


def test_receipts_cross_only_private_r2_with_content_verification(tmp_path):
    directory = handoff(tmp_path, "fred_csv", "fred-payems", "A")
    store = MemoryR2Store()
    stage = save_handoff(store, "42-1-fred_csv", directory)
    downloaded = tmp_path / "downloaded"
    load_handoff(store, stage, downloaded)
    assert (downloaded / "receipts.sqlite").read_bytes() == (
        directory / "receipts.sqlite"
    ).read_bytes()
    assert all(key.startswith("coordination/economic-sources/") for key in store.put_keys)
    assert not any(
        "artifact" in str(workflow(name))
        for name in ("cloud-economic-source.yml", "cloud-economic-sources.yml")
    )
    assert save_handoff(store, "42-1-fred_csv", directory) == stage
    key = next(key for key in store.values if key.endswith(".sqlite"))
    store.values[key].body = b"tampered"
    with pytest.raises(LakePublishError, match="receipt"):
        load_handoff(store, stage, tmp_path / "corrupt")


def test_common_integration_is_independently_scheduled_and_includes_weekends():
    parent = workflow("cloud-economic-sources.yml")
    assert "materialize" not in parent["jobs"]
    common = workflow("cloud-daily-batch.yml")
    assert common["on"]["schedule"] == [{"cron": "43 13 * * *"}]
    assert "needs" not in common["jobs"]["daily"]
    runs = [str(step.get("run", "")) for step in common["jobs"]["daily"]["steps"]]
    assert sum("r2_transfer.sh hydrate-market" in run for run in runs) == 1
    assert sum("baibai-jobs model-integration" in run for run in runs) == 1
    assert not any("r2_transfer.sh publish-lake" in run for run in runs)
    assert any("integration.sources plan" in run for run in runs)


def test_acquisition_deadline_leaves_time_to_stage_confirmed_sibling_receipts(
    tmp_path, monkeypatch
):
    import baibai_engine.data_collection.transport.context as base
    from baibai_engine.data_collection.transport.http import AcquisitionDeadlineExceeded

    expired = False

    def check(waiting=0):
        if expired:
            raise AcquisitionDeadlineExceeded("acquisition job deadline exhausted")

    def fetch(source, **kwargs):
        nonlocal expired
        expired = True
        return {DAY: [{"value": "confirmed before cutoff"}]}

    monkeypatch.setattr(base, "check_acquisition_deadline", check)
    monkeypatch.setattr(acquisition, "fetch_native_records", fetch)
    database = tmp_path / "source.sqlite"
    with closing(initialize_database(database)):
        pass
    directory = tmp_path / "stage"
    assert acquire("boj", "", DAY, directory, database) == 1
    report = json.loads((directory / "result.json").read_text())
    assert sum(row["status"] == "ok" for row in report["sources"]) == 1
    assert any(row["status"] == "failed" for row in report["sources"])
    store = MemoryR2Store()
    save_handoff(store, "deadline-boj", directory)
    assert any(key.endswith("handoff.json") for key in store.put_keys)
