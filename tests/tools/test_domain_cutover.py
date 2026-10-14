"""One source22→final copy preserves all facts and extracts unapproved old materials."""

import hashlib
import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from tests.helpers.assessment_v1 import APPENDIX_ASSESSMENT_PUBLICATION
from tests.helpers.db_seed import seed_ledger
from tests.helpers.ledger import load_portfolio_ledger
from tools.cutover import domain_v1 as tool
from tools.cutover.assessment_proposal_v1 import (
    _legacy_thesis_core_hash,
    _LegacyThesisDocument,
)
from tools.cutover.domain_v1 import KeepManifest, RequiredRunSource, cutover, main

from baibai_engine.position.decision_references import resolve_buy_reference
from baibai_engine.position.ledger import replay_events_through
from baibai_engine.position.ledger_read import load_ledger_document
from baibai_engine.research.assessment import StockAssessmentDocument
from baibai_engine.screening.run_store.schema import RUN_STORE_SCHEMA_VERSION
from baibai_engine.storage.application.json import canonical_json
from baibai_engine.storage.application.read import connect_read_only
from baibai_engine.storage.application.schema import APPLICATION_SCHEMA_VERSION

OLD_ID = "synthetic-old-assessment"
CAA_ID = "capital-allocation-assessment-20261014-synthetic"
EMPTY = KeepManifest()


def old_pair():
    publication = deepcopy(APPENDIX_ASSESSMENT_PUBLICATION)
    document, review = publication["document"], publication["review"]
    conclusion = document.pop("conclusion")
    document.pop("kind")
    document.pop("findings")
    document.pop("refresh")
    document["schema_version"] = 4
    document["judgment"] = {
        "disposition": "candidate",
        "proposed_at": conclusion["assessed_at"],
        "strongest_countercase": conclusion["strongest_countercase"],
    }
    review.pop("review_scope")
    review["reviewed_thesis_sha256"] = _legacy_thesis_core_hash(
        _LegacyThesisDocument.model_validate(document)
    )
    review.pop("reviewed_assessment_sha256")
    return document, review


def source22(tmp_path):
    native = tmp_path / "native-ledger.sqlite"
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
    source = tmp_path / "source22.sqlite"
    document, review = old_pair()
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.executescript(
            Path("tests/fixtures/application/research-selection-v1-schema.sql").read_text()
        )
        with closing(sqlite3.connect(native)) as reader:
            reader.row_factory = sqlite3.Row
            for table in tool._UNCHANGED:
                for row in reader.execute(f'SELECT * FROM "{table}"'):  # nosec B608
                    values = dict(row)
                    connection.execute(
                        f'INSERT INTO "{table}" VALUES ({",".join("?" for _ in values)})',
                        tuple(values.values()),
                    )  # nosec B608
        connection.execute(
            "INSERT INTO thesis VALUES (?,?,?,?,?,?,?,?)",
            (
                OLD_ID,
                "1234",
                "2026-10-14",
                "candidate",
                "2026-10-14T16:06:00+09:00",
                None,
                canonical_json(document),
                review["reviewed_thesis_sha256"],
            ),
        )
        connection.execute(
            "INSERT INTO thesis_review VALUES (?,?,?,?)",
            (review["review_id"], OLD_ID, review["reviewed_at"], canonical_json(review)),
        )
        connection.execute(
            "INSERT INTO task VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "synthetic-task",
                "open",
                "follow-up",
                "1234",
                "2026-10-15",
                None,
                "2026-10-14T12:00:00+09:00",
                None,
                '{"original":"preserve this exact JSON spacing"}',
            ),
        )
        connection.execute(
            "INSERT INTO market_context_assessment VALUES (?,?,?,?,?,?)",
            (
                "synthetic-context",
                4,
                "2026-10-14",
                "2026-10-14T12:00:00+09:00",
                None,
                '{"synthetic":"unchanged"}',
            ),
        )
        connection.execute(
            "INSERT INTO market_context_assessment_head VALUES (1,'synthetic-context')"
        )
        connection.execute(
            "INSERT INTO portfolio_outcome VALUES (?,?,?,?,?,?)",
            (
                "synthetic-outcome",
                "1y",
                "2025-10-14",
                "2026-10-14",
                "unresolved",
                '{"synthetic":"exact source outcome"}',
            ),
        )
    return source


def caa_payload(*, version=2, ticker="1234", allocate=True):
    raw = {
        "kind": "capital_allocation_assessment",
        "schema_version": version,
        "capital_allocation_assessment_id": CAA_ID,
        "as_of": "2026-10-14",
        "published_at": "2026-10-14T16:20:00+09:00",
        "result": "allocate" if allocate else "no_allocation",
        "headline": "架空の旧判断",
        "research_triage_id": "synthetic-triage",
        "comparison": "架空の比較",
        "forgone": "架空の見送り",
        "alternatives": [
            {
                "ticker": ticker,
                "thesis_id": OLD_ID,
                "thesis_core_sha256": "0" * 64,
                "thesis_review_id": "old-review",
                "disposition": "allocate" if allocate else "decline",
                "rationale": "架空の投資理由",
            }
        ],
        "review": {
            "attempt": 1,
            "reviewer_identity": "synthetic-independent",
            "reviewed_at": "2026-10-14T16:15:00+09:00",
            "conclusion": "pass",
            "draft_sha256": "0" * 64,
            "open_findings": [],
        },
    }
    raw["macro_context_id" if version == 1 else "market_context_assessment_id"] = None
    refresh_caa_hash(raw)
    return raw


def refresh_caa_hash(raw):
    draft = {key: value for key, value in raw.items() if key not in {"review", "published_at"}}
    raw["review"]["draft_sha256"] = hashlib.sha256(canonical_json(draft).encode()).hexdigest()


def insert_caa(source, raw=None):
    raw = raw or caa_payload()
    with sqlite3.connect(source) as connection:
        connection.execute(
            "INSERT INTO capital_allocation_assessment VALUES (?,?,?,?,?,?)",
            (
                raw["capital_allocation_assessment_id"],
                raw["as_of"],
                raw["published_at"],
                raw["result"],
                raw["research_triage_id"],
                canonical_json(raw),
            ),
        )
    return raw


def execute(
    tmp_path,
    source,
    *,
    manifest=EMPTY,
    check=False,
    source_runs=None,
    output_runs=None,
    workspace_root=None,
):
    return cutover(
        source_app=source,
        output_app=tmp_path / "final.sqlite",
        draft_dir=tmp_path / "drafts",
        manifest=manifest,
        check=check,
        source_runs=source_runs,
        output_runs=output_runs,
        workspace_root=workspace_root,
    )


def material_files(tmp_path):
    return [
        json.loads(path.read_text())
        for path in sorted((tmp_path / "drafts").glob("material-*.json"))
    ]


def test_source22_to_final24_preserves_every_fact_and_non_target_payload(tmp_path):
    source = source22(tmp_path)
    before = source.read_bytes()
    assert execute(tmp_path, source, check=True).blocked == ()
    assert not (tmp_path / "final.sqlite").exists()
    assert not (tmp_path / "drafts").exists()
    report = execute(tmp_path, source)
    assert report.source_version == 22
    assert report.output_version == APPLICATION_SCHEMA_VERSION == 24
    assert report.research_recreation_required is True
    assert source.read_bytes() == before
    with connect_read_only(tmp_path / "final.sqlite") as final, sqlite3.connect(source) as old:
        tables = {
            row[0] for row in final.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert set(tool._NEW_RESEARCH) <= tables
        assert not set(tool._OLD_RESEARCH[:-2]) & tables
        assert "research_triage" not in tables
        for table in tool._NEW_RESEARCH:
            assert final.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] == 0
        for table in tool._UNCHANGED:
            assert [
                tuple(row) for row in final.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
            ] == old.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
        assert final.execute("PRAGMA foreign_key_check").fetchall() == []
        assert final.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        old_document, final_document = load_ledger_document(old), load_ledger_document(final)
        assert final_document == old_document
        assert replay_events_through(
            final_document.events, final_document.as_of
        ) == replay_events_through(old_document.events, old_document.as_of)


