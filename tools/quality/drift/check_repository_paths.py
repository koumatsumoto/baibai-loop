"""Keep canonical repository path literals in their owning layout modules."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_RUNTIME_ROOTS = ("engine/src", "web/backend/src", "jobs/src")
_CONTEXT_PATH = (
    "engine/src/baibai_engine/machine_analysis/company_valuation/"
    "calibration/published/er-level-calibration-latest.yaml"
)
_OWNERS = {
    "stores/application/baibai.sqlite": Path(
        "engine/src/baibai_engine/foundation/repository_layout.py"
    ),
    "stores/market/market.sqlite": Path("engine/src/baibai_engine/foundation/repository_layout.py"),
    "stores/market_context/indicators.sqlite": Path(
        "engine/src/baibai_engine/foundation/repository_layout.py"
    ),
    "stores/screening/runs.sqlite": Path(
        "engine/src/baibai_engine/foundation/repository_layout.py"
    ),
    "stores/screening/calibration": Path(
        "engine/src/baibai_engine/foundation/repository_layout.py"
    ),
    "web/config/market-context-panel.yaml": Path("web/backend/src/baibai_web/repository_layout.py"),
    _CONTEXT_PATH: Path("engine/src/baibai_engine/foundation/repository_layout.py"),
}

_RULE_DIRECTORIES = (
    "engine/src/baibai_engine/machine_analysis/screening/rules/",
    "engine/src/baibai_engine/machine_analysis/market_context/reading/rules/",
)
_RULE_OWNER = Path("engine/src/baibai_engine/foundation/repository_layout.py")


def check(root: Path) -> list[str]:
    errors: list[str] = []
    for runtime_root in _RUNTIME_ROOTS:
        directory = root / runtime_root
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.py")):
            relative = path.relative_to(root)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(relative))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                owner = _OWNERS.get(node.value)
                if node.value.startswith(_RULE_DIRECTORIES) and node.value.endswith(".yaml"):
                    owner = _RULE_OWNER
                if owner is not None and relative != owner:
                    errors.append(
                        f"{relative}:{node.lineno}: repository path {node.value!r} "
                        f"belongs in {owner}"
                    )
    return errors


def main() -> int:
    errors = check(Path.cwd())
    print("\n".join(errors), file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
