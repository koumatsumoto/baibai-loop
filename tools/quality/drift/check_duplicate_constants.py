"""政策ラベル付きの重複記載を止め、Researchの資金規律を正本へ案内する。"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

_EXCLUDED = {
    Path("docs/portfolio-management.md"),
    Path("engine/src/baibai_engine/position/policy.py"),
}


def check(root: Path) -> list[str]:
    forbidden = _policy_patterns(root)
    candidates = [root / "README.md", root / "AGENTS.md"]
    candidates.extend((root / "docs").rglob("*.md"))
    candidates.extend((root / ".agents" / "skills").rglob("*.md"))
    errors: list[str] = []
    for path in candidates:
        if not path.is_file() or path.relative_to(root) in _EXCLUDED:
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in forbidden:
            for match in pattern.finditer(text):
                errors.append(
                    f"{path.relative_to(root)}: duplicated policy literal {match.group(0)!r}"
                )
                break
    return errors


def _policy_patterns(root: Path) -> tuple[re.Pattern[str], ...]:
    policy = _load_policy(root)
    cash = policy["cash_management"]
    order = policy["order_constraints"]
    board_lot = re.escape(_number(order["board_lot"]))
    dry_powder = re.escape(_number(cash["dry_powder_warning_pct"]))
    return (
        re.compile(rf"board[ _-]?lot.{{0,30}}\b{board_lot}\b", re.IGNORECASE),
        re.compile(rf"dry[ _-]?powder.{{0,30}}\b{dry_powder}(?:\.0)?\s*%", re.IGNORECASE),
    )


def _load_policy(root: Path) -> dict[str, dict[str, int | float | bool]]:
    policy_path = root / "engine/src/baibai_engine/position/policy.py"
    tree = ast.parse(policy_path.read_text(encoding="utf-8"))
    for statement in tree.body:
        if (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == "PORTFOLIO_POLICY"
            and statement.value is not None
        ):
            policy = ast.literal_eval(statement.value)
            if isinstance(policy, dict):
                return policy
    raise RuntimeError("PORTFOLIO_POLICY literal is required for duplicate constant scan")


def _number(value: int | float | bool) -> str:
    if isinstance(value, bool):
        raise RuntimeError("policy numeric value must not be boolean")
    return str(value).removesuffix(".0")


def main() -> int:
    errors = check(Path.cwd())
    print("\n".join(errors), file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