def test_useful_old_assessment_is_material_only_with_original_source_times_and_hash(tmp_path):
    source = source22(tmp_path)
    report = execute(tmp_path, source, manifest=KeepManifest(keep_assessment_sources=(OLD_ID,)))
    assert report.blocked == ()
    material = material_files(tmp_path)[0]
    old_document, old_review = old_pair()
    assert material["source_document"] == old_document
    assert material["source_core_sha256"] == old_review["reviewed_thesis_sha256"]
    draft = material["assessment_draft"]
    assert draft["input_snapshot"]["sources"] == old_document["input_snapshot"]["sources"]
    assert draft["input_snapshot"]["facts"] == old_document["input_snapshot"]["facts"]
    assert draft["findings"] is None
    assert draft["conclusion"]["assessed_at"] is None
    assert draft["refresh"] is None
    assert "review" not in draft
    assert material["new_review_required"] is True
    with pytest.raises(ValueError, match="validation errors"):
        StockAssessmentDocument.model_validate(draft)


@pytest.mark.parametrize("version", [1, 2])
def test_unreported_old_allocate_creates_only_minimal_buy_reference(tmp_path, version):
    source = source22(tmp_path)
    raw = insert_caa(source, caa_payload(version=version))
    report = execute(tmp_path, source, manifest=KeepManifest(retained_buy_references=(CAA_ID,)))
    assert report.blocked == ()
    assert report.retained_buy_references == (CAA_ID,)
    assert resolve_buy_reference(CAA_ID, db_path=tmp_path / "final.sqlite") == "1234"
    with connect_read_only(tmp_path / "final.sqlite") as final:
        binding = dict(final.execute("SELECT * FROM position_decision_reference").fetchone())
        assert binding == dict(
            reference=CAA_ID,
            ticker="1234",
            side="buy",
            origin="retained_reference",
            proposal_id=None,
            source_id=CAA_ID,
            source_digest=hashlib.sha256(canonical_json(raw).encode()).hexdigest(),
        )
        assert final.execute("SELECT count(*) FROM investment_proposal").fetchone()[0] == 0
        assert final.execute("SELECT count(*) FROM investment_proposal_item").fetchone()[0] == 0
    assert "limit_price_yen" not in binding
    assert "quantity" not in binding


def test_ledger_buy_reference_closure_does_not_invent_old_terms_from_fill(tmp_path):
    source = source22(tmp_path)
    insert_caa(source, caa_payload(ticker="2331"))
    with sqlite3.connect(source) as connection:
        rows = connection.execute(
            "SELECT append_seq,payload FROM ledger_event WHERE ticker='2331' AND event_type IN ('execution','reservation')"
        ).fetchall()
        for append_seq, payload in rows:
            raw = json.loads(payload)
            raw["decision_reference"] = CAA_ID
            connection.execute(
                "UPDATE ledger_event SET decision_reference=?,payload=? WHERE append_seq=?",
                (CAA_ID, canonical_json(raw), append_seq),
            )
    report = execute(tmp_path, source)
    assert report.blocked == ()
    assert report.retained_buy_references == (CAA_ID,)
    with connect_read_only(tmp_path / "final.sqlite") as final:
        assert final.execute(
            "SELECT count(*) FROM ledger_event WHERE decision_reference=?", (CAA_ID,)
        ).fetchone()[0] == len(rows)
        assert final.execute("SELECT count(*) FROM investment_proposal").fetchone()[0] == 0
    assert material_files(tmp_path) == []


def bind_ledger_reference(source, reference, *, ticker="2331"):
    with sqlite3.connect(source) as connection:
        rows = connection.execute(
            "SELECT append_seq,payload FROM ledger_event WHERE ticker=? AND event_type IN ('execution','reservation')",
            (ticker,),
        ).fetchall()
        for append_seq, payload in rows:
            raw = json.loads(payload)
            if raw.get("side", "buy") != "buy":
                continue
            raw["decision_reference"] = reference
            connection.execute(
                "UPDATE ledger_event SET decision_reference=?,payload=? WHERE append_seq=?",
                (reference, canonical_json(raw), append_seq),
            )


@pytest.mark.parametrize("caa_present", [False, True])
@pytest.mark.parametrize("missing_index_reference", [False, True])
def test_explicit_buy_ledger_identity_survives_missing_caa_or_late_review(
    tmp_path, caa_present, missing_index_reference
):
    source = source22(tmp_path)
    reference = CAA_ID if caa_present else "https://example.invalid/issues/413#comment-synthetic"
    if caa_present:
        raw = caa_payload(ticker="2331")
        raw["review"]["reviewed_at"] = "2026-10-14T16:22:16.617072+09:00"
        insert_caa(source, raw)
    bind_ledger_reference(source, reference)
    if missing_index_reference:
        with sqlite3.connect(source) as connection:
            connection.execute(
                "UPDATE ledger_event SET decision_reference=NULL WHERE decision_reference=?",
                (reference,),
            )
    before_bytes = source.read_bytes()
    with sqlite3.connect(source) as original:
        original.row_factory = sqlite3.Row
        original_rows = [
            dict(row) for row in original.execute("SELECT * FROM ledger_event ORDER BY append_seq")
        ]
        original_document = load_ledger_document(original)
    assert original_document is not None
    report = execute(tmp_path, source)
    assert report.blocked == ()
    assert report.retained_buy_references == (reference,)
    assert source.read_bytes() == before_bytes
    assert resolve_buy_reference(reference, db_path=tmp_path / "final.sqlite") == "2331"
    with connect_read_only(tmp_path / "final.sqlite") as final:
        copied_rows = [
            dict(row) for row in final.execute("SELECT * FROM ledger_event ORDER BY append_seq")
        ]
        assert copied_rows == original_rows
        binding = dict(final.execute("SELECT * FROM position_decision_reference").fetchone())
        evidence = [
            row
            for row in original_rows
            if json.loads(row["payload"]).get("decision_reference") == reference
        ]
        assert binding == dict(
            reference=reference,
            ticker="2331",
            side="buy",
            origin="retained_reference",
            proposal_id=None,
            source_id=f"ledger-event:{evidence[0]['event_id']}",
            source_digest=hashlib.sha256(canonical_json(evidence).encode()).hexdigest(),
        )
        assert final.execute("SELECT count(*) FROM investment_proposal").fetchone()[0] == 0
        copied_document = load_ledger_document(final)
    assert copied_document is not None
    assert copied_document == original_document
    assert replay_events_through(
        copied_document.events, copied_document.as_of
    ) == replay_events_through(original_document.events, original_document.as_of)
    assert material_files(tmp_path) == []


