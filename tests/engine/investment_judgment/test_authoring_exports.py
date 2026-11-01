"""新規exportの衝突は既存の下書きや公開済み正本を壊さない。"""

import json
from pathlib import Path

import pytest
from tests.helpers.assessment_v1 import NOW, assessment_id, pair_payload

from baibai_engine.cli import company_research, investment_judgment, judgment_retrospective
from baibai_engine.investment_judgment.stock_assessment.store import AssessmentStoreService


@pytest.mark.parametrize("command", ["company", "retrospective", "proposal"])
@pytest.mark.parametrize("collision", ["existing", "dangling_symlink", "concurrent"])
def test_authoring_export_never_replaces_an_existing_path(
    tmp_path, monkeypatch, capsys, command, collision
):
    output = tmp_path / "draft.json"
    missing_target = tmp_path / "missing-target"
    called = []

    def scaffold(**_kwargs):
        called.append(True)
        if collision == "concurrent":
            output.write_text("another author's draft", encoding="utf-8")
        return {"synthetic": "draft"}

    if collision == "existing":
        output.write_text("another author's draft", encoding="utf-8")
    elif collision == "dangling_symlink":
        output.symlink_to(missing_target)
    if command == "company":
        monkeypatch.setattr(company_research, "scaffold_research", scaffold)
        args = ["company-research", "scaffold", "--ticker", "1234", "--asof", "2026-10-14"]
    elif command == "retrospective":
        monkeypatch.setattr(judgment_retrospective, "scaffold_retrospective", scaffold)
        args = [
            "judgment-retrospective",
            "scaffold",
            "--assessment-id",
            assessment_id(),
            "--asof",
            "2026-10-14",
        ]
    else:
        monkeypatch.setattr(
            "baibai_engine.investment_judgment.trade_proposal.scaffold.scaffold_proposal", scaffold
        )
        args = [
            "trade-proposal-scaffold",
            "--operation-id",
            "synthetic-operation",
            "--proposal-id",
            "synthetic-proposal",
        ]
    assert (
        investment_judgment.main(
            [*args, "--db", str(tmp_path / "app"), "--out", str(output)], now=NOW
        )
        == 4
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "already exists" in captured.err
    assert called == ([True] if collision == "concurrent" else [])
    if collision == "dangling_symlink":
        assert output.is_symlink()
        assert output.readlink() == missing_target
        assert not missing_target.exists()
    else:
        assert output.read_text(encoding="utf-8") == "another author's draft"


@pytest.mark.parametrize("command", ["company-research", "judgment-retrospective"])
def test_failed_export_preserves_successful_authoritative_save(
    tmp_path, monkeypatch, capsys, command
):
    app, source, output = tmp_path / "app", tmp_path / "input.json", tmp_path / "export.json"
    if command == "company-research":
        owner = company_research
        authored = {
            "research_id": "synthetic-research",
            "document": {
                "ticker": "1234",
                "as_of": "2026-10-14",
                "investigated_scope": "追加の原典を確認中",
                "unresolved_questions": ["継続利益を確認する"],
            },
        }
        get_args = ["get", "--research-id", authored["research_id"]]
    else:
        owner = judgment_retrospective
        document, review = pair_payload()
        pair = AssessmentStoreService(app, clock=lambda: NOW).publish_reviewed_assessment(
            assessment_id(), document, review
        )
        authored = {
            "retrospective_id": "synthetic-retrospective",
            "document": {
                "ticker": "1234",
                "as_of": "2026-10-14",
                "milestone": "確認の途中経過",
                "origin": {
                    "assessment_id": pair.assessment_id,
                    "core_sha256": pair.core_sha256,
                    "review_id": pair.review.review_id,
                },
                "lesson": {"next_time": "追加原典を読む", "rationale": "根拠をまだ確認できない"},
            },
        }
        get_args = ["get", "--retrospective-id", authored["retrospective_id"]]
    source.write_text(json.dumps(authored), encoding="utf-8")
    before = source.read_bytes()

    def failed_export(_path: Path, _content: str):
        raise OSError("synthetic export failure")

    monkeypatch.setattr(owner, "write_text_new", failed_export)
    assert (
        investment_judgment.main(
            [command, "save", str(source), "--db", str(app), "--out", str(output)], now=NOW
        )
        == 3
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "synthetic export failure" in captured.err
    assert source.read_bytes() == before
    assert not output.exists()
    assert investment_judgment.main([command, *get_args, "--db", str(app)], now=NOW) == 0
    saved = json.loads(capsys.readouterr().out)
    assert (
        investment_judgment.main([command, "save", str(source), "--db", str(app)], now=NOW) == 0
    )
    retried = json.loads(capsys.readouterr().out)
    assert retried == (saved["record"] if command == "judgment-retrospective" else saved)
