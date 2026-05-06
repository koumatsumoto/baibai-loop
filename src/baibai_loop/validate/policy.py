"""Portfolio policy snapshot validation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

import yaml

from .errors import ValidationFinding
from .registry import validate_callable_ids_file

_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)


def discover_policy_files(root: Path) -> list[Path]:
    """Return immutable portfolio policy snapshot files."""
    if not root.exists():
        return []
    return sorted(path for path in root.glob("**/*.md") if path.is_file())


def validate_policy_file(path: Path) -> list[ValidationFinding]:
    """Validate portfolio policy business invariants."""
    parsed = _load_front_matter(path)
    if isinstance(parsed, ValidationFinding):
        return [parsed]
    findings: list[ValidationFinding] = []
    findings.extend(_check_required_sections(path, parsed))
    findings.extend(_check_cap_invariants(path, parsed))
    findings.extend(_check_policy_rules(path, parsed))
    findings.extend(_check_execution_scaling(path, parsed))
    findings.extend(validate_callable_ids_file(path))
    return findings


def _load_front_matter(path: Path) -> Mapping[str, object] | ValidationFinding:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return ValidationFinding(
            severity="error",
            target=path,
            code="policy.io",
            message=f"failed to read file: {exc}",
        )
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        return ValidationFinding(
            severity="error",
            target=path,
            code="policy.front-matter",
            message="portfolio policy must start with YAML front matter",
        )
    raw: object = yaml.safe_load(match.group(1))
    if not isinstance(raw, Mapping):
        return ValidationFinding(
            severity="error",
            target=path,
            code="policy.front-matter",
            message="portfolio policy front matter must be a mapping",
        )
    return raw


def _check_required_sections(
    path: Path, front_matter: Mapping[str, object]
) -> list[ValidationFinding]:
    required = (
        "policy_id",
        "effective_from",
        "policy_origin",
        "capital_basis",
        "risk_budget",
        "minimum_payoff",
        "execution_scaling",
        "conviction_tier_caps",
        "policy_rules",
    )
    findings: list[ValidationFinding] = []
    for field in required:
        if field not in front_matter:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="policy.required",
                    message=f"missing required policy field: {field}",
                    location=field,
                )
            )
    return findings


def _check_cap_invariants(
    path: Path, front_matter: Mapping[str, object]
) -> list[ValidationFinding]:
    capital = _mapping(front_matter.get("capital_basis"))
    risk = _mapping(front_matter.get("risk_budget"))
    tiers = _mapping(front_matter.get("conviction_tier_caps"))
    findings: list[ValidationFinding] = []

    paper_capital = _number(capital.get("paper_proxy_capital_yen"))
    max_paper_position = _number(risk.get("max_paper_proxy_position_size_yen"))
    if (
        paper_capital is not None
        and max_paper_position is not None
        and max_paper_position > paper_capital
    ):
        findings.append(
            _finding(
                path,
                "policy.paper-cap",
                "max_paper_proxy_position_size_yen must be <= paper_proxy_capital_yen",
                "risk_budget.max_paper_proxy_position_size_yen",
            )
        )

    real_capital = _number(capital.get("real_capital_yen"))
    tactical_budget = _number(capital.get("tactical_real_budget_yen"))
    max_real_order = _number(risk.get("max_real_order_notional_yen"))
    if (
        real_capital is not None
        and tactical_budget is not None
        and max_real_order is not None
        and not max_real_order <= tactical_budget <= real_capital
    ):
        findings.append(
            _finding(
                path,
                "policy.real-cap",
                "max_real_order_notional_yen must be <= tactical budget <= real capital",
                "capital_basis.tactical_real_budget_yen",
            )
        )

    for tier_name, tier_raw in tiers.items():
        if not isinstance(tier_raw, Mapping):
            continue
        tier_paper = _number(tier_raw.get("max_paper_proxy_position_size_yen"))
        tier_real = _number(tier_raw.get("max_real_order_notional_yen"))
        if (
            max_paper_position is not None
            and tier_paper is not None
            and tier_paper > max_paper_position
        ):
            findings.append(
                _finding(
                    path,
                    "policy.tier-paper-cap",
                    "conviction tier paper cap must be <= policy paper cap",
                    f"conviction_tier_caps.{tier_name}.max_paper_proxy_position_size_yen",
                )
            )
        if max_real_order is not None and tier_real is not None and tier_real > max_real_order:
            findings.append(
                _finding(
                    path,
                    "policy.tier-real-cap",
                    "conviction tier real cap must be <= policy real order cap",
                    f"conviction_tier_caps.{tier_name}.max_real_order_notional_yen",
                )
            )
    return findings


def _check_policy_rules(path: Path, front_matter: Mapping[str, object]) -> list[ValidationFinding]:
    minimum_payoff = _mapping(front_matter.get("minimum_payoff"))
    policy_rules = _mapping(front_matter.get("policy_rules"))
    findings: list[ValidationFinding] = []

    default_rule = _mapping(policy_rules.get("__default__"))
    if default_rule.get("override_allowed") is not False:
        findings.append(
            _finding(
                path,
                "policy.default-override",
                "policy_rules.__default__.override_allowed must be false",
                "policy_rules.__default__",
            )
        )
    if default_rule.get("override_severity") != "error":
        findings.append(
            _finding(
                path,
                "policy.default-severity",
                "policy_rules.__default__.override_severity must be error",
                "policy_rules.__default__",
            )
        )

    minimum_fields = {
        "min_risk_reward_ratio": "minimum_payoff.min_risk_reward_ratio",
        "min_expected_upside_pct": "minimum_payoff.min_expected_upside_pct",
    }
    for source_field, rule_key in minimum_fields.items():
        if source_field in minimum_payoff and rule_key not in policy_rules:
            findings.append(
                _finding(
                    path,
                    "policy.minimum-payoff-rule",
                    f"minimum payoff field must have explicit policy rule: {rule_key}",
                    "policy_rules",
                )
            )
    return findings


def _check_execution_scaling(
    path: Path, front_matter: Mapping[str, object]
) -> list[ValidationFinding]:
    scaling = _mapping(front_matter.get("execution_scaling"))
    value = _number(scaling.get("paper_to_real_order_notional_pct"))
    if value is None or value <= 0 or value > 100:
        return [
            _finding(
                path,
                "policy.execution-scaling",
                "paper_to_real_order_notional_pct must be in (0, 100]",
                "execution_scaling.paper_to_real_order_notional_pct",
            )
        ]
    return []


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _finding(path: Path, code: str, message: str, location: str) -> ValidationFinding:
    return ValidationFinding(
        severity="error",
        target=path,
        code=code,
        message=message,
        location=location,
    )