@pytest.mark.parametrize(
    "fault", ["multiple_tickers", "caa_ticker", "ledger_header", "reference_header"]
)
def test_explicit_buy_ledger_identity_rejects_conflicting_saved_facts(tmp_path, fault):
    source = source22(tmp_path)
    bind_ledger_reference(source, CAA_ID)
    if fault == "multiple_tickers":
        bind_ledger_reference(source, CAA_ID, ticker="8929")
    elif fault == "caa_ticker":
        insert_caa(source, caa_payload(ticker="1234"))
    elif fault == "reference_header":
        with sqlite3.connect(source) as connection:
            connection.execute(
                "UPDATE ledger_event SET decision_reference='other-reference' "
                "WHERE decision_reference=?",
                (CAA_ID,),
            )
    else:
        with sqlite3.connect(source) as connection:
            connection.execute(
                "UPDATE ledger_event SET ticker='1234' WHERE decision_reference=?", (CAA_ID,)
            )
    before = source.read_bytes()
    report = execute(tmp_path, source)
    assert report.blocked
    assert not (tmp_path / "final.sqlite").exists()
    assert source.read_bytes() == before


def test_late_caa_review_is_not_new_approval_for_unreferenced_material(tmp_path):
    source = source22(tmp_path)
    raw = caa_payload()
    raw["review"]["reviewed_at"] = "2026-10-14T16:22:16.617072+09:00"
    insert_caa(source, raw)
    report = execute(tmp_path, source, manifest=KeepManifest(retained_buy_references=(CAA_ID,)))
    assert any("review time" in reason for reason in report.blocked)
    assert not (tmp_path / "final.sqlite").exists()


@pytest.mark.parametrize(
    "fault", ["missing", "multi_allocate", "hash", "no_allocate", "ledger_ticker"]
)
def test_unresolved_old_buy_reference_stops_before_any_output(tmp_path, fault):
    source = source22(tmp_path)
    if fault != "missing":
        raw = caa_payload(allocate=fault != "no_allocate")
        if fault == "multi_allocate":
            second = deepcopy(raw["alternatives"][0])
            second["ticker"] = "5678"
            raw["alternatives"].append(second)
            refresh_caa_hash(raw)
        elif fault == "hash":
            raw["comparison"] = "different raw content after Review"
        elif fault == "ledger_ticker":
            with sqlite3.connect(source) as connection:
                payload = json.loads(
                    connection.execute(
                        "SELECT payload FROM ledger_event WHERE event_type='execution' LIMIT 1"
                    ).fetchone()[0]
                )
                payload["decision_reference"] = CAA_ID
                connection.execute(
                    "UPDATE ledger_event SET decision_reference=?,payload=? WHERE event_id=?",
                    (CAA_ID, canonical_json(payload), payload["event_id"]),
                )
        insert_caa(source, raw)
    before = source.read_bytes()
    report = execute(tmp_path, source, manifest=KeepManifest(retained_buy_references=(CAA_ID,)))
    assert report.blocked
    assert not (tmp_path / "final.sqlite").exists()
    assert not (tmp_path / "drafts").exists()
    assert source.read_bytes() == before


@pytest.mark.parametrize(
    "field", ["new_proposals", "operation_files", "proposal_file", "new_assessments"]
)
def test_manifest_rejects_isolated_new_artifact_imports(field):
    with pytest.raises(ValueError, match="Extra inputs"):
        KeepManifest.model_validate({field: ["synthetic-new-artifact.json"]})


def test_missing_old_assessment_closure_stops_all_writes_and_check_cli_reports(tmp_path, capsys):
    source = source22(tmp_path)
    manifest = tmp_path / "keep.json"
    manifest.write_text(KeepManifest(keep_assessment_sources=("missing",)).model_dump_json())
    assert (
        main(
            [
                "--source-app",
                str(source),
                "--output-app",
                str(tmp_path / "final.sqlite"),
                "--manifest",
                str(manifest),
                "--draft-dir",
                str(tmp_path / "drafts"),
                "--check",
            ]
        )
        == 3
    )
    assert "missing old assessment source" in json.loads(capsys.readouterr().out)["blocked"][0]
    assert not (tmp_path / "final.sqlite").exists()
    assert not (tmp_path / "drafts").exists()


@pytest.mark.parametrize("fault", ["review_hash", "source_ref", "arithmetic"])
def test_invalid_old_assessment_cannot_be_automatically_approved(tmp_path, fault):
    source = source22(tmp_path)
    with sqlite3.connect(source) as connection:
        document = json.loads(connection.execute("SELECT payload FROM thesis").fetchone()[0])
        review = json.loads(connection.execute("SELECT payload FROM thesis_review").fetchone()[0])
        if fault == "review_hash":
            review["reviewed_thesis_sha256"] = "0" * 64
        elif fault == "source_ref":
            document["investment_case"]["source_ids"] = ["missing"]
            connection.execute("DROP TRIGGER thesis_payload_immutable")
            digest = _legacy_thesis_core_hash(_LegacyThesisDocument.model_validate(document))
            review["reviewed_thesis_sha256"] = digest
            connection.execute(
                "UPDATE thesis SET payload=?,core_sha256=?", (canonical_json(document), digest)
            )
        else:
            review["recalculated_projections"][0]["terminal_value_per_share_yen"] = "1200"
        connection.execute("UPDATE thesis_review SET payload=?", (canonical_json(review),))
    assert execute(
        tmp_path, source, manifest=KeepManifest(keep_assessment_sources=(OLD_ID,))
    ).blocked
    assert not (tmp_path / "final.sqlite").exists()


def active_source(source, *, explicit=None, triage_id=None):
    selection = {
        "kind": "research_selection",
        "request": "synthetic original request",
        "targets": [{"ticker": "1234", "purpose": "new"}, {"ticker": "2331", "purpose": "holding"}],
    }
    if explicit is not None:
        selection["explicit_run"] = explicit
    if triage_id is not None:
        selection["research_triage_id"] = triage_id
    payload = {"checkpoint": "synthetic active draft", "artifacts": [selection]}
    identifier = "op-20261014-capital-allocation-1"
    with sqlite3.connect(source) as connection:
        connection.execute(
            "INSERT INTO operation_session VALUES (?,?,?,?,?,?,?,?)",
            (
                identifier,
                "capital-allocation",
                "active",
                "2026-10-14",
                None,
                "2026-10-14T16:00:00+09:00",
                None,
                canonical_json(payload),
            ),
        )
    return identifier


def bound_workspace(tmp_path, identifier):
    root = tmp_path / "workspace-root"
    workspace = root / "active"
    workspace.mkdir(parents=True)
    (workspace / "manifest.yaml").write_text(
        yaml.safe_dump({"operation_id": identifier, "as_of": "2026-10-14"})
    )
    (workspace / "1234").mkdir()
    (workspace / "1234/thesis-draft.yaml").write_text("synthetic authored draft\n")
    return root, workspace


