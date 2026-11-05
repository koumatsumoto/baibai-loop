"""明示MCP原本から通常native統合とL3引用まで。実HTTP/本番storeは使わない。"""

import json
import sqlite3
from contextlib import closing
from datetime import timedelta

import pytest
from tests.helpers.r2_store import MemoryR2Store
from tests.helpers.tradingview_market import CLOCK, capture

import baibai_jobs.acquisition.market_context as evidence
from baibai_engine.data_collection.providers.tradingview.market_models import MARKET_GROUP
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_engine.investment_judgment.market_assessment.models import AssessmentInput
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.economic_staging import save_handoff
from baibai_jobs.acquisition.receipts import available_handoffs, seal_integrated
from baibai_jobs.integration.native_source_merge import verify_stores
from baibai_jobs.integration.sources import apply


def packet(tmp_path, *, label="first", received=CLOCK):
    value = capture(receipt_id=label, received=received)
    path = tmp_path / f"{label}.json"
    path.write_text(value.model_dump_json(by_alias=True))
    directory = tmp_path / label
    report = evidence.prepare([path], directory)
    return value, directory, report


def test_prepare_and_read_are_local_and_match_assessment_input(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence, "Boto3R2Store", lambda **kwargs: pytest.fail("unexpected R2"))
    value, directory, report = packet(tmp_path)
    before = (directory / "receipts.sqlite").read_bytes()
    assert report["market_receipts"] == [{"receipt_id": value.receipt_id, "sha256": value.digest()}]
    output = evidence.read_exact(directory / "receipts.sqlite", value.receipt_id, value.digest())
    assert output["assessment_input"] is None
    assert output["retention"] == {"status": "local_only"}
    assert output["observation"]["formal_proposal_price"] is False
    assert (directory / "receipts.sqlite").read_bytes() == before
    with pytest.raises(FileExistsError):
        evidence.prepare([tmp_path / "first.json"], directory)
    assert (directory / "receipts.sqlite").read_bytes() == before


