"""Reject relative Markdown links whose target file does not exist."""

from __future__ import annotations

import re
import sys
from pathlib import Path

_LINK = re.compile(r"\]\((?P<target>\.?\.?/[^)#\s]+|/docs/[^)#\s]+)(?:#[^)]*)?\)")
_ROOT_FILES = ("README.md", "AGENTS.md")


def check(root: Path) -> list[str]:
    paths = [root / name for name in _ROOT_FILES if (root / name).is_file()]
    paths.extend((root / "docs").rglob("*.md") if (root / "docs").is_dir() else ())
    paths.extend((root / "data").rglob("*.md") if (root / "data").is_dir() else ())
    paths.extend(
        (root / ".agents" / "skills").rglob("*.md")
        if (root / ".agents" / "skills").is_dir()
        else ()
    )
    paths.extend(
        (root / ".claude" / "skills").rglob("*.md")
        if (root / ".claude" / "skills").is_dir()
        else ()
    )
    errors: list[str] = []
    for path in paths:
        for match in _LINK.finditer(path.read_text(encoding="utf-8")):
            target = match.group("target")
            resolved = (
                root / target.removeprefix("/") if target.startswith("/") else path.parent / target
            )
            if not resolved.exists():
                errors.append(f"{path.relative_to(root)}: missing Markdown link target {target}")
    return errors


def main() -> int:
    errors = check(Path.cwd())
    print("\n".join(errors), file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
