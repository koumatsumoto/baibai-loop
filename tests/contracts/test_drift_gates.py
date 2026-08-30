from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from tools.quality.drift import (
    check_cli_doc,
    check_cli_help,
    check_documented_commands,
    check_duplicate_constants,
    check_legacy_semantics,
    check_markdown_links,
    check_repository_paths,
    check_skill_inventory,
)

ROOT = Path(__file__).resolve().parents[2]


def test_repository_path_gate_rejects_runtime_literal_outside_owner(tmp_path: Path) -> None:
    path = tmp_path / "batch/src/baibai_batch/job.py"
    path.parent.mkdir(parents=True)
    path.write_text('DB = "stores/application/baibai.sqlite"\n', encoding="utf-8")
    assert check_repository_paths.check(tmp_path) == [
        (
            "batch/src/baibai_batch/job.py:1: repository path "
            "'stores/application/baibai.sqlite' belongs in "
            "engine/src/baibai_engine/foundation/repository_layout.py"
        )
    ]


def test_markdown_link_gate_rejects_missing_target(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("[broken](./missing.md)\n", encoding="utf-8")
    assert check_markdown_links.check(tmp_path) == [
        "README.md: missing Markdown link target ./missing.md"
    ]


def test_markdown_link_gate_scans_store_docs(tmp_path: Path) -> None:
    stores = tmp_path / "stores"
    stores.mkdir()
    (stores / "README.md").write_text("[broken](./missing.md)\n", encoding="utf-8")
    assert check_markdown_links.check(tmp_path) == [
        "stores/README.md: missing Markdown link target ./missing.md"
    ]


def test_cli_doc_gate_rejects_missing_and_phantom_commands(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n[project.scripts]\nbaibai-engine = 'pkg:main'\n", encoding="utf-8"
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "architecture.md").write_text("## Stable CLI\n`baibai-web`\n", encoding="utf-8")
    assert check_cli_doc.check(tmp_path) == [
        "docs/architecture.md: missing CLI baibai-engine",
        "docs/architecture.md: unknown CLI baibai-web",
    ]


def test_legacy_semantics_gate_rejects_obsolete_skill_instruction(tmp_path: Path) -> None:
    path = tmp_path / ".agents" / "skills" / "demo" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("Use ai-value-bargain-selection.\n", encoding="utf-8")
    assert check_legacy_semantics.check(tmp_path) == [
        ".agents/skills/demo/SKILL.md: obsolete operation instruction 'ai-value-bargain-selection'"
    ]


def test_legacy_semantics_gate_ignores_generated_store_manifests(tmp_path: Path) -> None:
    manifest = tmp_path / "stores" / "lake" / "manifests" / "old.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"dataset":"jquants.earnings_calendar"}\n', encoding="utf-8")

    assert check_legacy_semantics.check(tmp_path) == []


def test_legacy_semantics_gate_rejects_the_retired_lake_comparison_vocabulary(
    tmp_path: Path,
) -> None:
    """The L1 release is the authority, so nothing is compared against a legacy side."""

    path = tmp_path / "docs" / "reference" / "market-lake.md"
    path.parent.mkdir(parents=True)
    path.write_text("releaseは`shadow`または`production` profileを宣言し\n", encoding="utf-8")

    # The word is rejected on its own rather than in fixed combinations: the sentence
    # this gate exists to stop puts `または` between `shadow` and `profile`, so an
    # adjacency rule would let exactly the drift it was written for back in.
    assert check_legacy_semantics.check(tmp_path) == [
        "docs/reference/market-lake.md: obsolete operation instruction 'shadow'"
    ]


def test_legacy_semantics_gate_rejects_the_retired_projection_subsystem(tmp_path: Path) -> None:
    """`lake hydrate` materializes a fixed release; the projection builder is gone."""

    path = tmp_path / "docs" / "reference" / "market-lake.md"
    path.parent.mkdir(parents=True)
    path.write_text("`lake projection build` で射影を作る。\n", encoding="utf-8")

    assert check_legacy_semantics.check(tmp_path) == [
        "docs/reference/market-lake.md: obsolete repository path 'lake projection build'"
    ]


def test_legacy_semantics_gate_keeps_the_live_senses_of_shadow_and_projection(
    tmp_path: Path,
) -> None:
    """The gate must not sweep Tailwind classes or the doctrine sense of projection.

    Rejecting the bare words would make the rule unusable, so only the retired
    combinations are matched and this is what proves the rest still passes.
    """

    doc = tmp_path / "docs" / "doctrine.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(
        "表示物（projection）は canonical ではない。\n",
        encoding="utf-8",
    )
    component = tmp_path / "web" / "frontend" / "src" / "Card.tsx"
    component.parent.mkdir(parents=True)
    component.write_text('const cls = "shadow-sm drop-shadow";\n', encoding="utf-8")

    assert check_legacy_semantics.check(tmp_path) == []