def test_stage_download_exact_manifest_and_read_without_refetch(tmp_path, monkeypatch, capsys):
    value, directory, _ = packet(tmp_path)
    store = MemoryR2Store()
    monkeypatch.setattr(evidence, "Boto3R2Store", lambda **kwargs: store)
    stage_id = MARKET_GROUP + "-first"
    assert evidence.main(["stage", "--directory", str(directory), "--stage-id", stage_id]) == 0
    staged = json.loads(capsys.readouterr().out)
    output = evidence.read_exact(directory / "receipts.sqlite", value.receipt_id, value.digest())
    ref = AssessmentInput.model_validate(output["assessment_input"])
    assert ref.layer == "L1"
    assert ref.observation_as_of is None
    assert ref.available_at == value.received_at_utc
    assert stage_id in ref.reference
    assert staged["manifest_sha256"] in ref.reference
    assert value.digest() in ref.reference
    before = dict(store.values)
    assert evidence.main(["stage", "--directory", str(directory), "--stage-id", stage_id]) == 0
    capsys.readouterr()
    assert store.values == before
    restored = tmp_path / "restored"
    with pytest.raises(ValueError, match="manifest differs"):
        evidence.main(
            [
                "download",
                "--directory",
                str(restored),
                "--stage-id",
                stage_id,
                "--manifest-sha256",
                "0" * 64,
            ]
        )
    assert not restored.exists()
    assert (
        evidence.main(
            [
                "download",
                "--directory",
                str(restored),
                "--stage-id",
                stage_id,
                "--manifest-sha256",
                staged["manifest_sha256"],
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert evidence.read_exact(
        restored / "receipts.sqlite", value.receipt_id, value.digest()
    ) == evidence.read_exact(directory / "receipts.sqlite", value.receipt_id, value.digest())
    assert store.values == before


def test_common_integration_preserves_exact_market_receipts_without_schema_migration(
    tmp_path, monkeypatch
):
    value, directory, _ = packet(tmp_path)
    store = MemoryR2Store()
    save_handoff(store, MARKET_GROUP + "-first", directory)
    entries = available_handoffs(store)
    assert len(entries) == 1
    market, economic = tmp_path / "market.sqlite", tmp_path / "economic.sqlite"
    with closing(open_connection(market)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 49
    with closing(initialize_database(economic)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 13
    market_before = market.read_bytes()
    work = tmp_path / "work"
    work.mkdir()
    (work / "plan.json").write_text(json.dumps({"handoffs": entries}))
    monkeypatch.setattr("baibai_jobs.integration.sources.extract_saved_edinet", lambda *a, **k: {})
    result = apply(store, work, market=market, economic=economic, asof=CLOCK.date())
    assert result["handoffs"][0]["applied"] is True
    assert result["handoffs"][0]["failed"] is False
    assert result["handoffs"][0]["result"]["accepted_receipts"] == 1
    assert (
        evidence.read_exact(economic, value.receipt_id, value.digest())["observation"][
            "capture_sha256"
        ]
        == value.digest()
    )
    verify_stores(work / "receipt-00000" / "accepted.sqlite", economic)
    assert market.read_bytes() == market_before


def test_later_independent_capture_does_not_block_older_unique_receipt(tmp_path):
    _, newer, _ = packet(tmp_path, label="newer", received=CLOCK + timedelta(hours=1))
    _, older, _ = packet(tmp_path, label="older")
    store = MemoryR2Store()
    save_handoff(store, MARKET_GROUP + "-newer", newer)
    entry = available_handoffs(store)[0]
    seal_integrated(store, entry, generations={"synthetic": "generation"}, recovery=False)
    save_handoff(store, MARKET_GROUP + "-older", older)
    pending = available_handoffs(store)
    assert len(pending) == 1
    assert pending[0]["stage_id"] == MARKET_GROUP + "-older"
    assert pending[0]["late_before_integrated"] is False


def test_packet_rejects_unrelated_rows_before_private_upload(tmp_path, monkeypatch):
    _, directory, _ = packet(tmp_path)
    with closing(sqlite3.connect(directory / "receipts.sqlite")) as conn, conn:
        conn.execute("UPDATE economic_source_documents SET source_id='unrelated'")
    store = MemoryR2Store()
    monkeypatch.setattr(evidence, "Boto3R2Store", lambda **kwargs: store)
    with pytest.raises(ValueError, match="unrelated documents"):
        evidence.main(["stage", "--directory", str(directory), "--stage-id", MARKET_GROUP + "-bad"])
    assert store.values == {}


def test_capture_count_bound_does_not_create_partial_directory(tmp_path):
    target = tmp_path / "empty"
    with pytest.raises(ValueError, match="one to three"):
        evidence.prepare([], target)
    assert not target.exists()


def test_receipt_id_reuse_in_another_packet_cannot_ambiguate_canonical(tmp_path):
    from baibai_engine.data_collection.providers.tradingview.market_receipts import (
        retain_market_capture,
    )

    old = capture(receipt_id="reused")
    later, directory, _ = packet(tmp_path, label="reused", received=CLOCK + timedelta(hours=1))
    canonical = tmp_path / "canonical.sqlite"
    with closing(initialize_database(canonical)) as conn:
        retain_market_capture(conn, old)
    before = canonical.read_bytes()
    with pytest.raises(ValueError, match="digest differs"):
        evidence.validate_packet(directory, canonical=canonical)
    assert canonical.read_bytes() == before
    assert old.digest() != later.digest()


def test_modified_local_copy_cannot_supply_retained_assessment_input(tmp_path, monkeypatch, capsys):
    value, directory, _ = packet(tmp_path)
    store = MemoryR2Store()
    monkeypatch.setattr(evidence, "Boto3R2Store", lambda **kwargs: store)
    evidence.main(["stage", "--directory", str(directory), "--stage-id", MARKET_GROUP + "-frozen"])
    capsys.readouterr()
    with closing(sqlite3.connect(directory / "receipts.sqlite")) as conn, conn:
        conn.execute("PRAGMA user_version=13")
        conn.execute("CREATE TABLE unrelated(note TEXT)")
    with pytest.raises(ValueError, match="differs from retained"):
        evidence.read_exact(directory / "receipts.sqlite", value.receipt_id, value.digest())