def test_active_workspace_handoff_preserves_drafts_and_splits_mixed_requests(tmp_path):
    source = source22(tmp_path)
    identifier = active_source(source)
    root, workspace = bound_workspace(tmp_path, identifier)
    unrelated = root / "other"
    unrelated.mkdir()
    (unrelated / "manifest.yaml").write_text(yaml.safe_dump({"operation_id": "unrelated"}))
    (unrelated / "private-draft.yaml").write_text("unrelated authored file")
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    check = execute(tmp_path, source, workspace_root=root, check=True)
    assert check.active_handoffs == 1
    assert check.blocked == ()
    assert not (tmp_path / "drafts").exists()
    report = execute(tmp_path, source, workspace_root=root)
    assert report.blocked == ()
    handoff = json.loads((tmp_path / "drafts/handoff.json").read_text())[0]
    assert handoff["source_operation_id"] == identifier
    assert [item["scope"] for item in handoff["requests"]] == [None, "review_positions"]
    copied = list((tmp_path / "drafts").glob("workspace-*/1234/thesis-draft.yaml"))
    assert len(copied) == 1
    assert copied[0].read_bytes() == (workspace / "1234/thesis-draft.yaml").read_bytes()
    assert not list((tmp_path / "drafts").rglob("private-draft.yaml"))
    assert {path: path.read_bytes() for path in root.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("missing_new_entry", [False, True])
def test_mixed_triage_handoff_covers_only_new_targets_and_keeps_holding_material(
    tmp_path, missing_new_entry
):
    source = source22(tmp_path)
    # Frozen synthetic source22 Triage4, produced by the archived ed7b2bb7 helper.
    triage = json.loads(Path("tests/fixtures/application/triage-v4-synthetic.json").read_text())
    if missing_new_entry:
        triage["entries"] = [entry for entry in triage["entries"] if entry["ticker"] != "1234"]
    with closing(sqlite3.connect(source)) as connection:
        connection.execute(
            "INSERT INTO research_triage VALUES (?,?,?,?,?,?)",
            (
                triage["research_triage_id"],
                triage["review_set_id"],
                triage["run_revision_id"],
                triage["as_of"],
                triage["published_at"],
                canonical_json(triage),
            ),
        )
        connection.commit()
    identifier = active_source(source, triage_id=triage["research_triage_id"])
    holding_progress = {
        "kind": "research_progress",
        "outcomes": [
            {
                "ticker": "2331",
                "purpose": "holding",
                "status": "unfinished",
                "reason": "保有仮説の差分確認を執筆中",
                "references": ["2331/thesis-draft.yaml"],
            }
        ],
    }
    with closing(sqlite3.connect(source)) as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload FROM operation_session WHERE operation_id=?", (identifier,)
            ).fetchone()[0]
        )
        payload["artifacts"].append(holding_progress)
        connection.execute(
            "UPDATE operation_session SET payload=? WHERE operation_id=?",
            (canonical_json(payload), identifier),
        )
        connection.commit()
        ledger = load_ledger_document(connection)
        holdings = replay_events_through(ledger.events, ledger.as_of).lots
        assert sum(lot.quantity for lot in holdings["2331"]) > 0
        assert "1234" not in holdings
    root, workspace = bound_workspace(tmp_path, identifier)
    holding_directory = workspace / "2331"
    holding_directory.mkdir()
    holding_draft = holding_directory / "thesis-draft.yaml"
    holding_draft.write_text("保有の合成根拠と反証の未完draft\n")
    before_source = source.read_bytes()
    before_workspace = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    checked = execute(tmp_path, source, workspace_root=root, check=True)
    assert not (tmp_path / "drafts").exists()
    if missing_new_entry:
        assert any("old selected Triage analysis is incomplete" in item for item in checked.blocked)
        assert execute(tmp_path, source, workspace_root=root).blocked == checked.blocked
        assert not (tmp_path / "final.sqlite").exists()
    else:
        assert checked.blocked == ()
        assert execute(tmp_path, source, workspace_root=root) == checked
        retained = material_files(tmp_path)[0]
        assert retained["selected_entries"] == [triage["entries"][0]]
        handoff = json.loads((tmp_path / "drafts/handoff.json").read_text())[0]
        assert handoff["source_payload"]["artifacts"][1] == holding_progress
        assert handoff["requests"][1]["targets"] == [{"ticker": "2331", "purpose": "holding"}]
        copied = list((tmp_path / "drafts").glob("workspace-*/2331/thesis-draft.yaml"))
        assert len(copied) == 1
        assert copied[0].read_bytes() == holding_draft.read_bytes()
    assert source.read_bytes() == before_source
    assert {
        path: path.read_bytes() for path in root.rglob("*") if path.is_file()
    } == before_workspace


@pytest.mark.parametrize(
    "case", ["missing_root", "different_id", "outside_symlink", "nested_other"]
)
def test_active_handoff_confinement_and_binding_gaps_stop_copy(tmp_path, case):
    source = source22(tmp_path)
    identifier = active_source(source)
    root, workspace = bound_workspace(tmp_path, identifier)
    if case == "different_id":
        (workspace / "manifest.yaml").write_text(yaml.safe_dump({"operation_id": "other"}))
    elif case == "outside_symlink":
        outside = tmp_path / "outside"
        outside.write_text("outside root")
        (workspace / "draft.yaml").symlink_to(outside)
    elif case == "nested_other":
        (workspace / "child").mkdir()
        (workspace / "child/manifest.yaml").write_text(yaml.safe_dump({"operation_id": "other"}))
    report = execute(tmp_path, source, workspace_root=None if case == "missing_root" else root)
    assert report.blocked
    assert not (tmp_path / "final.sqlite").exists()


def test_source_recheck_uses_new_transaction_and_real_concurrent_change_stops_output(
    tmp_path, monkeypatch
):
    source = source22(tmp_path)
    original = tool._snapshot
    calls = 0

    def changed(reader):
        nonlocal calls
        calls += 1
        if calls == 2:
            with sqlite3.connect(source) as writer:
                writer.execute(
                    "UPDATE task SET payload=? WHERE task_id='synthetic-task'",
                    ('{"new":"concurrent writer"}',),
                )
        return original(reader)

    monkeypatch.setattr(tool, "_snapshot", changed)
    with pytest.raises(ValueError, match="source changed after copy"):
        execute(tmp_path, source)
    assert calls == 2
    assert not (tmp_path / "final.sqlite").exists()
    assert not (tmp_path / "drafts").exists()
    assert source.exists()


def old_run_cache(tmp_path):
    path = tmp_path / "old-runs.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(Path("tests/fixtures/application/run-v5-schema.sql").read_text())
        connection.execute(
            "INSERT INTO screening_run VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "run-old",
                "screening-20261013",
                "2026-10-13",
                "2026-10-13",
                "2026-10-13T18:00:00+09:00",
                1,
                "synthetic",
                "2026-10-13T18:00:00+09:00",
                canonical_json(
                    dict(
                        run_id="screening-20261013",
                        run_date="2026-10-13",
                        asof_date="2026-10-13",
                        run_at="2026-10-13T18:00:00+09:00",
                        universe_size=1,
                        screening_rules_hash="old-rules",
                        er_model_version=None,
                    )
                ),
            ),
        )
        connection.execute(
            "INSERT INTO security_analysis VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "run-old",
                0,
                "1234",
                "情報・通信業",
                None,
                None,
                None,
                None,
                None,
                '{"ticker":"1234","name":"合成企業","metrics":{"adv_yen":null,"unknown":null}}',
            ),
        )
    return path


def test_old_run_raw_closure_is_preserved_and_new_cache_starts_empty(tmp_path):
    source, runs = source22(tmp_path), old_run_cache(tmp_path)
    before_app, before_runs = source.read_bytes(), runs.read_bytes()
    manifest = KeepManifest(
        required_run_sources=(RequiredRunSource(run_revision_id="run-old", ticker="1234"),)
    )
    report = execute(
        tmp_path,
        source,
        manifest=manifest,
        source_runs=runs,
        output_runs=tmp_path / "new-runs.sqlite",
    )
    assert report.blocked == ()
    assert report.source_run_version == 5
    assert report.output_run_version == RUN_STORE_SCHEMA_VERSION == 6
    assert report.run_recreation_required is True
    assert source.read_bytes() == before_app
    assert runs.read_bytes() == before_runs
    material = material_files(tmp_path)[0]
    assert material["source_run_revision_id"] == "run-old"
    assert material["source_header"]["asof_date"] == "2026-10-13"
    assert material["source_analysis"]["metrics"]["unknown"] is None
    assert "candidate" not in material["source_analysis"]
    with sqlite3.connect(tmp_path / "new-runs.sqlite") as connection:
        assert connection.execute("SELECT count(*) FROM screening_run").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM stock_analysis").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_schema WHERE name='review_set'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("case", ["no_store", "missing_run", "missing_ticker"])
