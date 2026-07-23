"""Reject relative Markdown links whose target file or anchor does not exist."""

from __future__ import annotations

import re
import sys
from pathlib import Path

# A link body is `](target#anchor)` where either part may be absent: `](#anchor)`
# is a same-file anchor and `](target)` is a plain file link. External `http(s)`
# links start with neither `./`, `../`, `/docs/`, nor `#`, so they never match.
_LINK = re.compile(r"\]\((?P<target>\.{0,2}/[^)#\s]+|/docs/[^)#\s]+)?(?P<anchor>#[^)\s]+)?\)")
_HEADING = re.compile(r"^#{1,6}\s+(?P<text>.+?)\s*$", re.MULTILINE)
_EXPLICIT_ANCHOR = re.compile(r'<a\s+id="(?P<id>[^"]+)"')
_ROOT_FILES = ("README.md", "AGENTS.md")
_SCAN_DIRECTORIES = ("docs", "data", ".agents/skills", ".claude/skills")


def _slug(text: str) -> str:
    """Approximate GitHub's heading-to-anchor slug (keeps CJK, drops punctuation)."""
    text = text.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _anchors(text: str) -> set[str]:
    anchors = {match.group("id") for match in _EXPLICIT_ANCHOR.finditer(text)}
    anchors.update(_slug(match.group("text")) for match in _HEADING.finditer(text))
    return anchors


def _scan_paths(root: Path) -> list[Path]:
    paths = [root / name for name in _ROOT_FILES if (root / name).is_file()]
    for relative in _SCAN_DIRECTORIES:
        directory = root / relative
        if directory.is_dir():
            paths.extend(sorted(directory.rglob("*.md")))
    return paths


def check(root: Path) -> list[str]:
    anchor_cache: dict[Path, set[str]] = {}

    def anchors_of(path: Path) -> set[str]:
        if path not in anchor_cache:
            anchor_cache[path] = (
                _anchors(path.read_text(encoding="utf-8")) if path.is_file() else set()
            )
        return anchor_cache[path]

    errors: list[str] = []
    for path in _scan_paths(root):
        for match in _LINK.finditer(path.read_text(encoding="utf-8")):
            target = match.group("target")
            anchor = match.group("anchor")
            if target is None and anchor is None:
                continue
            if target is not None:
                resolved = (
                    root / target.removeprefix("/")
                    if target.startswith("/")
                    else path.parent / target
                )
                if not resolved.exists():
                    errors.append(
                        f"{path.relative_to(root)}: missing Markdown link target {target}"
                    )
                    continue
            else:
                resolved = path
            if (
                anchor is not None
                and resolved.suffix == ".md"
                and anchor[1:] not in anchors_of(resolved)
            ):
                errors.append(
                    f"{path.relative_to(root)}: missing Markdown anchor {target or ''}{anchor}"
                )
    return errors


def main() -> int:
    errors = check(Path.cwd())
    print("\n".join(errors), file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
