"""Prevent policy literals from being copied outside their two source documents."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

_EXCLUDED = {Path("docs/portfolio-management.md"), Path("src/baibai_loop/position/policy.py")}


def check(root: Path) -> list[str]:
    forbidden = _policy_patterns(root)
    candidates = [root / "README.md", root / "AGENTS.md"]
    candidates.extend((root / "docs").rglob("*.md"))
    candidates.extend((root / ".agents" / "skills").rglob("*.md"))
    candidates.extend((root / ".claude" / "skills").rglob("*.md"))
    errors: list[str] = []
    for path in candidates:
        if not path.is_file() or path.relative_to(root) in _EXCLUDED:
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in forbidden:
            if match := pattern.search(text):
                errors.append(
                    f"{path.relative_to(root)}: duplicated policy literal {match.group(0)!r}"
                )
    return errors


def _policy_patterns(root: Path) -> tuple[re.Pattern[str], ...]:
    policy = _load_policy(root)
    cash = policy["cash_management"]
    risk = policy["risk_budget"]
    order = policy["order_constraints"]
    monthly_yen = cash["monthly_contribution_yen"]
    monthly = str(monthly_yen)
    monthly_pattern = r"[,_]?".join(monthly)
    japanese_monthly_patterns: tuple[re.Pattern[str], ...] = ()
    if isinstance(monthly_yen, int) and not isinstance(monthly_yen, bool):
        monthly_man_yen, remainder = divmod(monthly_yen, 10_000)
        if remainder == 0:
            man_yen = re.escape(str(monthly_man_yen))
            japanese_monthly_patterns = (
                re.compile(
                    rf"(?<![\d.])(?:{man_yen}万円(?![株件人])|"
                    rf"(?:月|毎月)\s*{man_yen}万(?![円株件人]))"
                ),
            )
    concentrations = (
        risk["max_ticker_concentration_pct"],
        risk["max_sector_concentration_pct"],
        risk["max_common_factor_concentration_pct"],
    )
    concentration_pattern = "|".join(re.escape(_number(value)) for value in concentrations)
    board_lot = re.escape(_number(order["board_lot"]))
    dry_powder = re.escape(_number(cash["dry_powder_warning_pct"]))
    return (
        re.compile(rf"(?<!\d){monthly_pattern}(?!\d)"),
        *japanese_monthly_patterns,
        re.compile(rf"(?<![\d.])(?:{concentration_pattern})(?:\.0)?\s*%"),
        re.compile(rf"\b{board_lot}\s*株"),
        re.compile(rf"board[ _-]?lot.{{0,30}}\b{board_lot}\b", re.IGNORECASE),
        re.compile(rf"dry[ _-]?powder.{{0,30}}\b{dry_powder}(?:\.0)?\s*%", re.IGNORECASE),
    )


def _load_policy(root: Path) -> dict[str, dict[str, int | float | bool]]:
    tree = ast.parse((root / "src/baibai_loop/position/policy.py").read_text(encoding="utf-8"))
    for statement in tree.body:
        if (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == "PORTFOLIO_POLICY"
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