def test_duplicate_policy_constant_gate_rejects_skill_copy(tmp_path: Path) -> None:
    policy = tmp_path / "engine/src/baibai_engine/position/policy.py"
    policy.parent.mkdir(parents=True)
    policy.write_text(
        (ROOT / "engine/src/baibai_engine/position/policy.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    path = tmp_path / ".claude" / "skills" / "demo" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("cap is 10%\n", encoding="utf-8")
    assert check_duplicate_constants.check(tmp_path) == [
        ".claude/skills/demo/SKILL.md: duplicated policy literal '10%'"
    ]


def test_duplicate_policy_constant_gate_allows_marked_unrelated_literal(tmp_path: Path) -> None:
    policy = tmp_path / "engine/src/baibai_engine/position/policy.py"
    policy.parent.mkdir(parents=True)
    policy.write_text(
        (ROOT / "engine/src/baibai_engine/position/policy.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    path = tmp_path / "docs" / "unrelated-rate.md"
    path.parent.mkdir()
    path.write_text(
        "欠損率は 10% である <!-- drift: allow-unrelated-policy-literal -->\n",
        encoding="utf-8",
    )

    assert check_duplicate_constants.check(tmp_path) == []


def test_duplicate_policy_constant_gate_rejects_japanese_monthly_contribution(
    tmp_path: Path,
) -> None:
    policy = tmp_path / "engine/src/baibai_engine/position/policy.py"
    policy.parent.mkdir(parents=True)
    policy.write_text(
        (ROOT / "engine/src/baibai_engine/position/policy.py").read_text(encoding="utf-8"),
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
    policy = tmp_path / "engine/src/baibai_engine/position/policy.py"
    policy.parent.mkdir(parents=True)
    policy.write_text(
        (ROOT / "engine/src/baibai_engine/position/policy.py").read_text(encoding="utf-8"),
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


def test_legacy_semantics_gate_rejects_records_path_in_reference(tmp_path: Path) -> None:
    path = tmp_path / "docs" / "reference" / "demo.md"
    path.parent.mkdir(parents=True)
    path.write_text("設定は `records/` に置く。\n", encoding="utf-8")
    assert check_legacy_semantics.check(tmp_path) == [
        "docs/reference/demo.md: obsolete operation instruction 'records/'"
    ]


def test_legacy_semantics_gate_rejects_old_repository_paths_in_workflows(tmp_path: Path) -> None:
    path = tmp_path / ".github/workflows/ci.yml"
    path.parent.mkdir(parents=True)
    path.write_text("run: python tools/cloud/daily_batch.py\n", encoding="utf-8")
    assert check_legacy_semantics.check(tmp_path) == [
        ".github/workflows/ci.yml: obsolete repository path 'tools/cloud'"
    ]


@pytest.mark.parametrize(
    ("content", "matched"),
    [
        ("cd ui\nnpm test\n", "cd ui\n"),
        ('"build": "npm --prefix ../../ui run build"\n', "../../ui "),
        (
            "See reports/2026-01-01-flat-study.md for evidence.\n",
            "reports/2026-01-01-",
        ),
    ],
)
def test_legacy_semantics_gate_rejects_bare_ui_commands_and_flat_report_paths(
    tmp_path: Path, content: str, matched: str
) -> None:
    path = tmp_path / "docs/reference/demo.md"
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")

    assert check_legacy_semantics.check(tmp_path) == [
        f"docs/reference/demo.md: obsolete repository path {matched!r}"
    ]


@pytest.mark.parametrize("filename", [".gitignore", ".env.sample"])
def test_legacy_semantics_gate_rejects_old_repository_paths_in_root_config(
    tmp_path: Path, filename: str
) -> None:
    path = tmp_path / filename
    path.write_text("data/app/baibai.sqlite\n", encoding="utf-8")
    assert check_legacy_semantics.check(tmp_path) == [
        f"{filename}: obsolete repository path 'data/app'"
    ]


def test_legacy_semantics_gate_rejects_retired_product_names(tmp_path: Path) -> None:
    """The human-facing product name is Baibai Loop, so the old brand forms must not return."""

    path = tmp_path / "docs" / "demo.md"
    path.parent.mkdir(parents=True)
    for retired in ("Baibai App", "Baibai-Loop"):
        path.write_text(f"{retired} の Macro タブを開く。\n", encoding="utf-8")
        assert check_legacy_semantics.check(tmp_path) == [
            f"docs/demo.md: obsolete operation instruction {retired!r}"
        ]


def test_legacy_semantics_gate_allows_lowercase_distribution_name(tmp_path: Path) -> None:
    """`baibai-loop` names the distribution, so only the capitalized brand form is retired."""

    path = tmp_path / "docs" / "demo.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "repository root は baibai-loop で、package は baibai_web。\n", encoding="utf-8"
    )
    assert check_legacy_semantics.check(tmp_path) == []


def test_legacy_semantics_gate_allows_generic_records_word(tmp_path: Path) -> None:
    path = tmp_path / "docs" / "demo.md"
    path.parent.mkdir(parents=True)
    path.write_text("track records と TaskRecord は正当な一般名詞。\n", encoding="utf-8")
    assert check_legacy_semantics.check(tmp_path) == []


def test_markdown_link_gate_rejects_missing_anchor(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "architecture.md").write_text(
        "# Architecture\n\n## Cloud serving layer\n", encoding="utf-8"
    )
    (tmp_path / "README.md").write_text(
        "[x](./docs/architecture.md#automation)\n", encoding="utf-8"
    )
    assert check_markdown_links.check(tmp_path) == [
        "README.md: missing Markdown anchor ./docs/architecture.md#automation"
    ]


def test_markdown_link_gate_accepts_heading_and_explicit_anchor(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "architecture.md").write_text(
        '# Architecture\n\n## Cloud serving layer\n\n<a id="repository-map"></a>\n## Map\n',
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        "[a](./docs/architecture.md#cloud-serving-layer) "
        "[b](./docs/architecture.md#repository-map)\n",
        encoding="utf-8",
    )
    assert check_markdown_links.check(tmp_path) == []


def test_markdown_link_gate_accepts_duplicate_heading_suffix(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# T\n\n## Note\n\n## Note\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "[first](./docs/a.md#note) [second](./docs/a.md#note-1)\n", encoding="utf-8"
    )
    assert check_markdown_links.check(tmp_path) == []


def _parser_with(*, described: bool) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="demo")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("publish", help="publish a draft" if described else None)
    return parser


def test_cli_help_gate_rejects_a_subcommand_without_a_description() -> None:
    assert check_cli_help.undescribed_subcommands("demo", _parser_with(described=False)) == [
        "demo publish: subcommand has no --help description"
    ]


def test_cli_help_gate_accepts_a_described_subcommand() -> None:
    assert check_cli_help.undescribed_subcommands("demo", _parser_with(described=True)) == []


def test_cli_help_gate_rejects_an_unregistered_delegating_stub() -> None:
    parser = argparse.ArgumentParser(prog="demo")
    commands = parser.add_subparsers(dest="command", required=True)
    # 素通し stub は自分の parser を持たない。登録されていなければ、その配下の
    # subcommand は 1 つも検査されないまま gate が緑になる。
    commands.add_parser(
        "group", help="a group whose arguments are parsed elsewhere", add_help=False
    )
    assert check_cli_help.undescribed_subcommands("demo", parser) == [
        "demo group: delegating stub is not registered in DELEGATED_GROUPS"
    ]


def test_cli_help_gate_reaches_subcommands_behind_a_registered_delegation() -> None:
    # 実際の delegation を 1 本辿り、stub の向こう側まで検査が届くことを確かめる。
    context = check_cli_help.build_parser(
        check_cli_help.DELEGATED_GROUPS["baibai-engine macro context"]
    )
    assert "publish" in check_cli_help._subparser_actions(context)[0].choices


def _skill(root: Path, body: str) -> None:
    path = root / ".agents" / "skills" / "demo" / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_documented_command_gate_rejects_a_missing_required_option(tmp_path: Path) -> None:
    _skill(tmp_path, "```bash\nuv run baibai-engine screening extract-edinet-metrics\n```\n")
    assert check_documented_commands.check(tmp_path) == [
        (
            ".agents/skills/demo/SKILL.md: "
            "`uv run baibai-engine screening extract-edinet-metrics` omits required --asof"
        )
    ]


def test_documented_command_gate_accepts_a_complete_command(tmp_path: Path) -> None:
    _skill(
        tmp_path, "```bash\nuv run baibai-engine screening extract-edinet-metrics --asof <A>\n```\n"
    )
    assert check_documented_commands.check(tmp_path) == []


def _transfer_script(root: Path, *names: str) -> None:
    """A stand-in for `r2_transfer.sh` carrying only the dispatch the gate reads."""

    path = root / "batch" / "scripts" / "r2_transfer.sh"
    path.parent.mkdir(parents=True, exist_ok=True)
    branches = "".join(f"  {name})\n    :\n    ;;\n" for name in names)
    path.write_text(
        f'case "${{1:-}}" in\n{branches}  *)\n    usage\n    exit 2\n    ;;\nesac\n',
        encoding="utf-8",
    )


def test_documented_command_gate_rejects_an_absent_transfer_subcommand(tmp_path: Path) -> None:
    """A skill line naming a subcommand the script has no branch for dies on `usage`.

    This is the shape the Materialize section carried for months: `upload-serving`
    instead of `upload-serving-views`, past a gate that only looked at `uv run` lines.
    """

    _transfer_script(tmp_path, "upload-serving-views")
    _skill(tmp_path, "```bash\nbatch/scripts/r2_transfer.sh upload-serving <dir>\n```\n")

    assert check_documented_commands.check(tmp_path) == [
        (
            ".agents/skills/demo/SKILL.md: `batch/scripts/r2_transfer.sh upload-serving <dir>` "
            "names no r2_transfer subcommand (upload-serving)"
        )
    ]


def test_documented_command_gate_accepts_a_transfer_subcommand_the_script_dispatches(
    tmp_path: Path,
) -> None:
    _transfer_script(tmp_path, "pull-machine", "upload-serving-views")
    _skill(
        tmp_path,
        "```bash\nbatch/scripts/r2_transfer.sh pull-machine\n```\n"
        "`batch/scripts/r2_transfer.sh upload-serving-views <dir>` で出す。\n",
    )

    assert check_documented_commands.check(tmp_path) == []


def test_documented_command_gate_reads_the_dispatch_the_repository_script_runs() -> None:
    """Read from the branch that runs, not from the usage line beside it.

    A parser that returned nothing would call every documented line unknown and be
    noticed; one that returned too much would pass the very name this gate exists to
    reject, and nothing else would say so.
    """

    names = check_documented_commands.transfer_subcommands(ROOT)

    assert {"pull-machine", "push-machine", "upload-serving-views"} <= names
    assert "upload-serving" not in names
    assert "*" not in names


def test_documented_command_gate_resolves_batch_modules(tmp_path: Path) -> None:
    """A batch module with no required option is a complete documented command."""

    _skill(tmp_path, "```bash\nuv run python -m baibai_batch.observability.discord\n```\n")

    assert check_documented_commands.check(tmp_path) == []


def test_documented_command_gate_reads_inline_code_spans(tmp_path: Path) -> None:
    """手順の途中に 1 行で置かれた command も、fenced block と同じ実行対象である。"""

    _skill(
        tmp_path, "差分は `uv run python -m baibai_engine.research_watch --asof <A>` で確認する。\n"
    )
    assert check_documented_commands.check(tmp_path) == [
        (
            ".agents/skills/demo/SKILL.md: "
            "`uv run python -m baibai_engine.research_watch --asof <A>` omits required --sqlite-path"
        )
    ]


def test_documented_command_gate_rejects_the_removed_proposal_domain(
    tmp_path: Path,
) -> None:
    _skill(
        tmp_path,
        "```bash\nuv run baibai-engine proposal --db stores/application/baibai.sqlite decide <ID>\n```\n",
    )
    assert check_documented_commands.check(tmp_path) == [
        ".agents/skills/demo/SKILL.md: baibai-engine: unknown domain proposal"
    ]


def test_documented_command_gate_joins_backslash_continuations(tmp_path: Path) -> None:
    _skill(
        tmp_path,
        "```bash\nuv run python -m baibai_engine.research_watch \\\n"
        "  --sqlite-path stores/market/market.sqlite --asof <A>\n```\n",
    )
    assert check_documented_commands.check(tmp_path) == []


def test_markdown_link_gate_scans_dated_reports(tmp_path: Path) -> None:
    """reports は改善サイクルの一次資料なので、実装へ降りる link が切れたら止める。"""

    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "2026-01-01-demo.md").write_text("[src](../src/gone.py)\n", encoding="utf-8")
    assert check_markdown_links.check(tmp_path) == [
        "reports/2026-01-01-demo.md: missing Markdown link target ../src/gone.py"
    ]


def test_legacy_semantics_gate_rejects_a_named_screening_rules_revision(tmp_path: Path) -> None:
    """閾値の正本は現行 revision であって、doc に焼いた file 名ではない。"""

    path = tmp_path / "docs" / "reference" / "demo.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "閾値の正本は `method/screening/rules/2026-07-06T000000+0900.yaml`。\n", encoding="utf-8"
    )
    assert check_legacy_semantics.check(tmp_path) == [
        "docs/reference/demo.md: obsolete operation instruction 'method/screening/rules/2026-07-06'"
    ]


def test_legacy_semantics_gate_allows_the_screening_rules_directory(tmp_path: Path) -> None:
    path = tmp_path / "docs" / "reference" / "demo.md"
    path.parent.mkdir(parents=True)
    path.write_text("閾値の正本は `method/screening/rules/` の現行 revision。\n", encoding="utf-8")
    assert check_legacy_semantics.check(tmp_path) == []


def _policy_copy(root: Path) -> None:
    policy = root / "engine/src/baibai_engine/position/policy.py"
    policy.parent.mkdir(parents=True)
    policy.write_text(
        (ROOT / "engine/src/baibai_engine/position/policy.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )


def test_duplicate_policy_constant_gate_allows_unrelated_amounts(tmp_path: Path) -> None:
    _policy_copy(tmp_path)
    path = tmp_path / "docs" / "unrelated.md"
    path.parent.mkdir(parents=True)
    path.write_text("10万株、10万件、100001 円、8.5 倍、7.0 年\n", encoding="utf-8")
    assert check_duplicate_constants.check(tmp_path) == []
