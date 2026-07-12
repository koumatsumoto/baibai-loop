from __future__ import annotations

from pathlib import Path

from tools.drift import (
    check_clean_lineage,
    check_cli_doc,
    check_duplicate_constants,
    check_legacy_semantics,
    check_markdown_links,
)

ROOT = Path(__file__).resolve().parents[1]


def test_repository_passes_all_drift_gates() -> None:
    assert check_markdown_links.check(ROOT) == []
    assert check_cli_doc.check(ROOT) == []
    assert check_legacy_semantics.check(ROOT) == []
    assert check_duplicate_constants.check(ROOT) == []
    assert check_clean_lineage.check(ROOT) == []


def test_markdown_link_gate_rejects_missing_target(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("[broken](./missing.md)\n", encoding="utf-8")
    assert check_markdown_links.check(tmp_path) == [
        "README.md: missing Markdown link target ./missing.md"
    ]


def test_cli_doc_gate_rejects_missing_and_phantom_commands(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n[project.scripts]\nbaibai-loop-real = 'pkg:main'\n", encoding="utf-8"
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "architecture.md").write_text(
        "### CLI\n`baibai-loop-phantom`\n### Schema\n", encoding="utf-8"
    )
    assert check_cli_doc.check(tmp_path) == [
        "docs/architecture.md: missing CLI baibai-loop-real",
        "docs/architecture.md: unknown CLI baibai-loop-phantom",
    ]


def test_legacy_semantics_gate_rejects_active_record(tmp_path: Path) -> None:
    path = tmp_path / "records" / "04-position" / "bad.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("note: old cap 8% / 45%\n", encoding="utf-8")
    assert check_legacy_semantics.check(tmp_path) == [
        "records/04-position/bad.yaml: legacy semantics 'old cap 8% / 45%'"
    ]


def test_duplicate_policy_constant_gate_rejects_skill_copy(tmp_path: Path) -> None:
    policy = tmp_path / "src/baibai_loop/position/policy.py"
    policy.parent.mkdir(parents=True)
    policy.write_text(
        (ROOT / "src/baibai_loop/position/policy.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    path = tmp_path / ".claude" / "skills" / "demo" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("cap is 6%\n", encoding="utf-8")
    assert check_duplicate_constants.check(tmp_path) == [
        ".claude/skills/demo/SKILL.md: duplicated policy literal '6%'"
    ]


def test_clean_lineage_gate_rejects_ignored_local_source(tmp_path: Path) -> None:
    path = tmp_path / "records" / "03-thesis" / "packet.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "input_snapshot:\n  sources:\n    - dataset: data/screening/market.sqlite\n",
        encoding="utf-8",
    )
    assert check_clean_lineage.check(tmp_path) == [
        "records/03-thesis/packet.yaml: non-clean-checkout source ref data/screening/market.sqlite"
    ]