def test_required_old_run_gaps_are_not_filled_with_new_values(tmp_path, case):
    source = source22(tmp_path)
    runs = None if case == "no_store" else old_run_cache(tmp_path)
    requested = RequiredRunSource(
        run_revision_id="missing" if case == "missing_run" else "run-old",
        ticker="5678" if case == "missing_ticker" else "1234",
    )
    report = execute(
        tmp_path,
        source,
        manifest=KeepManifest(required_run_sources=(requested,)),
        source_runs=runs,
        output_runs=tmp_path / "new-runs.sqlite",
    )
    assert report.blocked
    assert not (tmp_path / "new-runs.sqlite").exists()
    assert not (tmp_path / "final.sqlite").exists()


def test_old_selected_snapshot_keeps_only_actual_values_without_new_candidate_fields(tmp_path):
    source = source22(tmp_path)
    explicit = {
        "run_revision_id": "run-pruned",
        "as_of": "2026-10-13",
        "run_at": "2026-10-13T18:00:00+09:00",
        "run_metadata": {"er_model_version": None},
        "analyses": {
            "1234": {"ticker": "1234", "metrics": {"dividend_basis": None}},
            "2331": {"ticker": "2331", "metrics": {"adv_yen": 10000}},
        },
    }
    identifier = active_source(source, explicit=explicit)
    root, _workspace = bound_workspace(tmp_path, identifier)
    report = execute(tmp_path, source, workspace_root=root)
    assert report.blocked == ()
    material = material_files(tmp_path)[0]
    assert material["selected_analyses"] == explicit["analyses"]
    assert material["source_as_of"] == "2026-10-13"
    assert material["source_run_revision_id"] == "run-pruned"
    assert all("candidate" not in row for row in material["selected_analyses"].values())


def test_explicit_run_handoff_still_requires_holding_analysis(tmp_path):
    source = source22(tmp_path)
    identifier = active_source(
        source,
        explicit={"analyses": {"1234": {"ticker": "1234", "metrics": {}}}},
    )
    root, _workspace = bound_workspace(tmp_path, identifier)
    report = execute(tmp_path, source, workspace_root=root)
    assert any("old selected analysis snapshot is incomplete" in item for item in report.blocked)
    assert not (tmp_path / "final.sqlite").exists()
    assert not (tmp_path / "drafts").exists()


def test_missing_selected_old_triage_evidence_stops_before_cache_retirement(tmp_path):
    source = source22(tmp_path)
    identifier = active_source(source, triage_id="missing-triage")
    root, _workspace = bound_workspace(tmp_path, identifier)
    report = execute(tmp_path, source, workspace_root=root)
    assert any("missing old selected Triage evidence" in message for message in report.blocked)
    assert not (tmp_path / "final.sqlite").exists()


def test_existing_outputs_and_development_schema23_are_rejected(tmp_path):
    source = source22(tmp_path)
    existing = tmp_path / "final.sqlite"
    existing.write_text("preserve me")
    with pytest.raises(ValueError, match="new, absent"):
        execute(tmp_path, source)
    assert existing.read_text() == "preserve me"
    existing.unlink()
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA user_version=23")
    with pytest.raises(ValueError, match="schema22"):
        execute(tmp_path, source)
    assert not existing.exists()


def test_old_schema22_reader_rejects_final24_generation(tmp_path, monkeypatch):
    import baibai_engine.storage.application.read as reader

    source = source22(tmp_path)
    assert not execute(tmp_path, source).blocked
    monkeypatch.setattr(reader, "APPLICATION_SCHEMA_VERSION", 22)
    with pytest.raises(RuntimeError, match="found user_version=24, expected=22"):
        reader.connect_read_only(tmp_path / "final.sqlite")


def test_tool_imports_do_not_retain_runtime_legacy_readers():
    import ast

    paths = [Path("tools/cutover/domain_v1.py"), Path("tools/cutover/assessment_proposal_v1.py")]
    legacy = {
        "baibai_engine.research.thesis",
        "baibai_engine.research.thesis_store",
        "baibai_engine.research.capital_allocation",
        "baibai_engine.research.position_review",
    }
    for path in paths:
        modules = {
            node.module
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.ImportFrom)
        }
        assert not modules & legacy


def insert_bound_caa(source):
    raw = caa_payload()
    with sqlite3.connect(source) as connection:
        digest, review_id = connection.execute(
            "SELECT t.core_sha256,r.review_id FROM thesis t JOIN thesis_review r USING(thesis_id) WHERE thesis_id=?",
            (OLD_ID,),
        ).fetchone()
    raw["alternatives"][0].update(thesis_core_sha256=digest, thesis_review_id=review_id)
    refresh_caa_hash(raw)
    insert_caa(source, raw)
    return raw


@pytest.mark.parametrize("field", ["ticker", "thesis_core_sha256", "thesis_review_id"])
def test_useful_old_caa_material_requires_exact_assessment_and_review_references(tmp_path, field):
    source = source22(tmp_path)
    raw = insert_bound_caa(source)
    raw["alternatives"][0][field] = {
        "ticker": "5678",
        "thesis_core_sha256": "0" * 64,
        "thesis_review_id": "different-review",
    }[field]
    refresh_caa_hash(raw)
    with sqlite3.connect(source) as connection:
        connection.execute(
            "UPDATE capital_allocation_assessment SET payload=?", (canonical_json(raw),)
        )
    report = execute(tmp_path, source, manifest=KeepManifest(keep_proposal_sources=(CAA_ID,)))
    assert any("exact assessment/Review" in reason for reason in report.blocked)
    assert not (tmp_path / "final.sqlite").exists()


def test_useful_old_caa_material_keeps_its_prose_and_closes_actual_assessment_sources(tmp_path):
    source = source22(tmp_path)
    raw = insert_bound_caa(source)
    report = execute(tmp_path, source, manifest=KeepManifest(keep_proposal_sources=(CAA_ID,)))
    assert report.blocked == ()
    materials = material_files(tmp_path)
    assert {material["kind"] for material in materials} == {
        "proposal_source_draft",
        "assessment_source_draft",
    }
    old_proposal = next(
        material for material in materials if material["kind"] == "proposal_source_draft"
    )
    assert old_proposal["source_document"] == raw
    assert old_proposal["saved_proposal_terms"] is None
    with connect_read_only(tmp_path / "final.sqlite") as connection:
        assert connection.execute("SELECT count(*) FROM investment_proposal").fetchone()[0] == 0


