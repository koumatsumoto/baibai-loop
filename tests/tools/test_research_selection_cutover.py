"""application22原本を保持し、必要な現行評価と全取引事実だけを移す。"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from tests.helpers.db_seed import seed_ledger
from tests.helpers.ledger import load_portfolio_ledger
from tests.helpers.research_v4 import pair_payload
from tools.cutover.research_selection_v2 import CutoverSelection, cutover, main

from baibai_engine.research.capital_allocation import (
    CapitalAllocationAssessment,
    capital_allocation_draft_sha256,
)
from baibai_engine.research.thesis_store import ThesisStoreService
from baibai_engine.storage.application.read import connect_read_only

NOW = datetime.fromisoformat("2026-09-07T18:00:00+09:00")
EMPTY = CutoverSelection(keep_thesis_ids=(), keep_caa_ids=(), keep_position_review_ids=())


def source22(tmp_path):
    native = tmp_path / "native.sqlite"
    ledger = load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
    ledger = ledger.model_copy(
        update={
            "market_prices": tuple(
                price.model_copy(
                    update={
                        "source_kind": "market_api",
                        "price_basis": "unadjusted_close",
                        "source_ref": "synthetic-source",
                    }
                )
                for price in ledger.market_prices
            )
        }
    )
    seed_ledger(native, ledger)
    thesis, review = pair_payload()
    ThesisStoreService(native, clock=lambda: NOW).publish_reviewed_thesis(
        "synthetic-thesis", thesis, review
    )
    source = tmp_path / "source22.sqlite"
    with sqlite3.connect(source) as db:
        db.executescript(
            Path("tests/fixtures/application/research-selection-v1-schema.sql").read_text()
        )
        with sqlite3.connect(native) as reader:
            reader.row_factory = sqlite3.Row
            for table in (
                "ledger_event",
                "ledger_meta",
                "ledger_market_price",
                "thesis",
                "thesis_review",
            ):
                for row in reader.execute(f'SELECT * FROM "{table}"'):  # nosec B608
                    values = dict(row)
                    db.execute(
                        f'INSERT INTO "{table}" VALUES ({",".join("?" for _ in values)})',
                        tuple(values.values()),
                    )  # nosec B608
    return source


def test_source_is_unchanged_output23_has_no_old_runtime_and_same_ledger(tmp_path):
    source = source22(tmp_path)
    before = source.read_bytes()
    output = tmp_path / "output23.sqlite"
    kept = EMPTY.model_copy(update={"keep_thesis_ids": ("synthetic-thesis",)})
    check = cutover(source=source, output=output, selection=kept, check=True)
    assert check.blocked == ()
    assert not output.exists()
    report = cutover(source=source, output=output, selection=kept)
    assert report.output_version == 23
    assert source.read_bytes() == before
    with connect_read_only(output) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT count(*) FROM operation_session").fetchone()[0] == 0
        assert (
            db.execute(
                "SELECT count(*) FROM sqlite_schema WHERE name='research_triage'"
            ).fetchone()[0]
            == 0
        )
        with sqlite3.connect(source) as old:
            for table in (
                "ledger_event",
                "ledger_meta",
                "ledger_market_price",
                "thesis",
                "thesis_review",
            ):
                assert [
                    tuple(row) for row in db.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
                ] == old.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()  # nosec B608
    with pytest.raises(RuntimeError, match="obsolete"):
        connect_read_only(source)
    with pytest.raises(ValueError, match="absent output"):
        cutover(source=source, output=output, selection=kept)


def test_current_caa_retains_opaque_triage_reference_and_exact_review_hash(tmp_path):
    from tests.engine.test_capital_allocation import _payload

    source = source22(tmp_path)
    raw = _payload()
    raw["alternatives"][0].update(thesis_id="synthetic-thesis")
    with sqlite3.connect(source) as db:
        raw["alternatives"][0]["thesis_core_sha256"] = db.execute(
            "SELECT core_sha256 FROM thesis"
        ).fetchone()[0]
        raw["alternatives"][0]["thesis_review_id"] = db.execute(
            "SELECT review_id FROM thesis_review"
        ).fetchone()[0]
        raw.update(result="no_allocation")
        raw["alternatives"][0]["disposition"] = "decline"
        value = CapitalAllocationAssessment.model_validate(raw)
        value = value.model_copy(
            update={
                "review": value.review.model_copy(
                    update={"draft_sha256": capital_allocation_draft_sha256(value)}
                )
            }
        )
        payload = json.dumps(value.payload())
        db.execute(
            "INSERT INTO research_triage VALUES ('research-triage-test','old-set','old-run','2026-08-30','2026-08-30T12:00:00+00:00','{}')"
        )
        db.execute(
            "INSERT INTO capital_allocation_assessment VALUES (?,?,?,?,?,?)",
            (
                value.capital_allocation_assessment_id,
                value.as_of.isoformat(),
                value.published_at.isoformat(),
                value.result,
                value.research_triage_id,
                payload,
            ),
        )
    output = tmp_path / "output"
    report = cutover(
        source=source,
        output=output,
        selection=EMPTY.model_copy(
            update={"keep_caa_ids": (value.capital_allocation_assessment_id,)}
        ),
    )
    assert not report.blocked
    with connect_read_only(output) as db:
        assert (
            db.execute("SELECT payload FROM capital_allocation_assessment").fetchone()[0] == payload
        )
        assert db.execute("SELECT count(*) FROM thesis").fetchone()[0] == 1
        assert (
            db.execute("SELECT research_triage_id FROM capital_allocation_assessment").fetchone()[0]
            == "research-triage-test"
        )


def test_missing_closure_stops_all_writes_and_check_emits_json(tmp_path, capsys):
    source = source22(tmp_path)
    before = source.read_bytes()
    output = tmp_path / "output"
    keep = EMPTY.model_copy(update={"keep_thesis_ids": ("missing",)})
    manifest = tmp_path / "keep.json"
    manifest.write_text(keep.model_dump_json())
    assert (
        main(
            [
                "--source",
                str(source),
                "--output",
                str(output),
                "--selection",
                str(manifest),
                "--check",
            ]
        )
        == 3
    )
    assert "missing thesis" in json.loads(capsys.readouterr().out)["blocked"][0]
    assert not output.exists()
    report = cutover(source=source, output=output, selection=keep)
    assert report.blocked
    assert source.read_bytes() == before
    assert not output.exists()
    with pytest.raises(ValueError, match="source must differ"):
        cutover(source=source, output=source, selection=EMPTY)


def test_source_change_after_copy_refuses_completed_output(tmp_path, monkeypatch):
    from tools.cutover import research_selection_v2 as tool

    source = source22(tmp_path)
    original = tool._snapshot
    calls = 0

    def changed(reader):
        nonlocal calls
        calls += 1
        snapshot = original(reader)
        if calls == 2:
            snapshot["ledger_event"][0]["payload"] = "synthetic concurrent change"
        return snapshot

    monkeypatch.setattr(tool, "_snapshot", changed)
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="source changed"):
        cutover(source=source, output=output, selection=EMPTY)
    assert not output.exists()


def active_source(source):
    payload = {
        "checkpoint": "synthetic active draft",
        "artifacts": [
            {
                "kind": "research_selection",
                "request": "synthetic original request",
                "targets": [
                    {"ticker": "1234", "purpose": "new"},
                    {"ticker": "2331", "purpose": "holding"},
                ],
            }
        ],
    }
    with sqlite3.connect(source) as db:
        db.execute(
            "INSERT INTO operation_session VALUES (?,?,?,?,?,?,?,?)",
            (
                "op-20260907-capital-allocation-1",
                "capital-allocation",
                "active",
                "2026-09-07",
                None,
                NOW.isoformat(),
                None,
                json.dumps(payload),
            ),
        )
    return "op-20260907-capital-allocation-1"


def test_active_workspace_handoff_is_confined_and_non_destructive(tmp_path):
    import yaml

    source = source22(tmp_path)
    identifier = active_source(source)
    root = tmp_path / "workspace-root"
    workspace = root / "active"
    workspace.mkdir(parents=True)
    (workspace / "manifest.yaml").write_text(
        yaml.safe_dump({"operation_id": identifier, "as_of": "2026-09-07"})
    )
    (workspace / "1234").mkdir()
    authored = workspace / "1234/thesis-draft.yaml"
    authored.write_text("synthetic authored draft\n")
    foreign = root / "other"
    foreign.mkdir()
    (foreign / "manifest.yaml").write_text(yaml.safe_dump({"operation_id": "op-other"}))
    (foreign / "secret-draft.yaml").write_text("not part of this handoff")
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    db_before = source.read_bytes()
    output = tmp_path / "output"
    report = cutover(source=source, output=output, selection=EMPTY, workspace_root=root, check=True)
    assert report.active_handoffs == 1
    assert report.blocked == ()
    assert not output.exists()
    result = cutover(source=source, output=output, selection=EMPTY, workspace_root=root)
    assert result.blocked == ()
    handoff = output.parent / (output.name + ".handoff")
    saved = json.loads((handoff / "handoff.json").read_text())[0]
    assert saved["source_operation_id"] == identifier
    assert [request["scope"] for request in saved["requests"]] == [
        None,
        "review_positions",
    ]
    assert saved["requires_reselection"] is True
    assert (
        handoff / (identifier + "-1") / "1234/thesis-draft.yaml"
    ).read_bytes() == authored.read_bytes()
    assert not list(handoff.rglob("secret-draft.yaml"))
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    assert source.read_bytes() == db_before
    with connect_read_only(output) as db:
        assert db.execute("SELECT count(*) FROM operation_session").fetchone()[0] == 0


@pytest.mark.parametrize(
    "case", ["missing_root", "different_id", "outside_symlink", "nested_other"]
)
def test_active_handoff_missing_or_conflicting_binding_stops_copy(tmp_path, case):
    import yaml

    source = source22(tmp_path)
    identifier = active_source(source)
    root = tmp_path / "root"
    root.mkdir()
    workspace = root / "active"
    workspace.mkdir()
    (workspace / "manifest.yaml").write_text(
        yaml.safe_dump({"operation_id": identifier if case != "different_id" else "op-other"})
    )
    if case == "outside_symlink":
        outside = tmp_path / "outside"
        outside.write_text("outside root")
        (workspace / "draft.yaml").symlink_to(outside)
    if case == "nested_other":
        child = workspace / "child"
        child.mkdir()
        (child / "manifest.yaml").write_text(yaml.safe_dump({"operation_id": "op-other"}))
    output = tmp_path / "output"
    before = source.read_bytes()
    report = cutover(
        source=source,
        output=output,
        selection=EMPTY,
        workspace_root=None if case == "missing_root" else root,
    )
    assert report.blocked
    assert not output.exists()
    assert source.read_bytes() == before


def test_old_schema22_reader_rejects_the_new_generation(tmp_path, monkeypatch):
    import baibai_engine.storage.application.read as reader

    source = source22(tmp_path)
    output = tmp_path / "output"
    assert not cutover(source=source, output=output, selection=EMPTY).blocked
    monkeypatch.setattr(reader, "APPLICATION_SCHEMA_VERSION", 22)
    with pytest.raises(RuntimeError, match="found user_version=23, expected=22"):
        reader.connect_read_only(output)
