"""Reject swing-era language from active record trees."""

from __future__ import annotations

import re
import sys
from pathlib import Path

_LEGACY = re.compile(
    r"time.?stop|tactical|pre_refactor|paper proxy|docs/components|cohort_tag|"
    r"kill_switch|swing thesis|損切り|(?:old|旧)\s*cap[^\n]*(?:8|45)\s*%",
    re.IGNORECASE,
)
_BEHAVIOR_LEGACY = re.compile(
    r"ai-value-bargain-selection|financial-pro-review|"
    r"records/04-position/\*[^\n]*\.md|records/03-thesis/\*[^\n]*\.md|"
    r"durability_gate|execution lifecycle",
    re.IGNORECASE,
)
_BEHAVIOR_ALLOW = {Path("docs/reference/testing-and-validation.md")}


def check(root: Path) -> list[str]:
    errors: list[str] = []
    for relative in ("records/01-macro-context", "records/03-thesis", "records/04-position"):
        directory = root / relative
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if not path.is_file():
                continue
            if match := _LEGACY.search(path.read_text(encoding="utf-8")):
                errors.append(f"{path.relative_to(root)}: legacy semantics {match.group(0)!r}")
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