@pytest.mark.parametrize("fault", ["asof", "public_id", "revision", "rules_ref"])
def test_old_run_header_and_rules_reference_mismatches_stop_copy(tmp_path, fault):
    source, runs = source22(tmp_path), old_run_cache(tmp_path)
    with sqlite3.connect(runs) as connection:
        header = json.loads(connection.execute("SELECT payload FROM screening_run").fetchone()[0])
        if fault == "asof":
            header["asof_date"] = "2026-10-12"
        elif fault == "public_id":
            header["run_id"] = "screening-20261012"
        elif fault == "revision":
            header["run_revision_id"] = "different-run"
        else:
            reference = {
                "review_set_id": "old-set",
                "run_revision_id": "run-old",
                "as_of": "2026-10-13",
                "created_at": "2026-10-13T18:05:00+09:00",
                "screening_rules_hash": "different-rules",
            }
            connection.execute(
                "INSERT INTO review_set VALUES (?,?,?,?)",
                ("old-set", "run-old", reference["created_at"], canonical_json(reference)),
            )
        connection.execute("UPDATE screening_run SET payload=?", (canonical_json(header),))
    report = execute(
        tmp_path,
        source,
        manifest=KeepManifest(
            required_run_sources=(RequiredRunSource(run_revision_id="run-old", ticker="1234"),)
        ),
        source_runs=runs,
        output_runs=tmp_path / "new-runs.sqlite",
    )
    assert any("identity differs" in reason for reason in report.blocked)
    assert not (tmp_path / "final.sqlite").exists()
    assert not (tmp_path / "new-runs.sqlite").exists()


def old_v3_document():
    raw, _review = old_pair()
    raw["schema_version"] = 3
    raw.pop("valuation")
    raw.pop("investment_case")
    raw["judgment"] = {
        "recommendation": "buy",
        "proposed_at": "2026-10-14T16:00:00+09:00",
        "confidence": "medium",
        "permanent_loss_conclusion": "acceptable",
        "strongest_countercase": "架空の反証",
        "sizing_action": "normal",
    }
    raw["estimates"] = {
        "model_version": "synthetic-old-scenario",
        "market_price_fact_id": "close",
        "entry_price_basis_yen": "1100",
        "entry_price_basis": "observed_market_price",
        "entry_price_source_ids": ["price"],
        "entry_price_assumption": "架空の旧起点",
        "required_5y_base_cagr_pct": 8.5,
        "current_fair_value_yen": "1200",
        "valuation_model_version": "synthetic-old-fv",
        "fair_value_source_ids": ["annual"],
        "scenarios": [
            dict(
                horizon_years=horizon,
                name=name,
                earnings_basis="net_income_attributable_to_owners",
                starting_earnings_fact_id="income",
                starting_earnings_yen="1000000",
                annual_earnings_growth_pct=0.0,
                starting_share_count_fact_id="shares",
                starting_share_count="1000",
                annual_share_count_change_pct=0.0,
                terminal_valuation_multiple="1.2",
                cumulative_dividend_per_share_yen="0",
                terminal_price_includes_dividends=False,
                claimed_terminal_earnings_yen="1000000",
                claimed_terminal_share_count="1000",
                claimed_terminal_price_yen="1200",
                claimed_total_return_cagr_pct=0.0,
                as_of="2026-10-14",
                unit="JPY_per_share_total_return",
                model_version="synthetic-old-scenario",
                assumption="架空の旧予測原文",
                source_ids=["annual"],
            )
            for horizon in (3, 5)
            for name in ("bear", "base", "bull")
        ],
    }
    raw["input_snapshot"]["facts"].extend(
        [
            dict(
                fact_id="income",
                fact_kind="net_income_attributable_to_owners",
                value=1000000,
                unit="JPY",
                as_of="2026-10-14",
                source_ids=["annual"],
            ),
            dict(
                fact_id="shares",
                fact_kind="shares_outstanding",
                value=1000,
                unit="shares",
                as_of="2026-10-14",
                source_ids=["annual"],
            ),
        ]
    )
    return raw


def install_v3_source(source, *, missing_hash=False):
    from tools.cutover.assessment_proposal_v1 import _legacy3_core_hash, _Legacy3ThesisDocument

    raw = old_v3_document()
    digest = _legacy3_core_hash(_Legacy3ThesisDocument.model_validate(raw))
    with sqlite3.connect(source) as connection:
        connection.execute("DROP TRIGGER thesis_payload_immutable")
        connection.execute("DELETE FROM thesis_review")
        connection.execute(
            "UPDATE thesis SET recommendation='buy',payload=?,core_sha256=?",
            (canonical_json(raw), None if missing_hash else digest),
        )
    return raw, digest


@pytest.mark.parametrize("missing_hash", [False, True])
def test_old_thesis3_is_preserved_as_material_with_explicit_limits_and_no_current_valuation(
    tmp_path, missing_hash
):
    source = source22(tmp_path)
    raw, digest = install_v3_source(source, missing_hash=missing_hash)
    report = execute(tmp_path, source, manifest=KeepManifest(keep_assessment_sources=(OLD_ID,)))
    assert report.blocked == ()
    assert report.material_limitations
    material = material_files(tmp_path)[0]
    assert material["source_schema_version"] == 3
    assert material["source_document"] == raw
    assert material["source_core_sha256"] == (None if missing_hash else digest)
    assert material["source_review"] is None
    assert material["assessment_draft"]["valuation"] is None
    assert material["assessment_draft"]["investment_case"] is None
    assert material["assessment_draft"]["findings"] is None
    assert "通常Researchで再作成" in " ".join(report.material_limitations)
    if missing_hash:
        assert "core hash未保存" in " ".join(report.material_limitations)
    with connect_read_only(tmp_path / "final.sqlite") as connection:
        assert connection.execute("SELECT count(*) FROM stock_assessment").fetchone()[0] == 0


@pytest.mark.parametrize("fault", ["hash", "header", "review_reference"])
def test_old_thesis3_recorded_hash_header_and_review_references_are_checked(tmp_path, fault):
    source = source22(tmp_path)
    raw, _digest = install_v3_source(source)
    with sqlite3.connect(source) as connection:
        if fault == "hash":
            connection.execute("UPDATE thesis SET core_sha256=?", ("not-sha256",))
        elif fault == "header":
            connection.execute("UPDATE thesis SET ticker='5678'")
        else:
            raw["independent_review_ref"] = "missing-review"
            from tools.cutover.assessment_proposal_v1 import (
                _legacy3_core_hash,
                _Legacy3ThesisDocument,
            )

            digest = _legacy3_core_hash(_Legacy3ThesisDocument.model_validate(raw))
            connection.execute(
                "UPDATE thesis SET payload=?,core_sha256=?", (canonical_json(raw), digest)
            )
    report = execute(tmp_path, source, manifest=KeepManifest(keep_assessment_sources=(OLD_ID,)))
    assert report.blocked
    assert not (tmp_path / "final.sqlite").exists()


