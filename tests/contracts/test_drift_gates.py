from __future__ import annotations

import argparse
import os
import shlex
from pathlib import Path

import pytest
from tools.quality.drift import (
    check_cli_help,
    check_documented_commands,
    check_markdown_links,
    check_readmodel_contract,
    check_repository_paths,
)

from baibai_engine.cli.operation import build_parser as build_operation_parser
from baibai_jobs.maintenance.market.cli import build_parser as build_market_parser
from baibai_web.contracts_export import generated_files

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("state", ["current", "stale", "missing"])
def test_readmodel_gate_checks_without_writing_artifacts(tmp_path, monkeypatch, state):
    files = generated_files(tmp_path)
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    target = next(iter(files))
    if state == "stale":
        target.write_text("stale", encoding="utf-8")
    elif state == "missing":
        target.unlink()
    before = {path: path.read_bytes() if path.exists() else None for path in files}
    monkeypatch.setattr(check_readmodel_contract, "_ROOT", tmp_path)

    assert check_readmodel_contract.main() == (0 if state == "current" else 1)
    assert {path: path.read_bytes() if path.exists() else None for path in files} == before


def test_repository_path_gate_rejects_runtime_literal_outside_owner(tmp_path: Path) -> None:
    path = tmp_path / "jobs/src/baibai_jobs/job.py"
    path.parent.mkdir(parents=True)
    path.write_text('DB = "stores/application/baibai.sqlite"\n', encoding="utf-8")
    assert check_repository_paths.check(tmp_path) == [
        (
            "jobs/src/baibai_jobs/job.py:1: repository path "
            "'stores/application/baibai.sqlite' belongs in "
            "engine/src/baibai_engine/storage/paths.py"
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


@pytest.mark.parametrize("ignored", ["node_modules", "dist", ".venv", "__pycache__"])
def test_markdown_link_gate_does_not_walk_ignored_trees(tmp_path, monkeypatch, ignored):
    web = tmp_path / "web"
    ignored_tree = web / "frontend" / ignored / "nested"
    ignored_tree.mkdir(parents=True)
    (ignored_tree / "README.md").write_text("[excluded](missing.md)\n")
    owned = web / "frontend" / "src" / "guide.md"
    owned.parent.mkdir()
    owned.write_text("[owned](missing.md)\n")
    scandir = os.scandir

    def checked(path):
        assert ignored not in Path(path).relative_to(tmp_path).parts
        return scandir(path)

    monkeypatch.setattr(os, "scandir", checked)
    assert check_markdown_links.check(tmp_path) == [
        "web/frontend/src/guide.md: missing Markdown link target missing.md"
    ]


def test_markdown_link_gate_preserves_scan_order_and_symlink_scope(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    target = tmp_path / "support"
    target.mkdir()
    (target / "README.md").write_text("[outside](missing.md)\n")
    (docs / "linked-directory").symlink_to(target, target_is_directory=True)
    (docs / "b.md").symlink_to(target / "README.md")
    (docs / "a.md").write_text("[owned](missing.md)\n")
    (docs / "dangling.md").symlink_to(target / "missing.md")

    assert check_markdown_links.check(tmp_path) == [
        "docs/a.md: missing Markdown link target missing.md",
        "docs/b.md: missing Markdown link target missing.md",
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


def test_markdown_link_gate_reads_each_document_once_across_links_and_anchors(
    tmp_path, monkeypatch
):
    docs = tmp_path / "docs"
    docs.mkdir()
    outside = tmp_path / "support"
    outside.mkdir()
    home, guide, target = tmp_path / "README.md", docs / "guide.md", outside / "target.md"
    home.write_text(
        "# Home\n[a](docs/guide.md#scope) [b](/docs/guide.md#scope) "
        "[c](docs/../docs/guide.md#scope) [self](#home) [extra](support/target.md#extra)\n"
    )
    guide.write_text(
        "# Guide\n## Scope\n[home](../README.md#home) [self](guide.md#scope) "
        "[extra](../support/target.md#extra)\n"
    )
    # An anchor target outside scan roots is read, without expanding the scan scope.
    target.write_text("# Extra\n[outside](missing.md)\n")
    reads = []
    read_text = Path.read_text

    def tracked(path, *args, **kwargs):
        reads.append(path.resolve())
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", tracked)
    assert check_markdown_links.check(tmp_path) == []
    assert sorted(reads) == sorted(path.resolve() for path in (home, guide, target))


def test_markdown_link_gate_rereads_changed_anchor_target_on_next_check(tmp_path):
    outside = tmp_path / "support"
    outside.mkdir()
    target = outside / "target.md"
    target.write_text("# Original\n")
    (tmp_path / "README.md").write_text("[x](support/target.md#original)\n")
    assert check_markdown_links.check(tmp_path) == []
    target.write_text("# Changed\n")
    assert check_markdown_links.check(tmp_path) == [
        "README.md: missing Markdown anchor support/target.md#original"
    ]


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
    _skill(
        tmp_path,
        "```bash\nuv run baibai-jobs maintain-market extract-edinet-metrics\n```\n",
    )
    assert check_documented_commands.check(tmp_path) == [
        (
            ".agents/skills/demo/SKILL.md: "
            "`uv run baibai-jobs maintain-market extract-edinet-metrics` omits required --asof"
        )
    ]


def test_documented_command_gate_accepts_a_complete_command(tmp_path: Path) -> None:
    _skill(
        tmp_path,
        "```bash\nuv run baibai-jobs maintain-market extract-edinet-metrics --asof <A>\n```\n",
    )
    assert check_documented_commands.check(tmp_path) == []


@pytest.mark.parametrize("options", ["--asof", "--asof --lookback-days 540", "--asof -- <A>"])
def test_documented_command_gate_rejects_a_named_option_with_no_value(tmp_path, options):
    args = f"extract-edinet-metrics {options}"
    command = f"uv run baibai-jobs maintain-market {args}"
    _skill(tmp_path, f"```bash\n{command}\n```\n")

    with pytest.raises(SystemExit) as result:
        build_market_parser().parse_args(shlex.split(args))
    assert result.value.code == 2
    assert check_documented_commands.check(tmp_path) == [
        f".agents/skills/demo/SKILL.md: `{command}` omits value for --asof"
    ]


@pytest.mark.parametrize("other_option", ["--format json", "'--format=json with spaces'"])
def test_documented_command_gate_rejects_a_parent_option_missing_its_value(tmp_path, other_option):
    args = f"--db {other_option} show"
    _skill(tmp_path, f"```bash\nuv run baibai-engine operation {args}\n```\n")

    with pytest.raises(SystemExit) as result:
        build_operation_parser().parse_args(shlex.split(args))
    assert result.value.code == 2
    assert check_documented_commands.check(tmp_path) == [
        ".agents/skills/demo/SKILL.md: baibai-engine operation: omits value for --db"
    ]


@pytest.mark.parametrize(
    ("entry", "build_parser", "args", "path_attribute"),
    [
        ("baibai-engine operation", build_operation_parser, "--db {path} --format json show", "db"),
        (
            "baibai-jobs maintain-market",
            build_market_parser,
            "extract-edinet-metrics --asof 2026-11-19 --sqlite-path {path}",
            "sqlite_path",
        ),
    ],
)
def test_documented_command_gate_matches_argparse_for_quoted_dash_leading_paths(
    tmp_path, entry, build_parser, args, path_attribute
):
    path = "- archive.sqlite"
    args = args.format(path=shlex.quote(path))
    _skill(tmp_path, f"```bash\nuv run {entry} {args}\n```\n")

    parsed = build_parser().parse_args(shlex.split(args))
    assert Path(getattr(parsed, path_attribute)) == Path(path)
    assert check_documented_commands.check(tmp_path) == []


@pytest.mark.parametrize(
    ("relative", "args"),
    [
        ("docs/reference/screening-runtime.md", "operation --db <APP> --format json show"),
        (
            "docs/reference/trade-proposal.md",
            "investment-judgment trade-proposal-compare-holdings cases.yaml --draft proposal.yaml",
        ),
    ],
)
def test_documented_command_gate_checks_the_reference_owner_examples(tmp_path, relative, args):
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    command = f"uv run baibai-engine {args}"
    path.write_text(f"```bash\n{command}\n```\n")
    assert check_documented_commands.check(tmp_path) == []

    path.write_text(f"```bash\n{command} --removed-option\n```\n")
    assert check_documented_commands.check(tmp_path) == [
        f"{relative}: `{command} --removed-option` has unknown options --removed-option"
    ]


def _transfer_script(root: Path, *names: str) -> None:
    """A stand-in for `r2_transfer.sh` carrying only the dispatch the gate reads."""

    path = root / "jobs" / "scripts" / "r2_transfer.sh"
    path.parent.mkdir(parents=True, exist_ok=True)
    branches = "".join(f"  {name})\n    :\n    ;;\n" for name in names)
    path.write_text(
        f'case "${{1:-}}" in\n{branches}  *)\n    usage\n    exit 2\n    ;;\nesac\n',
        encoding="utf-8",
    )


def test_documented_command_gate_rejects_an_absent_transfer_subcommand(tmp_path: Path) -> None:
    _transfer_script(tmp_path, "upload-portfolio-views")
    _skill(tmp_path, "```bash\njobs/scripts/r2_transfer.sh unknown-command <dir>\n```\n")

    assert check_documented_commands.check(tmp_path) == [
        (
            ".agents/skills/demo/SKILL.md: `jobs/scripts/r2_transfer.sh unknown-command <dir>` "
            "names no r2_transfer subcommand (unknown-command)"
        )
    ]


def test_documented_command_gate_accepts_a_transfer_subcommand_the_script_dispatches(
    tmp_path: Path,
) -> None:
    _transfer_script(tmp_path, "pull-machine", "upload-portfolio-views")
    _skill(
        tmp_path,
        "```bash\njobs/scripts/r2_transfer.sh pull-machine\n```\n"
        "`jobs/scripts/r2_transfer.sh upload-portfolio-views <dir>` で出す。\n",
    )

    assert check_documented_commands.check(tmp_path) == []


def test_documented_command_gate_reads_the_dispatch_the_repository_script_runs() -> None:
    """The executable branches, not the usage text, are authoritative."""

    names = check_documented_commands.transfer_subcommands(ROOT)

    assert {"pull-machine", "hydrate-market-for-daily", "pull-portfolio-market"} <= names
    assert {"push-machine", "publish-lake", "restore-machine-receipt", "seed-all"}.isdisjoint(names)
    assert "unknown-command" not in names
    assert "*" not in names


def test_documented_command_gate_resolves_batch_modules(tmp_path: Path) -> None:
    """A batch module with no required option is a complete documented command."""

    _skill(tmp_path, "```bash\nuv run python -m baibai_jobs.observability.discord\n```\n")

    assert check_documented_commands.check(tmp_path) == []


def test_documented_command_gate_reads_inline_code_spans(tmp_path: Path) -> None:
    """手順の途中に 1 行で置かれた command も、fenced block と同じ実行対象である。"""

    _skill(
        tmp_path,
        "指標は `uv run baibai-engine technical-analysis show --asof <A>` で確認する。\n",
    )
    assert check_documented_commands.check(tmp_path) == [
        (
            ".agents/skills/demo/SKILL.md: "
            "`uv run baibai-engine technical-analysis show --asof <A>` omits required --ticker"
        )
    ]


def test_documented_command_gate_rejects_an_unknown_engine_domain(
    tmp_path: Path,
) -> None:
    _skill(
        tmp_path,
        "```bash\nuv run baibai-engine unknown-domain show\n```\n",
    )
    assert check_documented_commands.check(tmp_path) == [
        ".agents/skills/demo/SKILL.md: baibai-engine: unknown domain unknown-domain"
    ]


def test_documented_command_gate_joins_backslash_continuations(tmp_path: Path) -> None:
    _skill(
        tmp_path,
        "```bash\nuv run baibai-engine technical-analysis show \\\n"
        "  --ticker 2331 --asof <A>\n```\n",
    )
    assert check_documented_commands.check(tmp_path) == []


def test_markdown_link_gate_scans_colocated_domain_documents(tmp_path: Path) -> None:
    domain = tmp_path / "engine/src/baibai_engine/machine_analysis/screening"
    domain.mkdir(parents=True)
    (domain / "README.md").write_text("[src](gone.py)\n", encoding="utf-8")
    assert check_markdown_links.check(tmp_path) == [
        "engine/src/baibai_engine/machine_analysis/screening/README.md: missing Markdown link target gone.py"
    ]


@pytest.mark.parametrize(
    ("options", "valid"),
    [("--mirror stores", False), ("--root <MIRROR>", True), ("--root=<MIRROR>", True), ("", True)],
)
def test_operations_inventory_rejects_unknown_leaf_options(
    tmp_path: Path, options: str, valid: bool
) -> None:
    path = tmp_path / "jobs/OPERATIONS.md"
    path.parent.mkdir(parents=True)
    path.write_text(f"```bash\nuv run baibai-jobs snapshots inventory {options}\n```\n")
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
    argv = ["--db=<DB>", "run", "-t", "2331", "--ticker=0001", "--offset", "-.5e+1"]
    assert parser.parse_args(argv).offset == "-.5e+1"
    resolved, rest = check_documented_commands._descend(parser, "example", argv)
    assert check_documented_commands._unknown_options(resolved, rest) == []
    assert check_documented_commands._missing_required(resolved, rest) == []
    assert check_documented_commands._missing_option_values(resolved, rest) == []
    assert check_documented_commands._unknown_options(resolved, ["--typo"]) == ["--typo"]


@pytest.mark.parametrize(
    "argv", [["--pair", "<ID>"], ["--pair", "<ID>", "--"], ["--pair=<ID>", "<SHA>"]]
)
def test_fixed_arity_option_has_every_value(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", nargs=2)
    parser.add_argument("--other")
    assert (
        check_documented_commands._missing_option_values(parser, ["--pair", "<ID>", "<SHA>"]) == []
    )
    option_as_value = ["--pair", "<ID>", "--other"]
    assert parser.parse_args(option_as_value).pair == ["<ID>", "--other"]
    assert check_documented_commands._missing_option_values(parser, option_as_value) == []
    with pytest.raises(SystemExit) as result:
        parser.parse_args(argv)
    assert result.value.code == 2
    assert check_documented_commands._missing_option_values(parser, argv) == ["--pair"]


@pytest.mark.parametrize(
    ("command", "expected_error"),
    [
        ("baibai-engine market-context reading --asof <ASOF>", None),
        ("baibai-engine market-context reading", "omits required --asof"),
        ("baibai-engine market-context reading -- --asof <ASOF>", "omits required --asof"),
        (
            "baibai-engine market-context reading --asof <ASOF> --typo",
            "has unknown options --typo",
        ),
        ("baibai-jobs missing", "baibai-jobs: unknown command missing"),
    ],
)
def test_documented_commands_reach_delegated_leaf_options(
    tmp_path: Path, command: str, expected_error: str | None
) -> None:
    _skill(tmp_path, f"```bash\nuv run {command}\n```\n")
    errors = check_documented_commands.check(tmp_path)
    if expected_error is None:
        assert errors == []
    else:
        assert len(errors) == 1
        assert expected_error in errors[0]


@pytest.mark.parametrize(
    ("command", "valid"),
    [
        ("baibai-jobs snapshots hydrate --mirror <MIRROR> --store <STORE>", True),
        ("baibai-jobs snapshots hydrate --store <STORE>", False),
    ],
)
def test_active_runbook_checks_lake_write_parser(tmp_path: Path, command: str, valid: bool) -> None:
    path = tmp_path / "jobs/OPERATIONS.md"
    path.parent.mkdir()
    path.write_text(f"```bash\nuv run {command}\n```\n")
    assert (not check_documented_commands.check(tmp_path)) is valid


@pytest.mark.parametrize("shell", ["|python -c 'pass'", " > /tmp/output", " && false", " $(false)"])
def test_documented_shell_composition_is_not_interpreted(tmp_path: Path, shell: str) -> None:
    path = tmp_path / "jobs/OPERATIONS.md"
    path.parent.mkdir()
    path.write_text(f"```bash\nuv run baibai-jobs snapshots inventory{shell}\n```\n")
    assert check_documented_commands.check(tmp_path) == []


@pytest.mark.parametrize(
    "method_path",
    [
        "engine/src/baibai_engine/machine_analysis/models/market_context/reading/rules/2099-01-01T000000+0900.yaml",
    ],
)
def test_repository_path_gate_tracks_dated_methods_without_pin_updates(tmp_path, method_path):
    owner = tmp_path / "engine/src/baibai_engine/storage/paths.py"
    owner.parent.mkdir(parents=True)
    owner.write_text(f"METHOD = {method_path!r}\n", encoding="utf-8")
    assert check_repository_paths.check(tmp_path) == []
    consumer = tmp_path / "jobs/src/baibai_jobs/job.py"
    consumer.parent.mkdir(parents=True)
    consumer.write_text(f"METHOD = {method_path!r}\n", encoding="utf-8")
    errors = check_repository_paths.check(tmp_path)
    assert len(errors) == 1
    assert repr(method_path) in errors[0]
    assert "belongs in engine/src/baibai_engine/storage/paths.py" in errors[0]


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


@pytest.mark.parametrize(
    ("command", "valid"),
    [
        ("acquire-economic groups", True),
        ("acquire-economic groups --typo", False),
        ("maintain-market extract-edinet-metrics --asof <DATE>", True),
        ("maintain-market extract-edinet-metrics", False),
    ],
)
def test_documented_jobs_command_resolves_lazy_module_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, valid: bool
) -> None:
    # Jobs resolves its own command table, without the engine's nested parser stubs.
    monkeypatch.setattr(check_documented_commands, "DELEGATED_GROUPS", {})
    _skill(tmp_path, f"```bash\nuv run baibai-jobs {command}\n```\n")
    assert (not check_documented_commands.check(tmp_path)) is valid
