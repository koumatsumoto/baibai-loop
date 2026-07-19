from __future__ import annotations

from pathlib import Path

from tools.drift import (
    check_clean_lineage,
    check_cli_doc,
    check_duplicate_constants,
    check_legacy_semantics,
    check_markdown_links,
    check_skill_inventory,
)

ROOT = Path(__file__).resolve().parents[1]


def test_repository_passes_all_drift_gates() -> None:
    assert check_markdown_links.check(ROOT) == []
    assert check_cli_doc.check(ROOT) == []
    assert check_legacy_semantics.check(ROOT) == []
    assert check_duplicate_constants.check(ROOT) == []
    assert check_clean_lineage.check(ROOT) == []
    assert check_skill_inventory.check(ROOT) == []


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


def test_legacy_semantics_gate_rejects_obsolete_skill_instruction(tmp_path: Path) -> None:
    path = tmp_path / ".agents" / "skills" / "demo" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("Use ai-value-bargain-selection.\n", encoding="utf-8")
    assert check_legacy_semantics.check(tmp_path) == [
        ".agents/skills/demo/SKILL.md: obsolete operation instruction 'ai-value-bargain-selection'"
    ]


def test_duplicate_policy_constant_gate_rejects_skill_copy(tmp_path: Path) -> None:
    policy = tmp_path / "src/baibai_engine/position/policy.py"
    policy.parent.mkdir(parents=True)
    policy.write_text(
        (ROOT / "src/baibai_engine/position/policy.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    path = tmp_path / ".claude" / "skills" / "demo" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("cap is 6%\n", encoding="utf-8")
    assert check_duplicate_constants.check(tmp_path) == [
        ".claude/skills/demo/SKILL.md: duplicated policy literal '6%'"
    ]


def test_duplicate_policy_constant_gate_rejects_japanese_monthly_contribution(
    tmp_path: Path,
) -> None:
    policy = tmp_path / "src/baibai_engine/position/policy.py"
    policy.parent.mkdir(parents=True)
    policy.write_text(
        (ROOT / "src/baibai_engine/position/policy.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    path = tmp_path / "docs" / "copied-policy.md"
    path.parent.mkdir()
    for literal in ("40万", "40万円"):
        path.write_text(f"月{literal}を拠出する\n", encoding="utf-8")
        matched_literal = "月40万" if literal == "40万" else literal
        assert check_duplicate_constants.check(tmp_path) == [
            f"docs/copied-policy.md: duplicated policy literal {matched_literal!r}"
        ]
    path.write_text("毎月 40万を拠出する\n", encoding="utf-8")
    assert check_duplicate_constants.check(tmp_path) == [
        "docs/copied-policy.md: duplicated policy literal '毎月 40万'"
    ]


def test_duplicate_policy_constant_gate_allows_other_japanese_quantities(
    tmp_path: Path,
) -> None:
    policy = tmp_path / "src/baibai_engine/position/policy.py"
    policy.parent.mkdir(parents=True)
    policy.write_text(
        (ROOT / "src/baibai_engine/position/policy.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    canonical = tmp_path / "docs" / "portfolio-management.md"
    canonical.parent.mkdir()
    canonical.write_text("月40万円、通常20〜30万円\n", encoding="utf-8")
    path = tmp_path / "docs" / "other-quantities.md"
    path.write_text(
        "0.40万円、140万円、40万人、40万株、40万件、40万台、40万個、40万ドル、40万票、40万社、"
        "40万トン、20〜30万件\n",
        encoding="utf-8",
    )
    assert check_duplicate_constants.check(tmp_path) == []


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


def test_skill_inventory_gate_rejects_copies_and_missing_skills(tmp_path: Path) -> None:
    canonical = tmp_path / ".agents" / "skills"
    claude = tmp_path / ".claude" / "skills"
    canonical.mkdir(parents=True)
    claude.mkdir(parents=True)
    (canonical / "decision-cycle").mkdir()
    (claude / "decision-cycle").mkdir()
    errors = check_skill_inventory.check(tmp_path)
    assert any("expected exact inventory" in error for error in errors)
    assert any("must be a relative symlink" in error for error in errors)


def test_skill_inventory_gate_rejects_canonical_symlink(tmp_path: Path) -> None:
    skill = tmp_path / ".agents" / "skills" / "decision-cycle"
    external = tmp_path / "external.md"
    skill.mkdir(parents=True)
    external.write_text("secret\n", encoding="utf-8")
    (skill / "SKILL.md").symlink_to(external)
    errors = check_skill_inventory.check(tmp_path)
    assert any("canonical skill tree must not contain symlinks" in error for error in errors)