def install_historical_v3_pair(source, *, review_ref="2026-10-14-1234-decision-review.yaml"):
    """A frozen published identity predates decimal-string and optional-field changes."""
    from tools.cutover.assessment_proposal_v1 import _legacy3_core_hash, _Legacy3ThesisDocument

    raw = old_v3_document()
    raw["independent_review_ref"] = review_ref
    raw.pop("human_evidence_override", None)
    for source_item in raw["input_snapshot"]["sources"]:
        for optional in ("ref", "provider", "dataset"):
            if source_item.get(optional) is None:
                source_item.pop(optional, None)
    for fact in raw["input_snapshot"]["facts"]:
        for optional in ("observed_at", "price_basis"):
            if fact.get(optional) is None:
                fact.pop(optional, None)
    raw["estimates"]["entry_price_basis_yen"] = 1100.0
    raw["estimates"]["current_fair_value_yen"] = 1200.0
    for scenario in raw["estimates"]["scenarios"]:
        for key, value in scenario.items():
            if key.endswith("_yen") or key in {
                "starting_share_count",
                "terminal_valuation_multiple",
                "claimed_terminal_share_count",
            }:
                scenario[key] = float(value)
    digest = hashlib.sha256(canonical_json(raw).encode()).hexdigest()
    assert digest != _legacy3_core_hash(_Legacy3ThesisDocument.model_validate(raw))
    review = {
        "review_id": "synthetic-review-row-id",
        "reviewer_role": "independent_second_pass",
        "reviewer_identity": "synthetic-independent",
        "reviewer_run_id": "synthetic-review-run",
        "reviewed_at": "2026-10-14T16:05:00+09:00",
        "reviewed_thesis_sha256": digest,
        "primary_source_check": "verified",
        "checked_source_ids": ["annual", "price"],
        "recalculated_scenarios": [
            dict(horizon_years=horizon, name=name, total_return_cagr_pct=0.0)
            for horizon in (3, 5)
            for name in ("bear", "base", "bull")
        ],
        "strongest_countercase": "架空の独立反証",
        "alternative_candidate_check": "unavailable",
        "proposal_changed": False,
        "change_rationale": None,
    }
    with sqlite3.connect(source) as connection:
        connection.execute("DROP TRIGGER thesis_payload_immutable")
        connection.execute("DELETE FROM thesis_review")
        connection.execute(
            "UPDATE thesis SET recommendation='buy',published_at=?,payload=?,core_sha256=?",
            (raw["judgment"]["proposed_at"], canonical_json(raw), digest),
        )
        connection.execute(
            "INSERT INTO thesis_review VALUES (?,?,?,?)",
            (
                review["review_id"],
                OLD_ID,
                review["reviewed_at"],
                canonical_json(review),
            ),
        )
    return raw, review, digest


@pytest.mark.parametrize(
    "review_ref", ["2026-10-14-1234-decision-review.yaml", "reviews/custom-name.yaml"]
)
def test_published_thesis3_keeps_saved_identity_and_filename_with_review_after_proposed_at(
    tmp_path, review_ref
):
    source = source22(tmp_path)
    raw, review, digest = install_historical_v3_pair(source, review_ref=review_ref)
    before = source.read_bytes()
    report = execute(tmp_path, source, manifest=KeepManifest(keep_assessment_sources=(OLD_ID,)))
    assert report.blocked == ()
    material = material_files(tmp_path)[0]
    assert material["source_document"] == raw
    assert material["source_payload"] == canonical_json(raw)
    assert material["source_core_sha256"] == digest
    assert material["source_review"] == review
    assert material["source_review_payload"] == canonical_json(review)
    assert material["source_published_at"] == raw["judgment"]["proposed_at"]
    assert raw["independent_review_ref"] != review["review_id"]
    assert material["new_review_required"] is True
    assert material["assessment_draft"]["valuation"] is None
    assert source.read_bytes() == before


@pytest.mark.parametrize("fault", ["saved_hash", "review_id", "review_time", "source_reference"])
def test_published_thesis3_still_rejects_saved_binding_and_source_contradictions(tmp_path, fault):
    source = source22(tmp_path)
    raw, _review, _digest = install_historical_v3_pair(source)
    with sqlite3.connect(source) as connection:
        if fault == "saved_hash":
            connection.execute("UPDATE thesis SET core_sha256=?", ("0" * 64,))
        elif fault == "review_id":
            connection.execute("UPDATE thesis_review SET review_id='different-id'")
        elif fault == "review_time":
            connection.execute("UPDATE thesis_review SET reviewed_at='2026-10-14T16:06:00+09:00'")
        else:
            raw["estimates"]["entry_price_source_ids"] = ["missing-source"]
            connection.execute("UPDATE thesis SET payload=?", (canonical_json(raw),))
    report = execute(tmp_path, source, manifest=KeepManifest(keep_assessment_sources=(OLD_ID,)))
    assert report.blocked
    assert not (tmp_path / "final.sqlite").exists()


@pytest.mark.parametrize(
    "fault", [None, "ticker", "as_of", "holding_source", "ledger_source", "candidate_source"]
)
def test_old_position_review2_binds_by_saved_row_and_sources_not_invented_root_ids(tmp_path, fault):
    source = source22(tmp_path)
    install_historical_v3_pair(source)
    raw = json.loads(
        Path("tests/fixtures/application/position-review-v2-synthetic.json").read_text()
    )
    if fault == "ticker":
        raw["ticker"] = "5678"
    elif fault == "as_of":
        raw["as_of"] = "2026-10-13"
    elif fault == "holding_source":
        raw["sources"]["holding_thesis"]["entity_id"] = "missing-thesis"
    elif fault == "ledger_source":
        raw["sources"]["ledger"]["entity_id"] = "other-ledger"
    elif fault == "candidate_source":
        raw["sources"]["candidate_thesis"] = {"entity_id": "missing-candidate"}
    identifier = "synthetic-old-position-review2"
    with sqlite3.connect(source) as connection:
        connection.execute(
            "INSERT INTO position_review VALUES (?,?,?,?,?,?)",
            (
                identifier,
                "1234",
                "2026-10-14",
                OLD_ID,
                None,
                canonical_json(raw),
            ),
        )
    before = source.read_bytes()
    report = execute(
        tmp_path, source, manifest=KeepManifest(keep_position_reason_sources=(identifier,))
    )
    assert source.read_bytes() == before
    if fault:
        assert report.blocked
        assert not (tmp_path / "final.sqlite").exists()
        return
    assert report.blocked == ()
    material = next(
        item for item in material_files(tmp_path) if item["kind"] == "position_reason_source_draft"
    )
    assert "position_review_id" not in raw
    assert "thesis_id" not in raw
    assert material["source_id"] == identifier
    assert material["source_thesis_id"] == OLD_ID
    assert material["source_document"] == raw
    assert material["adoption_history"] is None
    assert material["original_investment_reason"] is None
    assert material["new_proposal_required"] is True


@pytest.mark.parametrize("fault", [None, "candidate_ticker", "candidate_hash"])
def test_old_position_review2_preserves_a_valid_replacement_without_adopting_it(tmp_path, fault):
    source = source22(tmp_path)
    candidate_id = "synthetic-replacement-assessment"
    candidate, review = old_pair()
    candidate["input_snapshot"]["ticker"] = "5678"
    for item in candidate["input_snapshot"]["sources"]:
        item["ticker"] = "5678"
    digest = _legacy_thesis_core_hash(_LegacyThesisDocument.model_validate(candidate))
    review["review_id"] = "synthetic-replacement-review"
    review["reviewed_thesis_sha256"] = digest
    raw = json.loads(
        Path("tests/fixtures/application/position-review-v2-synthetic.json").read_text()
    )
    raw["sources"]["candidate_thesis"] = {"entity_id": candidate_id}
    raw["replacement_comparison"] = {
        "status": "evaluated",
        "candidate": {"ticker": "9999" if fault == "candidate_ticker" else "5678"},
    }
    identifier = "synthetic-position-review-with-replacement"
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.execute(
            "INSERT INTO thesis VALUES (?,?,?,?,?,?,?,?)",
            (
                candidate_id,
                "5678",
                "2026-10-14",
                "candidate",
                "2026-10-14T16:06:00+09:00",
                None,
                canonical_json(candidate),
                "0" * 64 if fault == "candidate_hash" else digest,
            ),
        )
        connection.execute(
            "INSERT INTO thesis_review VALUES (?,?,?,?)",
            (review["review_id"], candidate_id, review["reviewed_at"], canonical_json(review)),
        )
        connection.execute(
            "INSERT INTO position_review VALUES (?,?,?,?,?,?)",
            (identifier, "1234", "2026-10-14", OLD_ID, candidate_id, canonical_json(raw)),
        )
    before = source.read_bytes()
    report = execute(
        tmp_path, source, manifest=KeepManifest(keep_position_reason_sources=(identifier,))
    )
    assert source.read_bytes() == before
    if fault is not None:
        assert report.blocked
        assert not (tmp_path / "final.sqlite").exists()
        return
    assert report.blocked == ()
    material = next(
        item for item in material_files(tmp_path) if item["kind"] == "position_reason_source_draft"
    )
    assert material["source_candidate_thesis_id"] == candidate_id
    assert material["source_document"] == raw
    assert material["adoption_history"] is None
    assert material["original_investment_reason"] is None
    assert material["new_proposal_required"] is True


