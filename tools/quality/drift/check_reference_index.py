"""Check that the reference index lists its sibling documents and no missing files.

This validates the index inventory, not whether a reader follows every link or
whether the referenced contract is correct.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_DIRECTORY = _ROOT / "docs/reference"
_INDEX = _DIRECTORY / "README.md"

# The reference half of one index row: a Markdown link whose target is a sibling
# document. Rows pointing outside the directory (`../architecture.md`) index another
# subsystem's contract and are not this directory's inventory.
_ROW_TARGET = re.compile(r"\]\(\./([A-Za-z0-9._-]+\.md)")


def check() -> tuple[str, ...]:
    try:
        index = _INDEX.read_text(encoding="utf-8")
    except OSError as exc:
        return (f"unable to read the reference index: {exc}",)

    present = {path.name for path in _DIRECTORY.glob("*.md")} - {_INDEX.name}
    indexed = set(_ROW_TARGET.findall(index))

    failures: list[str] = []
    missing = sorted(present - indexed)
    if missing:
        failures.append(
            f"docs/reference/README.md does not index {missing}; "
            "add the current document to the index or remove the obsolete file"
        )
    absent = sorted(indexed - present)
    if absent:
        failures.append(f"docs/reference/README.md indexes files that do not exist: {absent}")
    return tuple(failures)


def main() -> int:
    failures = check()
    if failures:
        for failure in failures:
            print(f"error: {failure}")
        return 1
    print("ok: docs/reference/README.md indexes every reference")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
