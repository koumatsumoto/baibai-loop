"""Reject commands that a skill documents but the tool would refuse to run.

A skill's command lines are executed verbatim every cycle. A missing required option
does not fail loudly once — it fails on every run, and the operator patches it by hand
each time, which is exactly the repeated cost the skill exists to remove.

Two families are checked, because a skill's commands come from two places. Python entry
points are asked of argparse: what the command needs, never what its values mean, so
placeholders like `<ASOF>` survive and nothing is type-converted. Positional counting is
deliberately absent — telling an option's value apart from a positional needs value
semantics this gate does not have. `batch/scripts/r2_transfer.sh` has no argparse to
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

# CI は各 gate を script として起動するので、隣の gate は絶対 import で引く。
from tools.quality.drift.check_cli_help import DELEGATED_GROUPS, build_parser

_SCAN_DIRECTORIES = (".agents/skills",)
# Only the inventory examples in this reference are in the additional scope.
_SCAN_FILES = ("docs/reference/market-lake.md",)
_FENCE = re.compile(r"^\s*```")
_INLINE_CODE = re.compile(r"`([^`]+)`")
_COMMENT = re.compile(r"\s+#\s.*$")

_TRANSFER_SCRIPT = Path("batch/scripts/r2_transfer.sh")
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


def _option_arity(parser: argparse.ArgumentParser, token: str) -> int | None:
    """How many following tokens `token` consumes, or None when it is not an option here."""
    for action in parser._actions:
        if token in action.option_strings:
            return 0 if action.nargs == 0 or isinstance(action.const, bool) else 1
    return None


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
            arity = _option_arity(parser, token.split("=", 1)[0])
            if arity is None:
                raise _Unresolvable(f"{prog}: unknown option {token}")
            index += 1 + (0 if "=" in token else arity)
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
    if rest[:1] == ["baibai-engine"]:
        from baibai_engine.cli import DOMAINS

        if len(rest) < 2 or rest[1] not in DOMAINS:
            raise _Unresolvable(f"baibai-engine: unknown domain {' '.join(rest[1:2])}")
        return build_parser(DOMAINS[rest[1]].module), f"baibai-engine {rest[1]}", rest[2:]
    if rest[:1] == ["baibai-batch"]:
        from baibai_batch.cli import _COMMANDS

        if len(rest) < 2 or rest[1] not in _COMMANDS:
            raise _Unresolvable(f"baibai-batch: unknown command {' '.join(rest[1:2])}")
        return _tool_parser(_COMMANDS[rest[1]].__module__, f"baibai-batch {rest[1]}", rest[2:])
    if rest[:1] != ["python"] or len(rest) < 2:
        raise _Unresolvable(f"unrecognised entry point: {' '.join(rest[:3])}")
    module_prefixes = ("baibai_batch.", "baibai_engine.", "baibai_web.", "tools.")
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
    tokens = tokens[: tokens.index("--")] if "--" in tokens else tokens
    known = {option for action in parser._actions for option in action.option_strings}
    return [
        argument.split("=", 1)[0]
        for argument in tokens
        if argument.startswith("-")
        and argument != "-"
        and argument.split("=", 1)[0] not in known
        and not re.fullmatch(r"-\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|-\.\d+", argument)
    ]


def check(root: Path) -> list[str]:
    errors: list[str] = []
    # Read once, and only when a document actually names the script.
    subcommands: frozenset[str] | None = None
    paths = [path for directory in _SCAN_DIRECTORIES for path in (root / directory).rglob("*.md")]
    paths.extend(root / name for name in _SCAN_FILES if (root / name).is_file())
    for path in sorted(paths):
        relative = path.relative_to(root)
        for command in _command_lines(path.read_text(encoding="utf-8")):
            if relative.as_posix() in _SCAN_FILES and not command.startswith(
                "uv run baibai-engine lake inventory"
            ):
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
            unknown = _unknown_options(parser, rest)
            if unknown:
                errors.append(f"{relative}: `{command}` has unknown options {', '.join(unknown)}")
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
