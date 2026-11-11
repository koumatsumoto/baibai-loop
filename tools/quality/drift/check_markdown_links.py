"""Reject relative Markdown links whose target file or anchor does not exist."""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Include bare relative paths, not only ./ and ../ links.
_LINK = re.compile(r"\]\((?P<target>[^)#\s]+)?(?P<anchor>#[^)\s]+)?\)")
_EXTERNAL = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|//)")
_HEADING = re.compile(r"^#{1,6}\s+(?P<text>.+?)\s*$", re.MULTILINE)
_HEADING_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_EXPLICIT_ANCHOR = re.compile(r'<a\s+id="(?P<id>[^"]+)"')
_ROOT_FILES = ("README.md", "AGENTS.md")
# Responsibility roots carry current ownership and operations documents.
# Historical studies remain in fixed Git commits, not a second documentation tree.
_SCAN_DIRECTORIES = (
    "docs",
    "engine",
    "web",
    "jobs",
    "tools",
    "stores",
    ".agents/skills",
)
_IGNORED_DIRECTORY_NAMES = frozenset({"node_modules", "dist", ".venv", "__pycache__"})


def _slug(text: str) -> str:
    """Approximate GitHub's heading-to-anchor slug (keeps CJK, drops punctuation)."""
    text = _HEADING_LINK.sub(r"\1", text)  # a link in a heading contributes its text only
    text = text.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _anchors(text: str) -> set[str]:
    anchors = {match.group("id") for match in _EXPLICIT_ANCHOR.finditer(text)}
    seen: dict[str, int] = {}
    for match in _HEADING.finditer(text):
        base = _slug(match.group("text"))
        # GitHub disambiguates repeated heading slugs as foo, foo-1, foo-2, ...
        occurrence = seen.get(base, -1) + 1
        seen[base] = occurrence
        anchors.add(base if occurrence == 0 else f"{base}-{occurrence}")
    return anchors


def _scan_paths(root: Path) -> list[Path]:
    paths = [root / name for name in _ROOT_FILES if (root / name).is_file()]
    for relative in _SCAN_DIRECTORIES:
        directory = root / relative
        if directory.is_dir():
            found: list[Path] = []
            for parent, directories, files in directory.walk():
                # Skip dependency/build trees before walking their descendants.
                directories[:] = [
                    name for name in directories if name not in _IGNORED_DIRECTORY_NAMES
                ]
                found.extend(
                    parent / name
                    for name in files
                    if name.endswith(".md") and (parent / name).is_file()
                )
            paths.extend(sorted(found))
    return paths


def check(root: Path) -> list[str]:
    texts: dict[Path, str] = {}
    anchor_cache: dict[Path, set[str]] = {}

    def text_of(path: Path) -> str:
        key = path.resolve()
        if key not in texts:
            texts[key] = path.read_text(encoding="utf-8")
        return texts[key]

    def anchors_of(path: Path) -> set[str]:
        key = path.resolve()
        if key not in anchor_cache:
            anchor_cache[key] = _anchors(text_of(path)) if path.is_file() else set()
        return anchor_cache[key]

    errors: list[str] = []
    for path in _scan_paths(root):
        for match in _LINK.finditer(text_of(path)):
            target = match.group("target")
            anchor = match.group("anchor")
            if target is None and anchor is None:
                continue
            if target is not None:
                if _EXTERNAL.match(target):
                    continue
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
