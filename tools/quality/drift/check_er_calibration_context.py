"""Validate retained E[r] evidence without relabeling an older rules population."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml
from pydantic import ValidationError

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.screening.calibration.identity import rules_contract_hash
from baibai_engine.screening.rule_config import DEFAULT_RULES_PATH, load_screening_rules
from baibai_engine.screening.rules_identity import production_rules_contract_hash
from baibai_engine.valuation.calibration.contracts import (
    ER_CALIBRATION_CONTEXT_SCHEMA_VERSION,
    ErCalibrationContextArtifact,
)
from baibai_engine.valuation.estimates import EXPECTED_RETURN_MODEL_VERSION

_ROOT = Path(__file__).resolve().parents[3]
_CONTEXT = _ROOT / "reports/published/er-level-calibration-latest.yaml"
_RETAINED_SCHEMA_2_SHA256 = "28ac630de29b0011df1505b081e7428b63f11da0a53fd8d9f460b1c3d3dce670"


def check() -> tuple[str, ...]:
    """Return every identity mismatch; artifact freshness stays an operations concern."""

    try:
        original_bytes = _CONTEXT.read_bytes()
        raw = safe_load(original_bytes.decode("utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        return (f"unable to read {_CONTEXT.relative_to(_ROOT)}: {exc}",)
    if not isinstance(raw, dict):
        return ("published E[r] calibration context root must be a mapping",)
    expected_rules_hash = production_rules_contract_hash(
        load_screening_rules(_ROOT / DEFAULT_RULES_PATH).model_dump_json()
    )
    failures: list[str] = []
    if raw.get("schema_version") == 2:
        # Retention only: pin the original evidence bytes, without interpreting
        # cohort-relative statistics as schema 3 fixed-band statistics (#1493).
        # Runtime readers reject schema 2, even for a matching historical method.
        if hashlib.sha256(original_bytes).hexdigest() != _RETAINED_SCHEMA_2_SHA256:
            failures.append("retained schema 2 E[r] context bytes differ from the original")
    else:
        try:
            ErCalibrationContextArtifact.model_validate(raw)
        except ValidationError:
            failures.append("published E[r] context does not satisfy the current context contract")
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
                rules_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                known.add(production_rules_contract_hash(rules_json))
                # Retained evidence predates the one-bar scope. Recognize its
                # original hash for provenance without making it current at runtime.
                known.add(production_rules_contract_hash(rules_json, universe_scope_revision=""))
                # Retention only: v25 evidence keeps its original calculation and
                # input-window contract, before or after the one-bar scope change.
                # Do not derive these historical facts from current runtime values.
                for scope_revision in ("", "valid-price-one-bar-v2"):
                    known.add(
                        rules_contract_hash(
                            rules_json,
                            universe_scope_revision=scope_revision,
                            valuation_calculation_revision="same-profit-basis-yoy-v25",
                            variant="production",
                            valuation_history_sessions=750,
                            bars_input_window_days=1200,
                            production_authority=True,
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
