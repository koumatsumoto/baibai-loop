"""Validate retained E[r] evidence without relabeling an older rules population."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from pydantic import ValidationError

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.screening.rule_config import DEFAULT_RULES_PATH, load_screening_rules
from baibai_engine.screening.rules_identity import production_rules_contract_hash
from baibai_engine.valuation.calibration.contracts import (
    ER_CALIBRATION_CONTEXT_SCHEMA_VERSION,
    ErCalibrationContextArtifact,
)
from baibai_engine.valuation.estimates import EXPECTED_RETURN_MODEL_VERSION

_ROOT = Path(__file__).resolve().parents[3]
_CONTEXT = _ROOT / "reports/published/er-level-calibration-latest.yaml"


def check() -> tuple[str, ...]:
    """Return every identity mismatch; artifact freshness stays an operations concern."""

    try:
        raw = safe_load(_CONTEXT.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        return (f"unable to read {_CONTEXT.relative_to(_ROOT)}: {exc}",)
    if not isinstance(raw, dict):
        return ("published E[r] calibration context root must be a mapping",)
    expected_rules_hash = production_rules_contract_hash(
        load_screening_rules(_ROOT / DEFAULT_RULES_PATH).model_dump_json()
    )
    failures: list[str] = []
    try:
        ErCalibrationContextArtifact.model_validate(raw)
    except ValidationError:
        failures.append("published E[r] context does not satisfy the schema v2 contract")
    if raw.get("schema_version") != ER_CALIBRATION_CONTEXT_SCHEMA_VERSION:
        failures.append(
            "published E[r] context schema does not match the current reader: "
            f"{raw.get('schema_version')!r} != {ER_CALIBRATION_CONTEXT_SCHEMA_VERSION}"
        )
    if raw.get("screening_rules_hash") != expected_rules_hash:
        # A rules change may precede a new long-horizon context. Retain the original
        # evidence for its saved runs; runtime readers still demand exact run identity.
        known = set()
        for path in (_ROOT / DEFAULT_RULES_PATH).parent.glob("*.yaml"):
            try:
                payload = load_screening_rules(path).model_dump(mode="json")
                frozen = safe_load(path.read_text(encoding="utf-8"))
                quality = frozen.get("quality", {}) if isinstance(frozen, dict) else {}
                # The retired knob was serialized in the original publication's hash.
                # Preserve that provenance here; current writers no longer consume it.
                if "yoy_deterioration_threshold" in quality:
                    payload["quality"]["yoy_deterioration_threshold"] = quality[
                        "yoy_deterioration_threshold"
                    ]
                known.add(
                    production_rules_contract_hash(
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                    )
                )
            except ValidationError:
                # Older frozen schemas cannot be interpreted by the current writer.
                continue
        rules_hash = raw.get("screening_rules_hash")
        if not isinstance(rules_hash, str) or rules_hash not in known:
            failures.append(
                "published E[r] context screening_rules_hash has no known rules revision"
            )
    if raw.get("er_model_version") != EXPECTED_RETURN_MODEL_VERSION:
        failures.append(
            "published E[r] context er_model_version is stale; regenerate it in the same PR"
        )
    return tuple(failures)


def main() -> int:
    failures = check()
    if failures:
        for failure in failures:
            print(f"error: {failure}")
        return 1
    print(
        "ok: retained E[r] context has a known rules identity; readers require exact run identity"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
