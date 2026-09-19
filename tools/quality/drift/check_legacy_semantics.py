"""Reject obsolete operational instructions from current documentation."""

from __future__ import annotations

import re
import sys
from pathlib import Path

_BEHAVIOR_LEGACY = re.compile(
    r"ai-value-bargain-selection|financial-pro-review|"
    # The single human-facing product name is Baibai Loop. `baibai-loop` (the
    # distribution) stays lowercase, so the hyphenated brand form is matched
    # case-sensitively while the rest of this pattern keeps IGNORECASE.
    r"Baibai App|(?-i:Baibai-Loop)|"
    # `(?<!/)` keeps retired path references (`records/`, `` `records/` ``) while
    # skipping `/records/` fragments inside external URLs.
    r"(?<!/)\brecords/|macro-dashboard|"
    # Reject retired authority contracts, not the word shadow in CSS/Python prose.
    r"sqlite_authority|lake_authority|"
    # screening rules は dated revision で増え、現行 revision は
    # `rule_config.DEFAULT_RULES_PATH` が解決する。file 名の実値を書いた doc は次の改訂で
    # 存在しない path を「閾値の正本」として指すことになるので、revision を名指ししない。
    r"method/screening/rules/\d{4}-\d{2}-\d{2}",
    re.IGNORECASE,
)

_REPOSITORY_PATH_LEGACY = re.compile(
    r"(?:^|[\s`\"'(])src/baibai_(?:engine|app)|(?:^|[\s`\"'(])ui/|"
    r"(?:^|[\s`])cd\s+ui(?:/|\s|$)|(?:\.\./)+ui(?:/|\s|[\"'])|"
    r"reports/\d{4}-\d{2}-\d{2}-|"
    r"cloud/worker|tools/cloud|data/(?:app|screening|indicators)|"
    # 固定 release を SQLite へ実体化するのは `lake hydrate` だけになった。projection
    # subsystem とその診断は削除済みなので、その module・CLI・成果物・metadata を指す
    # 参照は存在しない path を現行手順として提示することになる。`projection` 単体は
    # doctrine の表示物にも当たるため、退役した結合だけを拒否する。
    r"lake_shadow|verify_lake_release_parity|benchmark_lake_projection|"
    r"build_projection|projection\.sqlite|projection_meta|projection_fingerprint|"
    r"projection_indexes|lake projection build|"
    r"method/(?:macro-panel|screening-rules|macro-reading|playbooks)",
    re.IGNORECASE,
)

# Documentation surfaces that state current behaviour. reports/ holds dated
# measurement records that intentionally keep the vocabulary of their time, so it
# is excluded; everything an agent reads as current instruction is scanned.
_SCAN_DIRECTORIES = (
    "docs",
    ".agents/skills",
    "engine",
    "web",
    "batch",
    "tools",
    "method",
    "stores",
    ".github",
)
_ROOT_FILES = ("README.md", "AGENTS.md")
_PATH_ROOT_FILES = (
    *_ROOT_FILES,
    "pyproject.toml",
    ".pre-commit-config.yaml",
    ".gitignore",
    ".env.sample",
)
_CURRENT_SUFFIXES = {".md", ".py", ".sh", ".yaml", ".yml", ".json", ".toml", ".ts", ".tsx"}
_IGNORED_PARTS = {"node_modules", ".playwright-cli", "__pycache__"}
_PATH_PATTERN_OWNERS = {
    Path("engine/src/baibai_engine/foundation/repository_layout.py"),
    Path("tools/quality/drift/check_legacy_semantics.py"),
}


def check(root: Path) -> list[str]:
    behavior_paths = [root / name for name in _ROOT_FILES]
    current_paths = [root / name for name in _PATH_ROOT_FILES if (root / name).is_file()]
    for directory_name in _SCAN_DIRECTORIES:
        directory = root / directory_name
        if directory.is_dir():
            paths = [
                path
                for path in sorted(directory.rglob("*"))
                if path.suffix in _CURRENT_SUFFIXES
                and not (_IGNORED_PARTS & set(path.parts))
                # stores/ contains runtime mirrors and immutable manifests alongside
                # its tracked README files. Generated JSON is evidence, not current
                # source, and may legitimately retain the vocabulary of its generation.
                and (directory_name != "stores" or path.suffix == ".md")
            ]
            current_paths.extend(paths)
            behavior_paths.extend(path for path in paths if path.suffix == ".md")
    errors: list[str] = []
    for path in behavior_paths:
        if not path.is_file():
            continue
        if match := _BEHAVIOR_LEGACY.search(path.read_text(encoding="utf-8")):
            errors.append(
                f"{path.relative_to(root)}: obsolete operation instruction {match.group(0)!r}"
            )
    for path in current_paths:
        relative_path = path.relative_to(root)
        text = path.read_text(encoding="utf-8")
        if relative_path in _PATH_PATTERN_OWNERS:
            continue
        if match := _REPOSITORY_PATH_LEGACY.search(text):
            errors.append(f"{relative_path}: obsolete repository path {match.group(0)!r}")
    return errors


def main() -> int:
    errors = check(Path.cwd())
    print("\n".join(errors), file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
