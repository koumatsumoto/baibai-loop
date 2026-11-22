"""An optional export failure must not hide an already committed research record."""

import importlib
import json
import sqlite3
from contextlib import closing
from datetime import date, datetime

import pytest
from tests.helpers.assessment_v1 import assessment_id, pair_payload

from baibai_engine.cli.app import main
from baibai_engine.investment_judgment.company_research.document import scaffold_research
from baibai_engine.investment_judgment.judgment_retrospective.query import scaffold_retrospective
from baibai_engine.investment_judgment.stock_assessment.store import AssessmentStoreService

NOW = datetime.fromisoformat("2026-10-14T18:00:00+09:00")
PRIVATE = "synthetic-private-authored-research"


def _case(tmp_path, domain):
    db = tmp_path / "app.sqlite"
    if domain == "company-research":
        draft = scaffold_research(research_id="r1", ticker="1234", as_of=date(2026, 10, 14))
        draft["document"]["investigated_scope"] = PRIVATE
        draft["document"]["unresolved_questions"] = ["契約条件の追加開示を確認する"]
        table, identity = "company_research", "research_id"
    else:
        document, review = pair_payload()
        AssessmentStoreService(db, clock=lambda: NOW).publish_reviewed_assessment(
            assessment_id(), document, review
        )
        draft = scaffold_retrospective(
            retrospective_id="r1",
            assessment_id=assessment_id(),
            ticker=None,
            proposal_id=None,
            as_of=date(2026, 10, 14),
            milestone="次の確認事項を振り返る",
            db_path=db,
            known_at=NOW,
        )["draft"]
        draft["document"]["lesson"] = {"next_time": PRIVATE, "rationale": "未確認を事実にしない"}
        table, identity = "judgment_retrospective", "retrospective_id"
    path = tmp_path / "draft.json"
    path.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    return db, path, table, identity


def _count(db, table):
    if not db.exists():
        return 0
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as connection:
        return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


@pytest.mark.parametrize("domain", ["company-research", "judgment-retrospective"])
@pytest.mark.parametrize("failure", ["blocked_parent", "raced_output", "partial_io"])
def test_committed_record_is_reported_and_exactly_recoverable_after_export_failure(
    tmp_path, monkeypatch, capsys, domain, failure
):
    db, draft, table, identity = _case(tmp_path, domain)
    output = tmp_path / "export" / "record.json"
    sentinel = "another writer or a partial export"
    module = importlib.import_module(f"baibai_engine.cli.{domain.replace('-', '_')}")
    original_write = module.write_text_new
    if failure == "blocked_parent":
        output.parent.write_text(sentinel)
    else:

        def fail_after_commit(path, content):
            # Only the export boundary is faulted; save uses the real service/SQLite transaction.
            assert _count(db, table) == 1
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(sentinel)
            if failure == "raced_output":
                return original_write(path, content)
            raise OSError("synthetic-private-os-detail")

        monkeypatch.setattr(module, "write_text_new", fail_after_commit)
    args = ["investment-judgment", domain, "save", str(draft), "--db", str(db)]

    assert main([*args, "--out", str(output)]) == (3 if failure == "partial_io" else 4)
    result = capsys.readouterr()
    assert _count(db, table) == 1
    acknowledgment = json.loads(result.out)
    assert set(acknowledgment) == {"status", "reference", "recorded_at"}
    assert acknowledgment["status"] == "saved_output_failed"
    assert acknowledgment["reference"][identity] == "r1"
    assert "record saved; output export failed" in result.err
    assert "get" in result.err
    assert PRIVATE not in result.out + result.err
    assert "synthetic-private-os-detail" not in result.out + result.err
    assert (output.parent if failure == "blocked_parent" else output).read_text() == sentinel

    assert (
        main(
            [
                "investment-judgment",
                domain,
                "get",
                f"--{identity.replace('_', '-')}",
                "r1",
                "--sha256",
                acknowledgment["reference"]["payload_sha256"],
                "--db",
                str(db),
            ]
        )
        == 0
    )
    retrieved = json.loads(capsys.readouterr().out)
    record = retrieved if domain == "company-research" else retrieved["record"]
    assert record["reference"] == acknowledgment["reference"]
    assert record["recorded_at"] == acknowledgment["recorded_at"]
    assert PRIVATE in json.dumps(record)
    # If a caller does retry the same request, it remains the same immutable record and clock.
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out) == record
    assert _count(db, table) == 1


