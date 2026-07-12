"""Reject tracked decision artifacts that depend on ignored local inputs."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

_FORBIDDEN_REF = ("records/02-candidates", "data/", ".cache/", "market.sqlite")


def check(root: Path) -> list[str]:
    errors: list[str] = []
    for base in (root / "records" / "03-thesis",):
        if not base.is_dir():
            continue
        for path in base.rglob("*.yaml"):
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                continue
            for ref in _source_values(loaded):
                if any(part in ref for part in _FORBIDDEN_REF):
                    errors.append(f"{path.relative_to(root)}: non-clean-checkout source ref {ref}")
    return errors


def _source_values(value: object) -> list[str]:
    if isinstance(value, dict):
        found = [
            item
            for key, item in value.items()
            if (key == "source_id" or key == "dataset" or key.endswith("_ref") or key == "ref")
            and isinstance(item, str)
        ]
        return found + [ref for item in value.values() for ref in _source_values(item)]
    if isinstance(value, list):
        return [ref for item in value for ref in _source_values(item)]
    return []


def main() -> int:
    errors = check(Path.cwd())
    print("\n".join(errors), file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
