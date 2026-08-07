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
_HEADING_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_EXPLICIT_ANCHOR = re.compile(r'<a\s+id="(?P<id>[^"]+)"')
_ROOT_FILES = ("README.md", "AGENTS.md", "CLAUDE.md")
# `tools` は architecture.md と ops-maintenance skill が cloud 運用の正本として名指しする
# `tools/cloud/README.md` を含み、`reports` は改善サイクルが再現手順つきの一次資料として
# 読む dated record を含む。どちらも実装へ降りる経路が切れると、その仕様がどこからも
# 引けなくなる。
_SCAN_DIRECTORIES = ("docs", "data", "reports", "tools", ".agents/skills", ".claude/skills")


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
            paths.extend(sorted(p for p in directory.rglob("*.md") if p.is_file()))
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
