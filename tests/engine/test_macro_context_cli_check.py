from __future__ import annotations

from pathlib import Path

import yaml
from tests.helpers.macro_context import macro_context_payload

from baibai_engine.macro.context.cli import main as context_main


def _write_draft(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "draft.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def test_check_accepts_a_valid_draft_without_creating_the_store(tmp_path: Path, capsys) -> None:
    draft = _write_draft(tmp_path, macro_context_payload())
    db_path = tmp_path / "app.sqlite"

    assert context_main(["--db", str(db_path), "publish", str(draft), "--check"]) == 0

    assert "check: ok" in capsys.readouterr().out
    assert not db_path.exists()


def test_check_reports_a_publication_gate_violation(tmp_path: Path, capsys) -> None:
    # A draft valid as a document but missing the strategy layer must fail the same
    # gate that real publish would raise, while still leaving the store untouched.
    draft = _write_draft(tmp_path, macro_context_payload(strategy_layer=False))
    db_path = tmp_path / "app.sqlite"

    assert context_main(["--db", str(db_path), "publish", str(draft), "--check"]) == 1

    assert "synthesis" in capsys.readouterr().err
    assert not db_path.exists()


def test_check_refuses_expected_head(tmp_path: Path, capsys) -> None:
    # The compare-and-swap belongs to real publish; accepting the flag here would
    # imply the head was verified when nothing read the store.
    draft = _write_draft(tmp_path, macro_context_payload())

    assert (
        context_main(
            [
                "--db",
                str(tmp_path / "app.sqlite"),
                "publish",
                str(draft),
                "--check",
                "--expected-head",
                "macro-context-2026-07-19-base",
            ]
        )
        == 1
    )

    assert "drop --expected-head" in capsys.readouterr().err
