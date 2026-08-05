"""Reject obsolete operational instructions from current documentation."""

from __future__ import annotations

import re
import sys
from pathlib import Path

_BEHAVIOR_LEGACY = re.compile(
    r"ai-value-bargain-selection|financial-pro-review|"
    r"durability_gate|execution lifecycle|"
    # Retired domain vocabulary (doctrine #vocabulary is the naming authority):
    # the judgment artifact is the thesis, the pre-cap rank pool is the longlist,
    # and the OP3 output is the shortlist. The Git method tree is method/, so
    # reject any records/ path.
    r"decision.packet|packet.scaffold|packet.draft|--packet-id|research_packet|"
    r"audit.pool|--audit-top|reviewed.shortlist|cockpit|"
    # The single human-facing product name is Baibai Loop. `baibai-loop` (the
    # distribution) stays lowercase, so the hyphenated brand form is matched
    # case-sensitively while the rest of this pattern keeps IGNORECASE.
    r"Baibai App|(?-i:Baibai-Loop)|"
    # Retired macro context contract: the report declares no shelf life
    # (`valid_until`), core sections carry an economic connection rather than an
    # investment one, and there is one full-depth report instead of a
    # decision-grade / delta pair.
    r"valid_until|investment_connection|scenarios_connections|japan_specific|"
    r"fx_liquidity|decision-grade|delta 更新|delta更新|"
    # `(?<!/)` keeps retired path references (`records/`, `` `records/` ``) while
    # skipping `/records/` fragments inside external URLs.
    r"(?<!/)\brecords/|macro-dashboard",
    re.IGNORECASE,
)

# Documentation surfaces that state current behaviour. reports/ holds dated
# measurement records that intentionally keep the vocabulary of their time, so it
# is excluded; everything an agent reads as current instruction is scanned.
_SCAN_DIRECTORIES = ("docs", ".agents/skills", "data")
_ROOT_FILES = ("README.md", "AGENTS.md", "CLAUDE.md")


def check(root: Path) -> list[str]:
    behavior_paths = [root / name for name in _ROOT_FILES]
    for relative in _SCAN_DIRECTORIES:
        directory = root / relative
        if directory.is_dir():
            behavior_paths.extend(sorted(directory.rglob("*.md")))
    errors: list[str] = []
    for path in behavior_paths:
        if not path.is_file():
            continue
        if match := _BEHAVIOR_LEGACY.search(path.read_text(encoding="utf-8")):
            errors.append(
                f"{path.relative_to(root)}: obsolete operation instruction {match.group(0)!r}"
            )
    return errors


def main() -> int:
    errors = check(Path.cwd())
    print("\n".join(errors), file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