@pytest.mark.parametrize("domain", ["company-research", "judgment-retrospective"])
@pytest.mark.parametrize("failure", ["existing_output", "invalid_draft"])
def test_failure_before_save_never_claims_a_committed_record(tmp_path, capsys, domain, failure):
    db, draft, table, _ = _case(tmp_path, domain)
    output = tmp_path / "export.json"
    before = db.read_bytes() if db.exists() else None
    if failure == "existing_output":
        output.write_text("keep-existing")
    else:
        draft.write_text("{}")

    assert main(
        [
            "investment-judgment",
            domain,
            "save",
            str(draft),
            "--db",
            str(db),
            "--out",
            str(output),
        ]
    ) == (4 if failure == "existing_output" else 3)
    result = capsys.readouterr()
    assert result.out == ""
    assert "record saved" not in result.err
    assert _count(db, table) == 0
    assert (db.read_bytes() if db.exists() else None) == before
    if failure == "existing_output":
        assert output.read_text() == "keep-existing"
    else:
        assert not output.exists()


@pytest.mark.parametrize("domain", ["company-research", "judgment-retrospective"])
def test_read_only_export_failure_never_claims_a_new_save(tmp_path, capsys, domain):
    db, draft, table, identity = _case(tmp_path, domain)
    assert (
        main(
            [
                "investment-judgment",
                domain,
                "save",
                str(draft),
                "--db",
                str(db),
            ]
        )
        == 0
    )
    capsys.readouterr()
    before = db.read_bytes()
    output = tmp_path / "blocked" / "record.json"
    output.parent.write_text("keep-existing")
    assert (
        main(
            [
                "investment-judgment",
                domain,
                "get",
                f"--{identity.replace('_', '-')}",
                "r1",
                "--db",
                str(db),
                "--out",
                str(output),
            ]
        )
        == 4
    )
    result = capsys.readouterr()
    assert result.out == ""
    assert "record saved" not in result.err
    assert _count(db, table) == 1
    assert db.read_bytes() == before
    assert output.parent.read_text() == "keep-existing"


@pytest.mark.parametrize("domain", ["company-research", "judgment-retrospective"])
def test_conflicting_save_keeps_old_record_without_a_saved_acknowledgment(tmp_path, capsys, domain):
    db, draft, table, _ = _case(tmp_path, domain)
    args = ["investment-judgment", domain, "save", str(draft), "--db", str(db)]
    assert main(args) == 0
    capsys.readouterr()
    before = db.read_bytes()
    changed = json.loads(draft.read_text())
    if domain == "company-research":
        changed["document"]["investigated_scope"] += " different content"
    else:
        changed["document"]["lesson"]["rationale"] += " different content"
    draft.write_text(json.dumps(changed))
    output = tmp_path / "record.json"
    assert main([*args, "--out", str(output)]) == 4
    result = capsys.readouterr()
    assert result.out == ""
    assert "record saved" not in result.err
    assert not output.exists()
    assert _count(db, table) == 1
    assert db.read_bytes() == before


@pytest.mark.parametrize("domain", ["company-research", "judgment-retrospective"])
@pytest.mark.parametrize("bad_yaml", ["duplicate", "nested_duplicate", "malformed"])
def test_authored_record_parse_errors_do_not_create_records_or_exports(
    tmp_path, capsys, domain, bad_yaml
):
    db, draft, table, identity = _case(tmp_path, domain)
    before = db.read_bytes() if db.exists() else None
    out = tmp_path / "export.json"
    text = draft.read_text(encoding="utf-8")
    if bad_yaml == "duplicate":
        text = f'{{"{identity}": "{PRIVATE}",' + text[1:]
    elif bad_yaml == "nested_duplicate":
        # Same-value duplicate under a legitimate draft key has no single authoring interpretation.
        key = "investigated_scope" if domain == "company-research" else "milestone"
        value = json.dumps(json.loads(text)["document"][key], ensure_ascii=False)
        text = text.replace('"document": {', f'"document": {{"{key}": {value},', 1)
    else:
        text = f"{PRIVATE}: [\n"
    draft.write_text(text, encoding="utf-8")
    assert (
        main(
            ["investment-judgment", domain, "save", str(draft), "--db", str(db), "--out", str(out)]
        )
        == 3
    )
    result = capsys.readouterr()
    assert result.out == ""
    expected = "research draft" if domain == "company-research" else "retrospective draft"
    assert result.err == f"error: invalid {expected} YAML\n"
    assert PRIVATE not in result.err
    assert _count(db, table) == 0
    assert (db.read_bytes() if db.exists() else None) == before
    assert not out.exists()
    assert draft.read_text(encoding="utf-8") == text
