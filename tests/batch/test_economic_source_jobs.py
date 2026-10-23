import json
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from baibai_batch.jobs.economic import acquire, receive
from baibai_engine.collection.economic import acquisition
from baibai_engine.collection.economic.handoff import export_receipts
from baibai_engine.collection.economic.records import read_snapshots, store_snapshot
from baibai_engine.collection.economic.store import initialize_database

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
    with pytest.raises(ValueError, match="readback"):
        receive(a, canonical, "fred_csv", "fred-payems", verify=True)


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
            tuple(read_snapshots(conn, DAY, DAY, source_id="boj-cai"))[0].records[0]["value"]
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


def test_each_provider_can_publish_without_waiting_for_the_acquisition_matrix():
    parent = workflow("cloud-economic-sources.yml")
    assert parent["on"]["schedule"] == [{"cron": "23 8 * * *"}]
    assert parent["jobs"]["source"]["uses"].endswith("/cloud-economic-source.yml")
    assert parent["jobs"]["source"]["strategy"]["fail-fast"] == "false"
    source = workflow("cloud-economic-source.yml")
    assert source["concurrency"]["group"] == "economic-source-${{ inputs.group }}"
    assert "cloud-publish" not in str(source["jobs"]["acquire"])
    publisher = source["jobs"]["publish"]
    assert publisher["needs"] == "acquire"
    assert publisher["concurrency"]["group"] == "cloud-publish"
    run = next(s["run"] for s in publisher["steps"] if s.get("id") == "publish")
    assert run.index("pull-indicators") < run.index("economic merge") < run.index("push-indicators")
    assert run.rindex("pull-indicators") < run.index("economic verify")
    assert "hydrate-market" not in str(source)
    assert "pull-app" not in str(source)
    assert "push-machine" not in str(source)
    assert "--saved-economic" in str(workflow("cloud-daily-batch.yml"))
