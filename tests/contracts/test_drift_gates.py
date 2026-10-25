from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from tools.quality.drift import (
    check_cli_help,
    check_documented_commands,
    check_markdown_links,
    check_repository_paths,
)

from baibai_engine.foundation.repository_layout import ER_LEVEL_CALIBRATION_CONTEXT_PATH

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
        check_cli_help.DELEGATED_GROUPS["baibai-engine market-context reading"]
    )
    assert "--asof" in context._option_string_actions


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
        tmp_path,
        "差分は `uv run python -m baibai_engine.research.price_watch --asof <A>` で確認する。\n",
    )
    assert check_documented_commands.check(tmp_path) == [
        (
            ".agents/skills/demo/SKILL.md: "
            "`uv run python -m baibai_engine.research.price_watch --asof <A>` omits required --sqlite-path"
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
        "```bash\nuv run python -m baibai_engine.research.price_watch \\\n"
        "  --sqlite-path stores/market/market.sqlite --asof <A>\n```\n",
    )
    assert check_documented_commands.check(tmp_path) == []


def test_markdown_link_gate_scans_colocated_domain_documents(tmp_path: Path) -> None:
    domain = tmp_path / "engine/src/baibai_engine/screening"
    domain.mkdir(parents=True)
    (domain / "README.md").write_text("[src](gone.py)\n", encoding="utf-8")
    assert check_markdown_links.check(tmp_path) == [
        "engine/src/baibai_engine/screening/README.md: missing Markdown link target gone.py"
    ]


@pytest.mark.parametrize(
    ("options", "valid"),
    [("--mirror stores", False), ("--root <MIRROR>", True), ("--root=<MIRROR>", True), ("", True)],
)
def test_operations_inventory_rejects_unknown_leaf_options(
    tmp_path: Path, options: str, valid: bool
) -> None:
    path = tmp_path / "batch/OPERATIONS.md"
    path.parent.mkdir(parents=True)
    path.write_text(f"```bash\nuv run baibai-engine lake inventory {options}\n```\n")
    errors = check_documented_commands.check(tmp_path)
    assert (not errors) is valid
    if not valid:
        assert "unknown options --mirror" in errors[0]


def test_leaf_options_keep_parent_alias_repeatable_negative_and_placeholder_values() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db")
    child = parser.add_subparsers().add_parser("run")
    child.add_argument("--ticker", "-t", action="append", required=True)
    child.add_argument("--offset")
    resolved, rest = check_documented_commands._descend(
        parser,
        "example",
        ["--db", "<DB>", "run", "-t", "2331", "--ticker=0001", "--offset", "-1.5"],
    )
    assert check_documented_commands._unknown_options(resolved, rest) == []
    assert check_documented_commands._missing_required(resolved, rest) == []
    assert check_documented_commands._unknown_options(resolved, ["--typo"]) == ["--typo"]


@pytest.mark.parametrize(
    ("command", "valid"),
    [
        ("analysis run --latest", False),
        ("analysis run --asof <ASOF>", False),
        ("analysis run --typo", False),
        ("analysis missing", False),
        ("missing", False),
    ],
)
def test_documented_batch_commands_use_the_delegated_parser(
    tmp_path: Path, command: str, valid: bool
) -> None:
    _skill(tmp_path, f"```bash\nuv run baibai-batch {command}\n```\n")
    assert (not check_documented_commands.check(tmp_path)) is valid


@pytest.mark.parametrize(
    ("command", "valid"),
    [
        ("baibai-web serve --port 8000", True),
        ("baibai-web serve --not-an-option", False),
        ("baibai-engine lake hydrate --mirror <MIRROR> --store <STORE>", True),
        ("baibai-engine lake hydrate --store <STORE>", False),
    ],
)
def test_active_runbook_checks_web_and_lake_write_parser(
    tmp_path: Path, command: str, valid: bool
) -> None:
    path = tmp_path / "batch/OPERATIONS.md"
    path.parent.mkdir()
    path.write_text(f"```bash\nuv run {command}\n```\n")
    assert (not check_documented_commands.check(tmp_path)) is valid


@pytest.mark.parametrize("shell", ["|python -c 'pass'", " > /tmp/output", " && false", " $(false)"])
def test_documented_shell_composition_is_not_interpreted(tmp_path: Path, shell: str) -> None:
    path = tmp_path / "batch/OPERATIONS.md"
    path.parent.mkdir()
    path.write_text(f"```bash\nuv run baibai-engine lake inventory{shell}\n```\n")
    assert check_documented_commands.check(tmp_path) == []


@pytest.mark.parametrize(
    "method_path",
    [
        "engine/src/baibai_engine/screening/rules/2026-10-16T133000+0900.yaml",
        "engine/src/baibai_engine/screening/rules/2099-01-01T000000+0900.yaml",
        "engine/src/baibai_engine/market_context/reading/rules/2099-01-01T000000+0900.yaml",
    ],
)
def test_repository_path_gate_tracks_dated_methods_without_pin_updates(tmp_path, method_path):
    owner = tmp_path / "engine/src/baibai_engine/foundation/repository_layout.py"
    owner.parent.mkdir(parents=True)
    owner.write_text(f"METHOD = {method_path!r}\n", encoding="utf-8")
    assert check_repository_paths.check(tmp_path) == []
    consumer = tmp_path / "batch/src/baibai_batch/job.py"
    consumer.parent.mkdir(parents=True)
    consumer.write_text(f"METHOD = {method_path!r}\n", encoding="utf-8")
    errors = check_repository_paths.check(tmp_path)
    assert len(errors) == 1
    assert repr(method_path) in errors[0]
    assert "belongs in engine/src/baibai_engine/foundation/repository_layout.py" in errors[0]


def test_repository_path_gate_tracks_published_context_in_domain_package(tmp_path):
    path = tmp_path / "web/backend/src/baibai_web/source.py"
    path.parent.mkdir(parents=True)
    path.write_text(f"CONTEXT = {ER_LEVEL_CALIBRATION_CONTEXT_PATH.as_posix()!r}\n")
    errors = check_repository_paths.check(tmp_path)
    assert len(errors) == 1
    assert repr(ER_LEVEL_CALIBRATION_CONTEXT_PATH.as_posix()) in errors[0]


@pytest.mark.parametrize("target", ["missing.md", "docs/missing.md", "/docs/missing.md"])
def test_markdown_link_gate_checks_bare_and_root_relative_paths(tmp_path, target):
    (tmp_path / "README.md").write_text(f"[broken]({target})\n")
    assert check_markdown_links.check(tmp_path) == [
        f"README.md: missing Markdown link target {target}"
    ]


def test_markdown_link_gate_keeps_external_links_out_of_filesystem_checks(tmp_path):
    (tmp_path / "README.md").write_text(
        "[web](https://example.invalid/a#heading) "
        "[mail](mailto:example@example.invalid) [web](//example.invalid/a)\n"
    )
    assert check_markdown_links.check(tmp_path) == []


def test_markdown_link_gate_checks_colocated_links_and_bare_path_anchors(tmp_path):
    study = tmp_path / "docs" / "reference" / "example"
    study.mkdir(parents=True)
    (study / "plan.md").write_text("# Plan\n\n## Scope\n")
    report = study / "report.md"
    report.write_text("[plan](plan.md#scope)\n")
    assert check_markdown_links.check(tmp_path) == []
    report.write_text("[plan](plan.md#gone)\n")
    assert check_markdown_links.check(tmp_path) == [
        "docs/reference/example/report.md: missing Markdown anchor plan.md#gone"
    ]
