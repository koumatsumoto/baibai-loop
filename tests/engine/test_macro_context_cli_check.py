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


def test_check_reports_an_unattributed_statement(tmp_path: Path, capsys) -> None:
    """`--check` is where the author reads the verdict before publishing, and this is the
    class the 2026-08-17 run had to catch by hand — it must not be publish-only."""

    payload = macro_context_payload()
    payload["inputs"]["articles"] = [
        {
            "input_id": "a-tsr-bankruptcy-202607",
            "source": "東京商工リサーチ",
            "title": "2026年7月の全国企業倒産",
            "url": "https://www.tsr-net.co.jp/data/detail/1200000_1527.html",
            "published_at": "2026-08-10T14:00:00+09:00",
            "accessed_at": "2026-08-17T12:00:00+09:00",
            "status": "ok",
            "used_for": "倒産件数と販売不振比率の確認",
            "identifiers": ["77.3%"],
        }
    ]
    payload["core"][0]["fact_summary"][0]["summary"] = "販売不振型が 77.3% を占める。"
    draft = _write_draft(tmp_path, payload)
    db_path = tmp_path / "app.sqlite"

    assert context_main(["--db", str(db_path), "publish", str(draft), "--check"]) == 1

    printed = capsys.readouterr().err
    assert "without citing" in printed
    assert "a-tsr-bankruptcy-202607" in printed
    assert not db_path.exists()
