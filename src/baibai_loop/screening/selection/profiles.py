"""Built-in selection profiles and profile-override resolution."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from ..rule_config import BUILTIN_SELECTION_PROFILES, SelectionRules

BALANCED_PROFILE_OVERRIDES: Mapping[str, object] = {
    "fast_dislocation": {
        "price_change_1d_max": -0.05,
        "price_change_5d_max": -0.08,
        "price_change_20d_max": -0.10,
        "price_change_60d_max": -0.22,
        "gap_from_52w_low_max": 0.15,
        "turnover_spike_5d_min": 1.5,
        "min_fundamental_guard_count": 2,
        "min_fundamental_guard_family_count": 2,
        "high_confidence_guard_count": 3,
        "high_confidence_guard_family_count": 2,
        "ocf_yield_min": 0.08,
        "fcf_yield_min": 0.05,
        "price_to_equity_max": 1.0,
        "equity_ratio_min": 0.4,
        "net_cash_to_market_cap_min": 0.2,
        "sales_yoy_min": 0.05,
        "operating_profit_positive_required": True,
    },
    "diversity": {
        "max_recommended_per_sector": 1,
        "max_recommended_per_lane": 2,
        "max_previous_candidates_in_recommended": 2,
        "previous_overlap_warning_ratio": 0.6,
    },
}

BUILTIN_PROFILE_OVERRIDES: Mapping[str, Mapping[str, object]] = {
    "balanced": BALANCED_PROFILE_OVERRIDES,
}

if set(BUILTIN_PROFILE_OVERRIDES) != BUILTIN_SELECTION_PROFILES:
    raise RuntimeError("BUILTIN_PROFILE_OVERRIDES must match BUILTIN_SELECTION_PROFILES")


def load_profile_overrides(path: Path | None) -> dict[str, Mapping[str, object]]:
    if path is None:
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"profile YAML root must be a mapping: {path}")
    raw_profiles = payload.get("profiles", payload)
    if not isinstance(raw_profiles, Mapping):
        raise ValueError(f"profile YAML must contain a profiles mapping: {path}")
    profiles: dict[str, Mapping[str, object]] = {}
    for name, raw_profile in raw_profiles.items():
        if not isinstance(raw_profile, Mapping):
            raise ValueError(f"profile {name!r} must be a mapping: {path}")
        profiles[str(name)] = raw_profile
    return profiles


def resolve_selection_rules(
    base: SelectionRules,
    *,
    profile: str,
    profile_overrides: Mapping[str, Mapping[str, object]] | None = None,
) -> SelectionRules:
    data = base.model_dump(mode="python")
    overrides = BUILTIN_PROFILE_OVERRIDES.get(profile)
    if overrides is None:
        overrides = (profile_overrides or {}).get(profile)
    elif profile_overrides and profile in profile_overrides:
        overrides = _deep_merge(overrides, profile_overrides[profile])
    if overrides is None:
        raise ValueError(f"unknown selection profile: {profile}")
    if overrides is not None:
        data = _deep_merge(data, overrides)
    data["default_profile"] = (
        profile if profile in BUILTIN_SELECTION_PROFILES else base.default_profile
    )
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
