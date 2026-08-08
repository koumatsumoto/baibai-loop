"""Selection profile resolution.

Built-in profile is ``balanced`` only and its values are inlined into the
canonical ``method/screening/rules/*.yaml`` so external profile YAML
loading was removed in cleanup round 2. The ``profile_overrides`` parameter
is kept for in-process callers such as ``selection-ablation`` whose
``no_diversity`` variant injects programmatic overrides without a YAML file.
"""

from __future__ import annotations

from collections.abc import Mapping

from ..rule_config import BUILTIN_SELECTION_PROFILES, SelectionRules


def resolve_selection_rules(
    base: SelectionRules,
    *,
    profile: str,
    profile_overrides: Mapping[str, Mapping[str, object]] | None = None,
) -> SelectionRules:
    if profile not in BUILTIN_SELECTION_PROFILES:
        raise ValueError(f"unknown selection profile: {profile}")
    overrides = (profile_overrides or {}).get(profile)
    if overrides is None:
        return base
    data = base.model_dump(mode="python")
    data = _deep_merge(data, overrides)
    return SelectionRules.model_validate(data)


def _deep_merge(
    base: Mapping[str, object],
    override: Mapping[str, object],
) -> dict[str, object]:
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged
