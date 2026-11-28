"""Check the command shapes documented by active skills, operations, and references.

A skill's command lines are executed verbatim every cycle. A missing required option
does not fail loudly once — it fails on every run, and the operator patches it by hand
each time, which is exactly the repeated cost the skill exists to remove.

Python entry points are asked of argparse: which options need tokens, never what
those values mean, so placeholders like `<ASOF>` survive and nothing is type-converted.
The gate does not count positional arguments or interpret the shell.
`jobs/scripts/r2_transfer.sh` has no argparse to
ask, so its subcommand is matched against the `case` the script dispatches in; a name
that is not a branch there dies on the first line with `usage; exit 2`.
"""

from __future__ import annotations

import argparse
import importlib
import re
import shlex
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import cast

# CI は各 gate を module として起動し、共通のparser解決を再利用する。
from tools.quality.drift.check_cli_help import DELEGATED_GROUPS, build_parser

_SCAN_DIRECTORIES = (".agents/skills",)
# Also check readback, holding comparison and the standalone offline Research runbook.
_SCAN_FILES = (
    "jobs/OPERATIONS.md",
    "docs/reference/screening-runtime.md",
    "docs/reference/trade-proposal.md",
    "docs/reference/us-research-mvp.md",
)
_FENCE = re.compile(r"^\s*```")
_INLINE_CODE = re.compile(r"`([^`]+)`")
_COMMENT = re.compile(r"\s+#\s.*$")
_NEGATIVE_NUMBER = re.compile(r"-(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")

_TRANSFER_SCRIPT = Path("jobs/scripts/r2_transfer.sh")
_TRANSFER_PREFIX = f"{_TRANSFER_SCRIPT.as_posix()} "
_COMMAND_PREFIXES = ("uv run ", _TRANSFER_PREFIX)
# The one `case` the script dispatches its argument in. Read from the branch that runs
# rather than from the usage string beside it: the usage line is a second copy of the
# same list and can drift from what the script actually accepts.
_DISPATCH_CASE = re.compile(r'^case "\$\{1:-\}" in$')
_CASE_LABEL = re.compile(r"^ {2}([a-z][a-z0-9-]*)\)")


class _Unresolvable(ValueError):
    """The command line does not name a parser this gate can ask."""


def transfer_subcommands(root: Path) -> frozenset[str]:
    """Every subcommand ``r2_transfer.sh`` branches on."""

    lines = (root / _TRANSFER_SCRIPT).read_text(encoding="utf-8").splitlines()
    start = next((index for index, line in enumerate(lines) if _DISPATCH_CASE.match(line)), None)
    if start is None:
        raise _Unresolvable(f"{_TRANSFER_SCRIPT}: no argument dispatch to read")
    names: set[str] = set()
    for line in lines[start + 1 :]:
        if line.rstrip() == "esac":
            return frozenset(names)
        if (match := _CASE_LABEL.match(line)) is not None:
            names.add(match.group(1))
    raise _Unresolvable(f"{_TRANSFER_SCRIPT}: argument dispatch is unterminated")


def _command_lines(text: str) -> Iterator[str]:
    """Every documented command in the document, fenced or inline."""
    in_fence = False
    pending = ""
    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            pending = ""
            continue
        if in_fence:
            stripped = _COMMENT.sub("", line).rstrip()
            if stripped.endswith("\\"):
                pending += stripped[:-1]
                continue
            candidate = (pending + stripped).strip()
            pending = ""
            if candidate.startswith(_COMMAND_PREFIXES):
                yield candidate
            continue
        for span in _INLINE_CODE.findall(line):
            if span.startswith(_COMMAND_PREFIXES):
                yield _COMMENT.sub("", span).strip()


def _option_arity(parser: argparse.ArgumentParser, token: str) -> tuple[int, bool] | None:
    """Minimum values required and whether argparse accepts option-shaped values."""
    for action in parser._actions:
        if token in action.option_strings:
            nargs = action.nargs
            if isinstance(nargs, int):
                return nargs, True
            return int(nargs in (None, "+")), False
    return None


def _looks_like_option(parser: argparse.ArgumentParser, word: str) -> bool:
    if not word.startswith("-") or word == "-":
        return False
    if " " in word:
        # An exact --option=value still wins over the quoted-space value rule.
        return _option_arity(parser, word.partition("=")[0]) is not None
    return _NEGATIVE_NUMBER.fullmatch(word) is None


def _lacks_values(
    parser: argparse.ArgumentParser, tokens: Sequence[str], index: int, arity: tuple[int, bool]
) -> bool:
    minimum, allow_option_values = arity
    if minimum == 0:
        return False
    if "=" in tokens[index]:
        return minimum > 1  # An explicit attached value is exactly one argument in argparse.
    values = tokens[index + 1 : index + 1 + minimum]
    return (
        len(values) < minimum
        or "--" in values
        or (not allow_option_values and any(_looks_like_option(parser, value) for value in values))
    )


def _subparser_action(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:  # type: ignore[type-arg]  # argparse ships no public parser-tree API
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _descend(
    parser: argparse.ArgumentParser, prog: str, tokens: Sequence[str]
) -> tuple[
    argparse.ArgumentParser,
    list[str],
]:
    """Follow the subcommand path, skipping options that belong to the parents."""
    remaining = list(tokens)
    while (action := _subparser_action(parser)) is not None:
        index = 0
        while index < len(remaining):
            token = remaining[index]
            if not token.startswith("-"):
                break
            name = token.partition("=")[0]
            arity = _option_arity(parser, name)
            if arity is None:
                raise _Unresolvable(f"{prog}: unknown option {token}")
            if _lacks_values(parser, remaining, index, arity):
                raise _Unresolvable(f"{prog}: omits value for {name}")
            index += 1 + (0 if "=" in token else arity[0])
        if index >= len(remaining):
            raise _Unresolvable(f"{prog}: no subcommand")
        name = remaining[index]
        child = action.choices.get(name)
        if child is None:
            raise _Unresolvable(f"{prog}: unknown subcommand {name}")
        prog = f"{prog} {name}"
        remaining = remaining[index + 1 :]
        if not child._actions:
            module = DELEGATED_GROUPS.get(prog)
            if module is None:
                raise _Unresolvable(f"{prog}: delegating stub is not registered")
            child = build_parser(module)
        parser = child
    return parser, remaining


def _tool_parser(
    module: str, prog: str, rest: list[str]
) -> tuple[
    argparse.ArgumentParser,
    str,
    list[str],
]:
    loaded = importlib.import_module(module)
    if not hasattr(loaded, "build_parser"):
        raise _Unresolvable(f"{module}: module exposes no build_parser()")
    return cast(argparse.ArgumentParser, loaded.build_parser()), prog, rest


def _entry_point(tokens: Sequence[str]) -> tuple[argparse.ArgumentParser, str, list[str]]:
    rest = list(tokens[2:])  # drop `uv run`
    uv_flags = {"--offline", "--no-sync"}
    while rest and rest[0] in uv_flags:
        uv_flags.remove(rest[0])
        rest = rest[1:]
    if rest[:1] == ["baibai-engine"]:
        from baibai_engine.cli.app import DOMAINS

        if len(rest) < 2 or rest[1] not in DOMAINS:
            raise _Unresolvable(f"baibai-engine: unknown domain {' '.join(rest[1:2])}")
        module = DOMAINS[rest[1]].module
        return build_parser(module), f"baibai-engine {rest[1]}", rest[2:]
    if rest[:1] == ["baibai-jobs"]:
        from baibai_jobs.cli import _COMMANDS

        if len(rest) < 2 or rest[1] not in _COMMANDS:
            raise _Unresolvable(f"baibai-jobs: unknown command {' '.join(rest[1:2])}")
        return _tool_parser(_COMMANDS[rest[1]], f"baibai-jobs {rest[1]}", rest[2:])
    if rest[:1] != ["python"] or len(rest) < 2:
        raise _Unresolvable(f"unrecognised entry point: {' '.join(rest[:3])}")
    module_prefixes = ("baibai_jobs.", "baibai_engine.", "baibai_web.", "tools.")
    if rest[1] == "-m" and len(rest) > 2 and rest[2].startswith(module_prefixes):
        return _tool_parser(rest[2], f"python -m {rest[2]}", rest[3:])
    # `python tools/x.py` と `python -m tools.x` は同じ module を指す。tool ごとに
    # どちらで書かれていても、gate が問い合わせる先は 1 つでなければならない。
    if rest[1].startswith("tools/") and rest[1].endswith(".py"):
        module = rest[1].removesuffix(".py").replace("/", ".")
        return _tool_parser(module, f"python {rest[1]}", rest[2:])
    raise _Unresolvable(f"unrecognised entry point: {' '.join(rest[:3])}")


def _missing_required(parser: argparse.ArgumentParser, tokens: Sequence[str]) -> list[str]:
    given = {token.split("=", 1)[0] for token in tokens if token.startswith("-")}
    return [
        action.option_strings[-1]
        for action in parser._actions
        if action.required
        and action.option_strings
        and not given.intersection(action.option_strings)
    ]


def _unknown_options(parser: argparse.ArgumentParser, tokens: Sequence[str]) -> list[str]:
    known = {option for action in parser._actions for option in action.option_strings}
    return [
        token.partition("=")[0]
        for token in tokens
        if token.partition("=")[0] not in known and _looks_like_option(parser, token)
    ]


def _missing_option_values(parser: argparse.ArgumentParser, tokens: Sequence[str]) -> list[str]:
    missing = []
    index = 0
    while index < len(tokens):
        name, attached, _ = tokens[index].partition("=")
        arity = _option_arity(parser, name)
        if arity is not None:
            if _lacks_values(parser, tokens, index, arity):
                missing.append(name)
            elif not attached:
                index += arity[0]
        index += 1
    return missing


def check(root: Path) -> list[str]:
    errors: list[str] = []
    # Read once, and only when a document actually names the script.
    subcommands: frozenset[str] | None = None
    paths = [path for directory in _SCAN_DIRECTORIES for path in (root / directory).rglob("*.md")]
    paths.extend(root / name for name in _SCAN_FILES if (root / name).is_file())
    for path in sorted(paths):
        relative = path.relative_to(root)
        for command in _command_lines(path.read_text(encoding="utf-8")):
            # A parser check is not a shell interpreter. Pipelines, substitutions and
            # redirections need a manual check; never execute a documented command.
            if any(marker in command for marker in ("$(", "`", "|", "&&", ";", " >", " <<")):
                continue
            try:
                tokens = shlex.split(command)
            except ValueError as error:
                errors.append(f"{relative}: cannot tokenise {command!r} ({error})")
                continue
            if command.startswith(_TRANSFER_PREFIX):
                try:
                    if subcommands is None:
                        subcommands = transfer_subcommands(root)
                except (OSError, _Unresolvable) as error:
                    errors.append(f"{relative}: {error}")
                    continue
                if tokens[1] not in subcommands:
                    errors.append(
                        f"{relative}: `{command}` names no r2_transfer subcommand ({tokens[1]})"
                    )
                continue
            try:
                parser, prog, rest = _entry_point(tokens)
                parser, rest = _descend(parser, prog, rest)
            except _Unresolvable as error:
                errors.append(f"{relative}: {error}")
                continue
            if "--" in rest:
                rest = rest[: rest.index("--")]
            unknown = _unknown_options(parser, rest)
            if unknown:
                errors.append(f"{relative}: `{command}` has unknown options {', '.join(unknown)}")
            missing_values = _missing_option_values(parser, rest)
            if missing_values:
                errors.append(
                    f"{relative}: `{command}` omits value for {', '.join(missing_values)}"
                )
            missing = _missing_required(parser, rest)
            if missing:
                errors.append(
                    f"{relative}: `{command}` omits required {', '.join(sorted(missing))}"
                )
    return errors


def main() -> int:
    errors = check(Path.cwd())
    print("\n".join(errors), file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
