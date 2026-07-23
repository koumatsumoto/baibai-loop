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
    # the OP3 output is the shortlist, and the read-only UI is Baibai App.
    r"decision.packet|packet.scaffold|packet.draft|--packet-id|research_packet|"
    r"audit.pool|--audit-top|reviewed.shortlist|cockpit|"
    r"records/_config|records/_playbooks|macro-dashboard",
    re.IGNORECASE,
)
_BEHAVIOR_ALLOW = {Path("docs/reference/testing-and-validation.md")}


def check(root: Path) -> list[str]:
    errors: list[str] = []
    behavior_paths = [root / "README.md", root / "AGENTS.md"]
    for relative in ("docs/operations", "docs/workflow", ".agents/skills"):
        directory = root / relative
        if directory.is_dir():
            behavior_paths.extend(directory.rglob("*.md"))
    for path in behavior_paths:
        if not path.is_file() or path.relative_to(root) in _BEHAVIOR_ALLOW:
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
