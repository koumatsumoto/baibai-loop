"""Keep the architecture CLI table aligned with declared console scripts."""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

_COMMAND = re.compile(r"\bbaibai-(?:engine|app)\b")


def check(root: Path) -> list[str]:
    scripts = set(
        tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
    )
    document = (root / "docs" / "architecture.md").read_text(encoding="utf-8")
    documented = set(_COMMAND.findall(document))
    missing = sorted(scripts - documented)
    phantom = sorted(documented - scripts)
    errors = [f"docs/architecture.md: missing CLI {name}" for name in missing]
    errors.extend(f"docs/architecture.md: unknown CLI {name}" for name in phantom)
    return errors


def main() -> int:
    errors = check(Path.cwd())
    print("\n".join(errors), file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