def test_old_position_review_current_case_is_not_fabricated_as_original_buy_reason(tmp_path):
    source = source22(tmp_path)
    identifier = "synthetic-old-position-review"
    raw = {
        "schema_version": 3,
        "position_review_id": identifier,
        "thesis_id": OLD_ID,
        "position_id": "synthetic-position",
        "ticker": "1234",
        "as_of": "2026-10-14",
        "holding": {"quantity": 100, "cost_yen": "100000", "quantity_basis_confirmed": True},
        "quote": None,
        "remaining_reward": None,
        "action": "hold",
        "unresolved_reason": None,
    }
    with sqlite3.connect(source) as connection:
        connection.execute(
            "INSERT INTO position_review VALUES (?,?,?,?,?,?)",
            (identifier, "1234", "2026-10-14", OLD_ID, None, canonical_json(raw)),
        )
    report = execute(
        tmp_path, source, manifest=KeepManifest(keep_position_reason_sources=(identifier,))
    )
    assert report.blocked == ()
    material = next(
        item for item in material_files(tmp_path) if item["kind"] == "position_reason_source_draft"
    )
    assert material["source_document"] == raw
    assert material["source_case_explanation"]
    assert material["original_investment_reason"] is None
    assert material["original_reason_unavailable_reason"]
    assert material["adoption_history"] is None


def test_last_source_recheck_after_material_copy_catches_late_writer_and_cleans_only_outputs(
    tmp_path, monkeypatch
):
    source = source22(tmp_path)
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
    original = tool.write_text_atomic
    changed = False

    def after_material(path, content):
        nonlocal changed
        original(path, content)
        if not changed:
            changed = True
            with sqlite3.connect(source) as writer:
                writer.execute(
                    "UPDATE task SET payload=? WHERE task_id='synthetic-task'",
                    ('{"late":"writer after draft copy"}',),
                )

    monkeypatch.setattr(tool, "write_text_atomic", after_material)
    with pytest.raises(ValueError, match="source changed after copy"):
        execute(tmp_path, source, manifest=KeepManifest(keep_assessment_sources=(OLD_ID,)))
    assert changed
    assert source.exists()
    assert not (tmp_path / "final.sqlite").exists()
    assert not (tmp_path / "drafts").exists()
    with sqlite3.connect(source) as connection:
        assert (
            json.loads(
                connection.execute(
                    "SELECT payload FROM task WHERE task_id='synthetic-task'"
                ).fetchone()[0]
            )["late"]
            == "writer after draft copy"
        )


def test_thesis3_core_hash_matches_independently_checked_original_f52c1403_preimage():
    from tools.cutover.assessment_proposal_v1 import _legacy3_core_hash, _Legacy3ThesisDocument

    assert (
        _legacy3_core_hash(_Legacy3ThesisDocument.model_validate(old_v3_document()))
        == "02254a7edca52d617a24fb3af35fddf3db1d6ccdea45cf7dbc700d5ca80b8ea2"
    )


def test_last_old_cache_fingerprint_recheck_catches_late_run_writer(tmp_path, monkeypatch):
    source, runs = source22(tmp_path), old_run_cache(tmp_path)
    with sqlite3.connect(runs) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
    original = tool.write_text_atomic
    changed = False

    def after_material(path, content):
        nonlocal changed
        original(path, content)
        if not changed:
            changed = True
            with sqlite3.connect(runs) as writer:
                writer.execute(
                    "UPDATE security_analysis SET payload=? WHERE ticker='1234'",
                    ('{"ticker":"1234","name":"late original source update"}',),
                )

    monkeypatch.setattr(tool, "write_text_atomic", after_material)
    manifest = KeepManifest(
        required_run_sources=(RequiredRunSource(run_revision_id="run-old", ticker="1234"),)
    )
    with pytest.raises(ValueError, match="run source changed after copy"):
        execute(
            tmp_path,
            source,
            manifest=manifest,
            source_runs=runs,
            output_runs=tmp_path / "new-runs.sqlite",
        )
    assert changed
    assert source.exists()
    assert runs.exists()
    assert not (tmp_path / "final.sqlite").exists()
    assert not (tmp_path / "new-runs.sqlite").exists()
    assert not (tmp_path / "drafts").exists()


def test_cli_synthetic_check_and_copy_rehearsal_are_same_plan_and_keep_sources(tmp_path, capsys):
    source, runs = source22(tmp_path), old_run_cache(tmp_path)
    manifest = tmp_path / "keep.json"
    manifest.write_text(
        KeepManifest(
            keep_assessment_sources=(OLD_ID,),
            required_run_sources=(RequiredRunSource(run_revision_id="run-old", ticker="1234"),),
        ).model_dump_json()
    )
    before = {path: path.read_bytes() for path in (source, runs, manifest)}
    argv = [
        "--source-app",
        str(source),
        "--source-runs",
        str(runs),
        "--output-app",
        str(tmp_path / "final.sqlite"),
        "--output-runs",
        str(tmp_path / "new-runs.sqlite"),
        "--manifest",
        str(manifest),
        "--draft-dir",
        str(tmp_path / "drafts"),
    ]
    assert main([*argv, "--check"]) == 0
    check_report = json.loads(capsys.readouterr().out)
    assert not (tmp_path / "final.sqlite").exists()
    assert not (tmp_path / "new-runs.sqlite").exists()
    assert not (tmp_path / "drafts").exists()
    assert main(argv) == 0
    assert json.loads(capsys.readouterr().out) == check_report
    assert {path: path.read_bytes() for path in before} == before
    assert check_report["source_version"] == 22
    assert check_report["output_version"] == 24
    assert check_report["output_run_version"] == 6
    assert check_report["blocked"] == []


@pytest.mark.parametrize("version", [1, 2])
def test_historical_caa_material_preserves_review_after_authored_publication(tmp_path, version):
    source = source22(tmp_path)
    _, review = old_pair()
    raw = caa_payload(version=version)
    raw["alternatives"][0]["thesis_core_sha256"] = review["reviewed_thesis_sha256"]
    raw["alternatives"][0]["thesis_review_id"] = review["review_id"]
    raw["review"]["reviewed_at"] = "2026-10-14T16:22:16.617072+09:00"
    refresh_caa_hash(raw)
    insert_caa(source, raw)
    before = source.read_bytes()
    report = execute(tmp_path, source, manifest=KeepManifest(keep_proposal_sources=(CAA_ID,)))
    assert report.blocked == ()
    assert source.read_bytes() == before
    material = next(
        item for item in material_files(tmp_path) if item["kind"] == "proposal_source_draft"
    )
    assert material["source_document"] == raw
    assert material["saved_proposal_terms"] is None
    assert material["new_proposal_required"] is True
    with connect_read_only(tmp_path / "final.sqlite") as final:
        assert final.execute("SELECT count(*) FROM investment_proposal").fetchone()[0] == 0
        assert final.execute("SELECT count(*) FROM position_decision_reference").fetchone()[0] == 0
