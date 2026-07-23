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
    # the OP3 output is the shortlist, and the read-only UI is Baibai App. The Git
    # method tree is method/ (records/ was renamed), so reject any records/ path.
    r"decision.packet|packet.scaffold|packet.draft|--packet-id|research_packet|"
    r"audit.pool|--audit-top|reviewed.shortlist|cockpit|"
    r"\brecords/|macro-dashboard",
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
