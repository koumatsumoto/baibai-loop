"""Reject commands that a skill documents but argparse would refuse to run.

A skill's command lines are executed verbatim every cycle. A missing required option
does not fail loudly once — it fails on every run, and the operator patches it by hand
each time, which is exactly the repeated cost the skill exists to remove.

The check asks argparse what the command needs, never what its values mean: placeholders
like `<ASOF>` must survive. So required *options* are checked for presence and nothing is
type-converted. Positional counting is deliberately absent — telling an option's value
apart from a positional needs value semantics this gate does not have.
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
from tools.drift.check_cli_help import DELEGATED_GROUPS, build_parser

_SCAN_DIRECTORIES = (".agents/skills",)
_FENCE = re.compile(r"^\s*```")
_INLINE_CODE = re.compile(r"`([^`]+)`")
_COMMENT = re.compile(r"\s+#\s.*$")


class _Unresolvable(ValueError):
    """The command line does not name a parser this gate can ask."""


def _command_lines(text: str) -> Iterator[str]:
    """Every `uv run ...` command in the document, fenced or inline."""
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
            if candidate.startswith("uv run "):
                yield candidate
            continue
        for span in _INLINE_CODE.findall(line):
            if span.startswith("uv run "):
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
        remaining = remaining[:index] + remaining[index + 1 :]
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
    if rest[:1] != ["python"] or len(rest) < 2:
        raise _Unresolvable(f"unrecognised entry point: {' '.join(rest[:3])}")
    if rest[1] == "-m" and len(rest) > 2 and rest[2].startswith("tools."):
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


def check(root: Path) -> list[str]:
    errors: list[str] = []
    for directory in _SCAN_DIRECTORIES:
        for path in sorted((root / directory).rglob("*.md")):
            relative = path.relative_to(root)
            for command in _command_lines(path.read_text(encoding="utf-8")):
                try:
                    tokens = shlex.split(command)
                except ValueError as error:
                    errors.append(f"{relative}: cannot tokenise {command!r} ({error})")
                    continue
                try:
                    parser, prog, rest = _entry_point(tokens)
                    parser, rest = _descend(parser, prog, rest)
                except _Unresolvable as error:
                    errors.append(f"{relative}: {error}")
                    continue
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
