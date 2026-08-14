"""Validate or freeze a Stage A Macro World Model workspace."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from pathlib import Path
from typing import TextIO, cast

from tools.experiments.macro_world_model.build_evidence_snapshot import (
    READING_SEMANTIC_FIELDS,
    canonical_payload_sha256,
)

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.macro.context.models import MacroContextDocument

BLIND_FILES = (
    "charter.yaml",
    "evidence-snapshot.json",
    "evidence-packs.yaml",
    "states.yaml",
    "hypotheses.yaml",
    "evidence-matrix.yaml",
    "world-model.yaml",
)
HORIZONS = ("now", "0_3m", "3_12m", "12_24m")
STATE_DIMENSIONS = ("level", "momentum", "acceleration", "breadth", "persistence")
UNCERTAINTY_DIMENSIONS = ("data", "state", "structural", "policy")
NODE_TYPES = {
    "exogenous_shock",
    "initial_condition",
    "observed_state",
    "latent_state",
    "constraint",
    "expectation",
    "policy_reaction",
    "real_outcome",
    "nominal_outcome",
    "financial_outcome",
}
RELATION_KINDS = {
    "accounting_identity",
    "institutional_rule",
    "externally_identified_empirical_relation",
    "internal_observational_association",
    "market_implied_relation",
    "model_based_relation",
    "judgmental_hypothesis",
}
CLAIM_STRENGTHS = {"identified", "supported", "plausible", "speculative"}
CLAIM_STRENGTH_ORDER = {
    "speculative": 0,
    "plausible": 1,
    "supported": 2,
    "identified": 3,
}
SIGNS = {"positive", "negative", "nonlinear", "ambiguous"}
REAL_NOMINAL_BASES = {"real", "nominal", "mixed", "not_applicable"}
STOCK_FLOW_BASES = {"stock", "flow", "ratio", "mixed", "not_applicable"}
OBSERVATION_EXPECTATION_BASES = {"observation", "expectation", "mixed"}
PROJECTION_ARTIFACTS = {"one_page", "v4"}
V4_NON_JUDGMENT_KEYS = {
    "section_id",
    "series_id",
    "series_ids",
    "source_ids",
    "core_section_ids",
    "force_id",
    "force_ids",
    "fact_summary",
    "previous_scorecard_snapshot_id",
}
PRIOR_REFERENCE = re.compile(r"macro-context-\d{4}-\d{2}-\d{2}")
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
LEGACY_V1_WORKSPACE_SHA256 = frozenset(
    {
        "eb778fbf2a8a1cdc9f734faa979a33d4c62ddfc47acf2efc0090a921d343ed2e",
        "e33acb7e4208139044795376a0f2c3a4997ba30c73adaa6deabb58eeadcc1bb7",
    }
)


class WorldModelValidationError(ValueError):
    """Raised when a workspace violates the Stage A contract."""


@dataclass(frozen=True)
class GraphIndex:
    node_ids: frozenset[str]
    edge_ids: frozenset[str]
    edge_endpoints: Mapping[str, tuple[str, str]]
    edge_strengths: Mapping[str, str]
    edge_evidence_ids: Mapping[str, frozenset[str]]


@dataclass(frozen=True)
class WorldModelIndex:
    schema_version: int
    scenario_ranks: Mapping[str, int]
    judgment_ids: frozenset[str]
    scenario_ids: frozenset[str]
    judgment_evidence_ids: Mapping[str, frozenset[str]]
    scenario_evidence_ids: Mapping[str, frozenset[str]]
    graph: GraphIndex
    consumer_node_ids: frozenset[str]
    consumer_edge_ids: frozenset[str]


@dataclass(frozen=True)
class SnapshotIndex:
    schema_version: int
    as_of: date | None
    evidence_ids: frozenset[str]
    selected_ids: frozenset[str]
    measure_units: Mapping[str, Mapping[str, frozenset[str]]]


@dataclass(frozen=True)
class OnePageField:
    pointer: str
    value: str | int
    bound_source: tuple[str, str] | None = None


@dataclass(frozen=True)
class OnePageLine:
    section: str
    rendered_text: str
    fields: tuple[OnePageField, ...]


@dataclass(frozen=True)
class ClaimQualifier:
    claim_strength: str
    uncertainty: frozenset[str]
    real_nominal: str
    stock_flow: str
    observation_expectation: str
    units: frozenset[str]


@dataclass(frozen=True)
class ProjectionMetrics:
    schema_version: int | None = None
    lineage_enforced: bool = False
    material_claim_count: int = 0
    mapped_target_count: int = 0
    mapped_selected_evidence_count: int | None = None
    true_orphan_node_count: int | None = None
    true_orphan_edge_count: int | None = None


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise WorldModelValidationError(f"{label} must be a mapping")
    return cast(Mapping[str, object], value)


def _mapping_list(value: object, *, label: str) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise WorldModelValidationError(f"{label} must be a list of mappings")
    return [cast(Mapping[str, object], item) for item in value]


def _strings(value: object, *, label: str, minimum: int = 1) -> list[str]:
    if (
        not isinstance(value, list)
        or not all(isinstance(item, str) and item.strip() for item in value)
        or len(value) < minimum
    ):
        raise WorldModelValidationError(f"{label} must contain at least {minimum} strings")
    return cast(list[str], value)


def _required_text(row: Mapping[str, object], key: str, *, label: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WorldModelValidationError(f"{label}.{key} is required")
    return value


def _schema_version(document: Mapping[str, object], *, label: str) -> int:
    value = document.get("schema_version")
    if not isinstance(value, int) or isinstance(value, bool) or value not in {1, 2}:
        raise WorldModelValidationError(f"{label}.schema_version must be 1 or 2")
    return value


def _iso_date(value: object, *, label: str, optional: bool = False) -> date | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or ISO_DATE.fullmatch(value) is None:
        suffix = " or null" if optional else ""
        raise WorldModelValidationError(f"{label} must be an ISO date{suffix}")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise WorldModelValidationError(f"{label} must be an ISO date") from error


def _integer(
    value: object,
    *,
    label: str,
    minimum: int | None = None,
    optional: bool = False,
) -> int | None:
    if value is None and optional:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        suffix = " or null" if optional else ""
        raise WorldModelValidationError(f"{label} must be an integer{suffix}")
    if minimum is not None and value < minimum:
        qualifier = "positive" if minimum == 1 else f">= {minimum}"
        raise WorldModelValidationError(f"{label} must be {qualifier}")
    return value


def _require_exact_keys(
    row: Mapping[str, object],
    *,
    required: set[str],
    optional: set[str] | None = None,
    label: str,
) -> None:
    optional_keys = optional or set()
    actual = {str(key) for key in row}
    missing = required - actual
    extra = actual - required - optional_keys
    if missing:
        raise WorldModelValidationError(f"{label} is missing fields: {sorted(missing)}")
    if extra:
        raise WorldModelValidationError(f"{label} has unknown fields: {sorted(extra)}")


def _json_pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _resolve_json_pointer(document: object, pointer: str, *, label: str) -> object:
    if not pointer.startswith("/"):
        raise WorldModelValidationError(f"{label} must be an absolute JSON pointer")
    current = document
    for encoded in pointer[1:].split("/"):
        token = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                raise WorldModelValidationError(f"{label} does not resolve: {pointer}")
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit() or int(token) >= len(current):
                raise WorldModelValidationError(f"{label} does not resolve: {pointer}")
            current = current[int(token)]
        else:
            raise WorldModelValidationError(f"{label} does not resolve: {pointer}")
    return current


def _unique_ids(
    rows: Sequence[Mapping[str, object]], *, key: str, label: str
) -> dict[str, Mapping[str, object]]:
    result: dict[str, Mapping[str, object]] = {}
    for row in rows:
        identifier = _required_text(row, key, label=label)
        if identifier in result:
            raise WorldModelValidationError(f"duplicate {label} {key}: {identifier}")
        result[identifier] = row
    return result


def _load_yaml(path: Path) -> Mapping[str, object]:
    if not path.is_file():
        raise WorldModelValidationError(f"required workspace file is missing: {path.name}")
    return _mapping(safe_load(path.read_text(encoding="utf-8")), label=path.name)


def _load_json(path: Path) -> Mapping[str, object]:
    if not path.is_file():
        raise WorldModelValidationError(f"required workspace file is missing: {path.name}")
    with path.open(encoding="utf-8") as handle:
        return _mapping(json.load(handle), label=path.name)


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def blind_file_digests(workspace: Path) -> dict[str, str]:
    return {name: _file_digest(workspace / name) for name in BLIND_FILES}


def _aggregate_digest(files: Mapping[str, str]) -> str:
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def build_freeze_record(workspace: Path) -> dict[str, object]:
    files = blind_file_digests(workspace)
    return {
        "schema_version": 1,
        "kind": "macro-world-model-blind-freeze",
        "files": files,
        "workspace_sha256": _aggregate_digest(files),
    }


def verify_freeze(workspace: Path, freeze_path: Path) -> str:
    freeze = _load_json(freeze_path)
    stored_files = _mapping(freeze.get("files"), label="blind-freeze.files")
    expected = blind_file_digests(workspace)
    if dict(stored_files) != expected:
        raise WorldModelValidationError("blind-freeze file digests do not match the workspace")
    expected_digest = _aggregate_digest(expected)
    if freeze.get("workspace_sha256") != expected_digest:
        raise WorldModelValidationError("blind-freeze workspace_sha256 is invalid")
    return expected_digest


def _reject_prior_material(files: Mapping[str, Mapping[str, object]]) -> None:
    # Safe YAML loaders preserve unquoted ISO dates as ``date`` values.
    # Stringifying scalar extensions keeps the blind scan total over valid YAML.
    serialized = json.dumps(files, ensure_ascii=False, sort_keys=True, default=str)
    if PRIOR_REFERENCE.search(serialized):
        raise WorldModelValidationError("blind workspace contains a prior macro context id")
    forbidden_keys = {
        "previous_head",
        "previous_model",
        "previous_monitoring",
        "previous_report",
        "previous_scorecard",
        "prior_head",
        "prior_model",
        "prior_monitoring",
        "prior_report",
        "revision_diff",
    }

    def walk(value: object) -> None:
        if isinstance(value, Mapping):
            overlap = forbidden_keys & {str(key) for key in value}
            if overlap:
                raise WorldModelValidationError(
                    f"blind workspace contains prior-derived fields: {sorted(overlap)}"
                )
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    walk(files)


def _snapshot_measure_units(row: Mapping[str, object]) -> dict[str, frozenset[str]]:
    raw_unit = cast(str, row["unit"])
    statistic_unit = cast(str, row["statistic_unit"])
    result: dict[str, frozenset[str]] = {}

    def register(measure: str, value: object, *units: str) -> None:
        if value is not None:
            result[measure] = frozenset(units)

    register("latest_value", row.get("latest_value"), raw_unit)
    register("observed_at", row.get("observed_at"), "date")
    register("statistic_value", row.get("statistic_value"), statistic_unit)
    register("z_score", row.get("z_score"), "dimensionless")
    register("percentile", row.get("percentile"), "dimensionless")
    register("window_years", row.get("window_years"), "years")
    register("window_observations", row.get("window_observations"), "observations")
    register("expected_observations", row.get("expected_observations"), "observations")
    register("next_print_estimate", row.get("next_print_estimate"), "date")
    register("print_due_in_days", row.get("print_due_in_days"), "days")
    register("revisions_truncated", row.get("revisions_truncated"), "observations")

    release_change = row.get("release_change")
    if isinstance(release_change, Mapping):
        release_unit = release_change.get("unit")
        if not isinstance(release_unit, str) or not release_unit.strip():
            release_unit = raw_unit
        for field in ("from_value", "to_value", "absolute_change"):
            register(f"release_change.{field}", release_change.get(field), release_unit)
        register("release_change.percent_change", release_change.get("percent_change"), "percent")

    revisions = row.get("revisions")
    if isinstance(revisions, list) and revisions:
        revision_units = {
            str(vintage["unit"])
            for revision in revisions
            if isinstance(revision, Mapping)
            for vintage in revision.get("vintages", [])
            if isinstance(vintage, Mapping)
            and isinstance(vintage.get("unit"), str)
            and str(vintage["unit"]).strip()
        }
        if revision_units:
            result["revisions.value"] = frozenset(revision_units)
        result["revisions.observed_at"] = frozenset({"date"})
        result["revisions.vintage_at"] = frozenset({"date"})
    return result


def _snapshot_evidence_ids(snapshot: Mapping[str, object]) -> SnapshotIndex:
    schema_version = _schema_version(snapshot, label="evidence-snapshot")
    snapshot_as_of = (
        _iso_date(snapshot.get("as_of"), label="evidence-snapshot.as_of")
        if schema_version == 2
        else None
    )
    stored_hash = snapshot.get("canonical_payload_sha256")
    if not isinstance(stored_hash, str) or stored_hash != canonical_payload_sha256(snapshot):
        raise WorldModelValidationError("evidence snapshot canonical hash is invalid")
    scan = _mapping_list(snapshot.get("coverage_scan"), label="evidence-snapshot.coverage_scan")
    identifiers = _unique_ids(scan, key="evidence_id", label="evidence")
    if not identifiers:
        raise WorldModelValidationError("evidence snapshot is empty")
    measure_units: dict[str, Mapping[str, frozenset[str]]] = {}
    for evidence_id, row in identifiers.items():
        if schema_version == 2:
            assert snapshot_as_of is not None
            missing = [field for field in READING_SEMANTIC_FIELDS if field not in row]
            if missing:
                raise WorldModelValidationError(
                    f"evidence {evidence_id} is missing reading semantic fields: {missing}"
                )
            if not isinstance(row.get("unit"), str) or not str(row["unit"]).strip():
                raise WorldModelValidationError(f"evidence {evidence_id}.unit is required")
            _integer(
                row.get("window_years"),
                label=f"evidence {evidence_id}.window_years",
                minimum=1,
            )
            _integer(
                row.get("window_observations"),
                label=f"evidence {evidence_id}.window_observations",
                minimum=0,
            )
            _integer(
                row.get("expected_observations"),
                label=f"evidence {evidence_id}.expected_observations",
                minimum=1,
                optional=True,
            )
            if row.get("statistic") not in {"level", "yoy"}:
                raise WorldModelValidationError(
                    f"evidence {evidence_id}.statistic must be level or yoy"
                )
            if (
                not isinstance(row.get("statistic_unit"), str)
                or not str(row["statistic_unit"]).strip()
            ):
                raise WorldModelValidationError(
                    f"evidence {evidence_id}.statistic_unit is required"
                )
            statistic_value = row.get("statistic_value")
            if statistic_value is not None and (
                isinstance(statistic_value, bool)
                or not isinstance(statistic_value, int | float)
                or not math.isfinite(statistic_value)
            ):
                raise WorldModelValidationError(
                    f"evidence {evidence_id}.statistic_value must be finite or null"
                )
            next_print = _iso_date(
                row.get("next_print_estimate"),
                label=f"evidence {evidence_id}.next_print_estimate",
                optional=True,
            )
            print_due = _integer(
                row.get("print_due_in_days"),
                label=f"evidence {evidence_id}.print_due_in_days",
                optional=True,
            )
            if (next_print is None) != (print_due is None):
                raise WorldModelValidationError(
                    f"evidence {evidence_id} next print date and due days must both be null "
                    "or both populated"
                )
            if next_print is not None and print_due != (next_print - snapshot_as_of).days:
                raise WorldModelValidationError(
                    f"evidence {evidence_id}.print_due_in_days does not match snapshot as_of"
                )
            measure_units[evidence_id] = _snapshot_measure_units(row)
    manifest = _mapping_list(
        snapshot.get("coverage_manifest"), label="evidence-snapshot.coverage_manifest"
    )
    _unique_ids(manifest, key="candidate_id", label="coverage candidate")
    selected: set[str] = set()
    for row in manifest:
        candidate_id = _required_text(row, "candidate_id", label="coverage candidate")
        if candidate_id not in identifiers:
            raise WorldModelValidationError(
                f"coverage manifest references unknown evidence: {candidate_id}"
            )
        status = row.get("status")
        if status not in {"selected", "excluded"}:
            raise WorldModelValidationError(
                f"coverage candidate {candidate_id} has invalid status: {status}"
            )
        if status == "excluded":
            _required_text(row, "exclusion_reason", label=f"coverage candidate {candidate_id}")
        else:
            selected.add(candidate_id)
    return SnapshotIndex(
        schema_version=schema_version,
        as_of=snapshot_as_of,
        evidence_ids=frozenset(identifiers),
        selected_ids=frozenset(selected),
        measure_units=measure_units,
    )


def _validate_evidence_packs(
    document: Mapping[str, object],
    *,
    snapshot_ids: set[str],
    selected_snapshot_ids: set[str],
) -> set[str]:
    external_sources = _mapping_list(
        document.get("external_sources"), label="evidence-packs.external_sources"
    )
    source_index = _unique_ids(external_sources, key="evidence_id", label="external source")
    overlap = set(source_index) & snapshot_ids
    if overlap:
        raise WorldModelValidationError(
            f"external source ids collide with snapshot evidence: {sorted(overlap)}"
        )
    for evidence_id, source in source_index.items():
        for key in ("publisher", "title", "published_at", "url"):
            _required_text(source, key, label=f"external source {evidence_id}")
        if source.get("source_tier") not in {1, 2}:
            raise WorldModelValidationError(
                f"external source {evidence_id}.source_tier must be 1 or 2"
            )

    all_evidence_ids = snapshot_ids | set(source_index)
    packs = _mapping_list(document.get("packs"), label="evidence-packs.packs")
    pack_index = _unique_ids(packs, key="pack_id", label="evidence pack")
    if not pack_index:
        raise WorldModelValidationError("evidence packs are empty")
    referenced: set[str] = set()
    for pack_id, pack in pack_index.items():
        _required_text(pack, "block", label=f"evidence pack {pack_id}")
        for key in ("evidence_for", "evidence_against"):
            refs = set(_strings(pack.get(key), label=f"evidence pack {pack_id}.{key}"))
            unknown = refs - all_evidence_ids
            if unknown:
                raise WorldModelValidationError(
                    f"evidence pack {pack_id} references unknown evidence: {sorted(unknown)}"
                )
            referenced.update(refs)
        _strings(pack.get("open_questions"), label=f"evidence pack {pack_id}.open_questions")

    uncovered = selected_snapshot_ids - referenced
    if uncovered:
        raise WorldModelValidationError(
            f"selected coverage is absent from evidence packs: {sorted(uncovered)}"
        )
    unused_sources = set(source_index) - referenced
    if unused_sources:
        raise WorldModelValidationError(
            f"external sources are absent from evidence packs: {sorted(unused_sources)}"
        )
    return all_evidence_ids


def _require_evidence_refs(
    row: Mapping[str, object],
    *,
    key: str,
    label: str,
    evidence_ids: set[str],
) -> list[str]:
    references = _strings(row.get(key), label=f"{label}.{key}")
    unknown = set(references) - evidence_ids
    if unknown:
        raise WorldModelValidationError(f"{label} references unknown evidence: {sorted(unknown)}")
    return references


def _validate_charter(charter: Mapping[str, object]) -> None:
    _required_text(charter, "as_of", label="charter")
    _required_text(charter, "data_cutoff", label="charter")
    questions = _strings(
        charter.get("central_questions"), label="charter.central_questions", minimum=6
    )
    if len(questions) != 6:
        raise WorldModelValidationError(
            "charter.central_questions must contain exactly 6 questions"
        )
    horizons = charter.get("horizons")
    if horizons != list(HORIZONS):
        raise WorldModelValidationError(f"charter.horizons must equal {list(HORIZONS)}")
    _strings(charter.get("conditioning_assumptions"), label="charter.conditioning_assumptions")
    _strings(charter.get("standing_questions"), label="charter.standing_questions", minimum=5)


def _validate_states(
    document: Mapping[str, object], *, evidence_ids: set[str]
) -> dict[str, Mapping[str, object]]:
    states = _mapping_list(document.get("states"), label="states.states")
    by_id = _unique_ids(states, key="state_id", label="state")
    for state_id, state in by_id.items():
        _required_text(state, "geography", label=f"state {state_id}")
        _required_text(state, "block", label=f"state {state_id}")
        _required_text(state, "reference_frame", label=f"state {state_id}")
        horizons = _mapping(state.get("horizons"), label=f"state {state_id}.horizons")
        if set(horizons) != set(HORIZONS):
            raise WorldModelValidationError(
                f"state {state_id} must carry exactly the horizons {list(HORIZONS)}"
            )
        for horizon in HORIZONS:
            dimensions = _mapping(horizons[horizon], label=f"state {state_id}.{horizon}")
            if set(dimensions) != set(STATE_DIMENSIONS):
                raise WorldModelValidationError(
                    f"state {state_id}.{horizon} must contain exactly {list(STATE_DIMENSIONS)}"
                )
            for dimension in STATE_DIMENSIONS:
                _required_text(
                    dimensions,
                    dimension,
                    label=f"state {state_id}.{horizon}",
                )
        _require_evidence_refs(
            state,
            key="evidence_for",
            label=f"state {state_id}",
            evidence_ids=evidence_ids,
        )
        _require_evidence_refs(
            state,
            key="evidence_against",
            label=f"state {state_id}",
            evidence_ids=evidence_ids,
        )
        uncertainty = _mapping(state.get("uncertainty"), label=f"state {state_id}.uncertainty")
        if set(uncertainty) != set(UNCERTAINTY_DIMENSIONS):
            raise WorldModelValidationError(
                f"state {state_id}.uncertainty must contain exactly {list(UNCERTAINTY_DIMENSIONS)}"
            )
        for dimension in UNCERTAINTY_DIMENSIONS:
            _required_text(uncertainty, dimension, label=f"state {state_id}.uncertainty")
    return by_id


def _validate_hypotheses(
    document: Mapping[str, object], *, evidence_ids: set[str]
) -> dict[str, Mapping[str, object]]:
    hypotheses = _mapping_list(document.get("hypotheses"), label="hypotheses.hypotheses")
    if not 2 <= len(hypotheses) <= 3:
        raise WorldModelValidationError("workspace requires 2 to 3 competing hypotheses")
    by_id = _unique_ids(hypotheses, key="hypothesis_id", label="hypothesis")
    ranks: set[int] = set()
    for hypothesis_id, hypothesis in by_id.items():
        _required_text(hypothesis, "mechanism_summary", label=f"hypothesis {hypothesis_id}")
        rank = hypothesis.get("plausibility_rank")
        if not isinstance(rank, int) or isinstance(rank, bool) or rank < 1:
            raise WorldModelValidationError(
                f"hypothesis {hypothesis_id}.plausibility_rank must be a positive integer"
            )
        ranks.add(rank)
        _require_evidence_refs(
            hypothesis,
            key="evidence_for",
            label=f"hypothesis {hypothesis_id}",
            evidence_ids=evidence_ids,
        )
        _require_evidence_refs(
            hypothesis,
            key="evidence_against",
            label=f"hypothesis {hypothesis_id}",
            evidence_ids=evidence_ids,
        )
        for key in (
            "unexplained_residuals",
            "required_assumptions",
            "discriminating_signposts",
            "invalidation",
        ):
            _strings(hypothesis.get(key), label=f"hypothesis {hypothesis_id}.{key}")
    if ranks != set(range(1, len(hypotheses) + 1)):
        raise WorldModelValidationError(
            "hypothesis plausibility ranks must be contiguous and unique"
        )
    return by_id


def _validate_matrix(
    document: Mapping[str, object],
    *,
    evidence_ids: set[str],
    hypothesis_ids: set[str],
) -> None:
    rows = _mapping_list(document.get("rows"), label="evidence-matrix.rows")
    if not rows:
        raise WorldModelValidationError("common evidence matrix is empty")
    _unique_ids(rows, key="evidence_id", label="evidence matrix row")
    for row in rows:
        evidence_id = _required_text(row, "evidence_id", label="evidence matrix row")
        if evidence_id not in evidence_ids:
            raise WorldModelValidationError(f"matrix references unknown evidence: {evidence_id}")
        assessments = _mapping(
            row.get("hypothesis_assessments"),
            label=f"matrix {evidence_id}.hypothesis_assessments",
        )
        if set(assessments) != hypothesis_ids:
            raise WorldModelValidationError(
                f"matrix {evidence_id} must assess every competing hypothesis"
            )
        for hypothesis_id, assessment in assessments.items():
            if assessment not in {"supports", "contradicts", "mixed", "neutral"}:
                raise WorldModelValidationError(
                    f"matrix {evidence_id}.{hypothesis_id} has invalid assessment: {assessment}"
                )


def _validate_graph(graph: Mapping[str, object], *, evidence_ids: set[str]) -> GraphIndex:
    nodes = _mapping_list(graph.get("nodes"), label="world-model.graph.nodes")
    edges = _mapping_list(graph.get("edges"), label="world-model.graph.edges")
    if len(nodes) > 20:
        raise WorldModelValidationError("graph exceeds the hard cap of 20 nodes")
    if len(edges) > 30:
        raise WorldModelValidationError("graph exceeds the hard cap of 30 edges")
    node_index = _unique_ids(nodes, key="node_id", label="node")
    for node_id, node in node_index.items():
        node_type = node.get("node_type")
        if node_type not in NODE_TYPES:
            raise WorldModelValidationError(f"node {node_id} has invalid node_type: {node_type}")
        _required_text(node, "label", label=f"node {node_id}")
    edge_index = _unique_ids(edges, key="edge_id", label="edge")
    edge_endpoints: dict[str, tuple[str, str]] = {}
    edge_strengths: dict[str, str] = {}
    edge_evidence_ids: dict[str, frozenset[str]] = {}
    for edge_id, edge in edge_index.items():
        source = _required_text(edge, "from_node", label=f"edge {edge_id}")
        target = _required_text(edge, "to_node", label=f"edge {edge_id}")
        if source not in node_index or target not in node_index:
            raise WorldModelValidationError(f"edge {edge_id} references an unknown node")
        if edge.get("relation_kind") not in RELATION_KINDS:
            raise WorldModelValidationError(f"edge {edge_id} has invalid relation_kind")
        claim_strength = edge.get("claim_strength")
        if claim_strength not in CLAIM_STRENGTHS:
            raise WorldModelValidationError(f"edge {edge_id} has invalid claim_strength")
        edge_endpoints[edge_id] = (source, target)
        edge_strengths[edge_id] = claim_strength
        if edge.get("sign") not in SIGNS:
            raise WorldModelValidationError(f"edge {edge_id} has invalid sign")
        lag = _mapping(edge.get("lag"), label=f"edge {edge_id}.lag")
        minimum = lag.get("min_months")
        maximum = lag.get("max_months")
        if (
            not isinstance(minimum, int)
            or isinstance(minimum, bool)
            or not isinstance(maximum, int)
            or isinstance(maximum, bool)
            or minimum < 0
            or maximum < minimum
        ):
            raise WorldModelValidationError(f"edge {edge_id} has an invalid lag range")
        edge_evidence_ids[edge_id] = frozenset(
            _require_evidence_refs(
                edge,
                key="evidence_ids",
                label=f"edge {edge_id}",
                evidence_ids=evidence_ids,
            )
        )
        falsifiers = _mapping_list(edge.get("falsifiers"), label=f"edge {edge_id}.falsifiers")
        if not falsifiers:
            raise WorldModelValidationError(f"edge {edge_id} requires an observable falsifier")
        for falsifier in falsifiers:
            if falsifier.get("kind") not in {"series", "release_event"}:
                raise WorldModelValidationError(
                    f"edge {edge_id} falsifier kind must be series or release_event"
                )
            _required_text(falsifier, "name", label=f"edge {edge_id} falsifier")
    return GraphIndex(
        node_ids=frozenset(node_index),
        edge_ids=frozenset(edge_index),
        edge_endpoints=edge_endpoints,
        edge_strengths=edge_strengths,
        edge_evidence_ids=edge_evidence_ids,
    )


def _reference_ids(
    row: Mapping[str, object],
    *,
    key: str,
    known: set[str] | frozenset[str],
    label: str,
    minimum: int = 1,
) -> set[str]:
    references = set(_strings(row.get(key), label=f"{label}.{key}", minimum=minimum))
    unknown = references - set(known)
    if unknown:
        raise WorldModelValidationError(f"{label} references unknown {key}: {sorted(unknown)}")
    return references


def _validate_subgraph_refs(
    *,
    node_refs: set[str],
    edge_refs: set[str],
    graph: GraphIndex,
    label: str,
) -> None:
    required_nodes = {node_id for edge_id in edge_refs for node_id in graph.edge_endpoints[edge_id]}
    missing = required_nodes - node_refs
    if missing:
        raise WorldModelValidationError(
            f"{label}.node_ids must include endpoints of referenced edges: {sorted(missing)}"
        )


def _validate_world_model(
    document: Mapping[str, object],
    *,
    evidence_ids: set[str],
    state_ids: set[str],
    hypothesis_ids: set[str],
) -> WorldModelIndex:
    schema_version = _schema_version(document, label="world-model")
    graph = _mapping(document.get("graph"), label="world-model.graph")
    graph_index = _validate_graph(graph, evidence_ids=evidence_ids)
    judgments = _mapping_list(document.get("key_judgments"), label="world-model.key_judgments")
    if not 3 <= len(judgments) <= 5:
        raise WorldModelValidationError("world model requires 3 to 5 key judgments")
    judgment_index = _unique_ids(judgments, key="judgment_id", label="key judgment")
    consumer_node_ids: set[str] = set()
    consumer_edge_ids: set[str] = set()
    judgment_evidence_ids: dict[str, frozenset[str]] = {}
    for judgment in judgments:
        judgment_id = _required_text(judgment, "judgment_id", label="key judgment")
        _required_text(judgment, "summary", label=f"judgment {judgment_id}")
        state_refs = set(
            _strings(judgment.get("state_ids"), label=f"judgment {judgment_id}.state_ids")
        )
        if not state_refs <= state_ids:
            raise WorldModelValidationError(f"judgment {judgment_id} references unknown states")
        node_refs = set(
            _strings(judgment.get("node_ids"), label=f"judgment {judgment_id}.node_ids")
        )
        if not node_refs <= graph_index.node_ids:
            raise WorldModelValidationError(f"judgment {judgment_id} references unknown nodes")
        edge_refs = set(
            _strings(judgment.get("edge_ids"), label=f"judgment {judgment_id}.edge_ids")
        )
        if not edge_refs <= graph_index.edge_ids:
            raise WorldModelValidationError(f"judgment {judgment_id} references unknown edges")
        if schema_version == 2:
            _validate_subgraph_refs(
                node_refs=node_refs,
                edge_refs=edge_refs,
                graph=graph_index,
                label=f"judgment {judgment_id}",
            )
        consumer_node_ids.update(node_refs)
        consumer_edge_ids.update(edge_refs)
        horizon_refs = set(
            _strings(judgment.get("horizons"), label=f"judgment {judgment_id}.horizons")
        )
        if not horizon_refs <= set(HORIZONS):
            raise WorldModelValidationError(f"judgment {judgment_id} has an unknown horizon")
        counter_refs = set(
            _strings(
                judgment.get("counter_hypothesis_ids"),
                label=f"judgment {judgment_id}.counter_hypothesis_ids",
            )
        )
        if not counter_refs <= hypothesis_ids:
            raise WorldModelValidationError(
                f"judgment {judgment_id} references unknown counter hypotheses"
            )
        judgment_evidence_ids[judgment_id] = frozenset(
            _require_evidence_refs(
                judgment,
                key="evidence_ids",
                label=f"judgment {judgment_id}",
                evidence_ids=evidence_ids,
            )
        )

    baseline = _mapping(document.get("baseline_path"), label="world-model.baseline_path")
    if set(baseline) != set(HORIZONS):
        raise WorldModelValidationError(f"baseline_path must equal the horizons {list(HORIZONS)}")
    for horizon in HORIZONS:
        _strings(baseline[horizon], label=f"baseline_path.{horizon}")

    scenarios = _mapping_list(document.get("scenarios"), label="world-model.scenarios")
    if schema_version == 2 and len(scenarios) != 3:
        raise WorldModelValidationError("world-model v2 requires exactly 3 scenarios")
    if schema_version == 1 and not 2 <= len(scenarios) <= 4:
        raise WorldModelValidationError("world model requires 2 to 4 scenarios")
    scenario_index = _unique_ids(scenarios, key="scenario_id", label="scenario")
    ranks: set[int] = set()
    scenario_evidence_ids: dict[str, frozenset[str]] = {}
    for scenario in scenarios:
        scenario_id = _required_text(scenario, "scenario_id", label="scenario")
        _required_text(scenario, "name", label=f"scenario {scenario_id}")
        for key in ("initial_shock", "persistence", "propagation_delta", "policy_reaction"):
            _required_text(scenario, key, label=f"scenario {scenario_id}")
        rank = scenario.get("plausibility_rank")
        if not isinstance(rank, int) or isinstance(rank, bool) or rank < 1:
            raise WorldModelValidationError(
                f"scenario {scenario_id}.plausibility_rank must be a positive integer"
            )
        ranks.add(rank)
        paths = _mapping(scenario.get("paths"), label=f"scenario {scenario_id}.paths")
        required_paths = {"growth", "inflation", "rates", "credit", "fx", "balance_sheet"}
        if set(paths) != required_paths:
            raise WorldModelValidationError(
                f"scenario {scenario_id}.paths must cover {sorted(required_paths)}"
            )
        for path_name in sorted(required_paths):
            _required_text(paths, path_name, label=f"scenario {scenario_id}.paths")
        for key in ("signposts", "invalidation", "known_omissions"):
            _strings(scenario.get(key), label=f"scenario {scenario_id}.{key}")
        if schema_version == 2:
            _reference_ids(
                scenario,
                key="state_ids",
                known=state_ids,
                label=f"scenario {scenario_id}",
            )
            _reference_ids(
                scenario,
                key="hypothesis_ids",
                known=hypothesis_ids,
                label=f"scenario {scenario_id}",
            )
            scenario_node_refs = _reference_ids(
                scenario,
                key="node_ids",
                known=graph_index.node_ids,
                label=f"scenario {scenario_id}",
            )
            scenario_edge_refs = _reference_ids(
                scenario,
                key="edge_ids",
                known=graph_index.edge_ids,
                label=f"scenario {scenario_id}",
            )
            _validate_subgraph_refs(
                node_refs=scenario_node_refs,
                edge_refs=scenario_edge_refs,
                graph=graph_index,
                label=f"scenario {scenario_id}",
            )
            scenario_evidence_ids[scenario_id] = frozenset(
                _require_evidence_refs(
                    scenario,
                    key="evidence_ids",
                    label=f"scenario {scenario_id}",
                    evidence_ids=evidence_ids,
                )
            )
            consumer_node_ids.update(scenario_node_refs)
            consumer_edge_ids.update(scenario_edge_refs)
    if ranks != set(range(1, len(scenarios) + 1)):
        raise WorldModelValidationError("scenario plausibility ranks must be contiguous and unique")

    _strings(document.get("unresolved_tensions"), label="world-model.unresolved_tensions")
    _strings(document.get("signposts"), label="world-model.signposts")
    scenario_ranks = {
        str(row["scenario_id"]): cast(int, row["plausibility_rank"]) for row in scenarios
    }
    return WorldModelIndex(
        schema_version=schema_version,
        scenario_ranks=scenario_ranks,
        judgment_ids=frozenset(judgment_index),
        scenario_ids=frozenset(scenario_index),
        judgment_evidence_ids=judgment_evidence_ids,
        scenario_evidence_ids=scenario_evidence_ids,
        graph=graph_index,
        consumer_node_ids=frozenset(consumer_node_ids),
        consumer_edge_ids=frozenset(consumer_edge_ids),
    )


def _record_text_path(paths: set[str], *, pointer: str, value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise WorldModelValidationError(f"{label} must be non-blank text")
    paths.add(pointer)


def _record_scalar_path(paths: set[str], *, pointer: str, value: object, label: str) -> None:
    if isinstance(value, date):
        pass
    elif isinstance(value, str):
        if not value.strip():
            raise WorldModelValidationError(f"{label} must be non-blank")
    elif isinstance(value, int | float) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            raise WorldModelValidationError(f"{label} must be finite")
    else:
        raise WorldModelValidationError(f"{label} must be a scalar claim value")
    paths.add(pointer)


def _collect_scalar_paths(
    value: object,
    *,
    pointer: str,
    paths: set[str],
    label: str,
    ignored_keys: set[str] | None = None,
) -> None:
    ignored = ignored_keys or set()
    if value is None:
        return
    if isinstance(value, Mapping):
        for key, nested in value.items():
            key_text = str(key)
            if key_text in ignored:
                continue
            _collect_scalar_paths(
                nested,
                pointer=f"{pointer}/{_json_pointer_token(key_text)}",
                paths=paths,
                label=label,
                ignored_keys=ignored,
            )
        return
    if isinstance(value, list):
        for index, nested in enumerate(value):
            _collect_scalar_paths(
                nested,
                pointer=f"{pointer}/{index}",
                paths=paths,
                label=label,
                ignored_keys=ignored,
            )
        return
    _record_scalar_path(paths, pointer=pointer, value=value, label=f"{label}{pointer}")


def _canonical_v4_document(document: Mapping[str, object]) -> Mapping[str, object]:
    try:
        model = MacroContextDocument.model_validate(document)
    except ValueError as error:
        raise WorldModelValidationError(
            f"v4 document violates MacroContextDocument: {error}"
        ) from error
    return cast(Mapping[str, object], model.payload())


def _required_v4_paths(document: Mapping[str, object]) -> tuple[set[str], dict[str, str]]:
    paths: set[str] = set()
    scenario_cases: dict[str, str] = {}
    _record_text_path(paths, pointer="/summary", value=document.get("summary"), label="v4.summary")

    synthesis = _mapping(document.get("synthesis"), label="v4.synthesis")
    _collect_scalar_paths(
        synthesis,
        pointer="/synthesis",
        paths=paths,
        label="v4",
        ignored_keys=V4_NON_JUDGMENT_KEYS,
    )

    core = _mapping_list(document.get("core"), label="v4.core")
    for section_index, section in enumerate(core):
        section_id = _required_text(section, "section_id", label="v4 core section")
        _collect_scalar_paths(
            section,
            pointer=f"/core/{section_index}",
            paths=paths,
            label="v4",
            ignored_keys=V4_NON_JUDGMENT_KEYS,
        )
        scenarios = _mapping_list(section.get("scenarios", []), label=f"v4.{section_id}.scenarios")
        for scenario_index, scenario in enumerate(scenarios):
            case = _required_text(scenario, "case", label="v4 scenario")
            base = f"/core/{section_index}/scenarios/{scenario_index}"
            scenario_cases[base] = case

    connection = _mapping(document.get("connection"), label="v4.connection")
    _collect_scalar_paths(
        connection,
        pointer="/connection",
        paths=paths,
        label="v4",
        ignored_keys=V4_NON_JUDGMENT_KEYS,
    )
    return paths, scenario_cases


def _validate_revision_diff(revision_diff: Mapping[str, object], *, expected_as_of: date) -> None:
    if revision_diff.get("schema_version") != 1:
        raise WorldModelValidationError("revision-diff.schema_version must be 1")
    revision_as_of = _iso_date(revision_diff.get("as_of"), label="revision-diff.as_of")
    if revision_as_of != expected_as_of:
        raise WorldModelValidationError("revision-diff.as_of differs from cycle as_of")


def build_one_page_render_plan(
    world_model: Mapping[str, object],
    revision_diff: Mapping[str, object],
    *,
    expected_as_of: date,
) -> tuple[OnePageLine, ...]:
    """Build the single structured source used by one-page rendering and lineage."""

    _validate_revision_diff(revision_diff, expected_as_of=expected_as_of)
    plan: list[OnePageLine] = []
    judgments = _mapping_list(world_model.get("key_judgments"), label="world-model.key_judgments")
    for judgment in judgments:
        judgment_id = _required_text(judgment, "judgment_id", label="key judgment")
        summary = _required_text(judgment, "summary", label=f"judgment {judgment_id}")
        horizons = _strings(judgment.get("horizons"), label=f"judgment {judgment_id}.horizons")
        base = f"/key_judgments/{_json_pointer_token(judgment_id)}"
        fields = [
            OnePageField(f"{base}/summary", summary, ("judgment", judgment_id)),
            *(
                OnePageField(
                    f"{base}/horizons/{index}",
                    horizon,
                    ("judgment", judgment_id),
                )
                for index, horizon in enumerate(horizons)
            ),
        ]
        plan.append(
            OnePageLine(
                section="Key judgments",
                rendered_text=f"- {summary} (horizons: {', '.join(horizons)})",
                fields=tuple(fields),
            )
        )

    baseline = _mapping(world_model.get("baseline_path"), label="world-model.baseline_path")
    for horizon in HORIZONS:
        values = _strings(baseline[horizon], label=f"baseline_path.{horizon}")
        plan.append(
            OnePageLine(
                section="Horizon paths",
                rendered_text=f"- **{horizon}:** {'; '.join(values)}",
                fields=tuple(
                    OnePageField(f"/baseline_path/{horizon}/{index}", value)
                    for index, value in enumerate(values)
                ),
            )
        )
    scenarios = _mapping_list(world_model.get("scenarios"), label="world-model.scenarios")
    ranked_scenarios: list[tuple[int, Mapping[str, object]]] = []
    for scenario in scenarios:
        rank = scenario.get("plausibility_rank")
        if not isinstance(rank, int) or isinstance(rank, bool):
            raise WorldModelValidationError("scenario.plausibility_rank must be an integer")
        ranked_scenarios.append((rank, scenario))
    for rank, scenario in sorted(ranked_scenarios, key=lambda item: item[0]):
        scenario_id = _required_text(scenario, "scenario_id", label="scenario")
        scenario_values = {
            field: _required_text(scenario, field, label=f"scenario {scenario_id}")
            for field in ("name", "initial_shock", "propagation_delta", "policy_reaction")
        }
        base = f"/scenarios/{_json_pointer_token(scenario_id)}"
        plan.append(
            OnePageLine(
                section="Horizon paths",
                rendered_text=(
                    f"- **Scenario {rank} — {scenario_values['name']}:** "
                    f"shock: {scenario_values['initial_shock']}; "
                    f"propagation: {scenario_values['propagation_delta']}; "
                    f"policy: {scenario_values['policy_reaction']}"
                ),
                fields=(
                    OnePageField(f"{base}/plausibility_rank", rank, ("scenario", scenario_id)),
                    *(
                        OnePageField(
                            f"{base}/{field}",
                            value,
                            ("scenario", scenario_id),
                        )
                        for field, value in scenario_values.items()
                    ),
                ),
            )
        )

    for field, section in (
        ("unresolved_tensions", "Unresolved tensions"),
        ("signposts", "Signposts"),
    ):
        values = _strings(world_model.get(field), label=f"world-model.{field}")
        plan.extend(
            OnePageLine(
                section=section,
                rendered_text=f"- {value}",
                fields=(OnePageField(f"/{field}/{index}", value),),
            )
            for index, value in enumerate(values)
        )

    changes = _mapping_list(revision_diff.get("changes"), label="revision-diff.changes")
    if changes:
        for index, change in enumerate(changes):
            area = _required_text(change, "area", label="revision diff")
            summary = _required_text(change, "summary", label="revision diff")
            plan.append(
                OnePageLine(
                    section="What changed",
                    rendered_text=f"- {area}: {summary}",
                    fields=(
                        OnePageField(f"/revision_diff/changes/{index}/area", area),
                        OnePageField(f"/revision_diff/changes/{index}/summary", summary),
                    ),
                )
            )
    else:
        fallback = "No evidence-backed change is recorded."
        plan.append(
            OnePageLine(
                section="What changed",
                rendered_text=f"- {fallback}",
                fields=(OnePageField("/revision_diff/no_evidence_backed_change", fallback),),
            )
        )
    return tuple(plan)


def _required_one_page_paths(
    world_model: Mapping[str, object],
    revision_diff: Mapping[str, object],
    *,
    expected_as_of: date,
) -> tuple[set[str], dict[str, tuple[str, str]]]:
    paths: set[str] = set()
    bound_sources: dict[str, tuple[str, str]] = {}
    plan = build_one_page_render_plan(
        world_model,
        revision_diff,
        expected_as_of=expected_as_of,
    )
    for line in plan:
        for field in line.fields:
            _record_scalar_path(
                paths,
                pointer=field.pointer,
                value=field.value,
                label=f"one_page{field.pointer}",
            )
            if field.bound_source is not None:
                bound_sources[field.pointer] = field.bound_source
    return paths, bound_sources


def _parse_claim_qualifier(value: object, *, label: str) -> ClaimQualifier:
    qualifier = _mapping(value, label=label)
    _require_exact_keys(
        qualifier,
        required={"claim_strength", "uncertainty", "basis", "units"},
        label=label,
    )
    strength = qualifier.get("claim_strength")
    if strength not in CLAIM_STRENGTHS:
        raise WorldModelValidationError(f"{label}.claim_strength is invalid")
    uncertainty = _strings(qualifier.get("uncertainty"), label=f"{label}.uncertainty")
    if len(uncertainty) != len(set(uncertainty)):
        raise WorldModelValidationError(f"{label}.uncertainty must be unique")
    unknown_uncertainty = set(uncertainty) - set(UNCERTAINTY_DIMENSIONS)
    if unknown_uncertainty:
        raise WorldModelValidationError(
            f"{label}.uncertainty is invalid: {sorted(unknown_uncertainty)}"
        )
    basis = _mapping(qualifier.get("basis"), label=f"{label}.basis")
    _require_exact_keys(
        basis,
        required={"real_nominal", "stock_flow", "observation_expectation"},
        label=f"{label}.basis",
    )
    real_nominal = basis.get("real_nominal")
    stock_flow = basis.get("stock_flow")
    observation_expectation = basis.get("observation_expectation")
    if real_nominal not in REAL_NOMINAL_BASES:
        raise WorldModelValidationError(f"{label}.basis.real_nominal is invalid")
    if stock_flow not in STOCK_FLOW_BASES:
        raise WorldModelValidationError(f"{label}.basis.stock_flow is invalid")
    if observation_expectation not in OBSERVATION_EXPECTATION_BASES:
        raise WorldModelValidationError(f"{label}.basis.observation_expectation is invalid")
    units = _strings(qualifier.get("units"), label=f"{label}.units")
    if len(units) != len(set(units)):
        raise WorldModelValidationError(f"{label}.units must be unique")
    return ClaimQualifier(
        claim_strength=strength,
        uncertainty=frozenset(uncertainty),
        real_nominal=real_nominal,
        stock_flow=stock_flow,
        observation_expectation=observation_expectation,
        units=frozenset(units),
    )


def _validate_qualifier_projection(
    source: ClaimQualifier,
    output: ClaimQualifier,
    *,
    unit_transform: object,
    label: str,
) -> None:
    if CLAIM_STRENGTH_ORDER[output.claim_strength] > CLAIM_STRENGTH_ORDER[source.claim_strength]:
        raise WorldModelValidationError(f"{label} strengthens claim_strength")
    missing_uncertainty = source.uncertainty - output.uncertainty
    if missing_uncertainty:
        raise WorldModelValidationError(
            f"{label} drops uncertainty dimensions: {sorted(missing_uncertainty)}"
        )
    if (
        output.real_nominal != source.real_nominal
        or output.stock_flow != source.stock_flow
        or output.observation_expectation != source.observation_expectation
    ):
        raise WorldModelValidationError(f"{label} changes a quantity basis")
    if output.units == source.units:
        if unit_transform is not None:
            raise WorldModelValidationError(f"{label} has an unnecessary unit_transform")
        return
    transform = _mapping(unit_transform, label=f"{label}.unit_transform")
    _require_exact_keys(
        transform,
        required={"formula", "from_units", "to_units"},
        label=f"{label}.unit_transform",
    )
    _required_text(transform, "formula", label=f"{label}.unit_transform")
    from_units = frozenset(
        _strings(transform.get("from_units"), label=f"{label}.unit_transform.from_units")
    )
    to_units = frozenset(
        _strings(transform.get("to_units"), label=f"{label}.unit_transform.to_units")
    )
    if from_units != source.units or to_units != output.units:
        raise WorldModelValidationError(f"{label}.unit_transform does not match declared units")


def _require_lineage_evidence_overlap(
    *,
    claim_id: str,
    claim_evidence_ids: set[str],
    references: set[str],
    evidence_by_reference: Mapping[str, frozenset[str]],
    reference_kind: str,
) -> None:
    for reference in sorted(references):
        if not claim_evidence_ids & set(evidence_by_reference[reference]):
            raise WorldModelValidationError(
                f"material claim {claim_id} evidence does not overlap "
                f"{reference_kind} {reference} evidence"
            )


def _validate_v4_projection(
    path: Path,
    *,
    world_model: Mapping[str, object],
    world_index: WorldModelIndex,
    state_index: Mapping[str, Mapping[str, object]],
    hypothesis_index: Mapping[str, Mapping[str, object]],
    evidence_ids: set[str],
    snapshot: SnapshotIndex,
    v4_document_path: Path | None,
    revision_diff_path: Path | None,
) -> ProjectionMetrics:
    projection = _load_yaml(path)
    projection_version = _schema_version(projection, label="v4-projection")
    if projection_version != world_index.schema_version:
        raise WorldModelValidationError(
            "v4-projection schema version must match world-model schema version"
        )
    if projection_version == 2:
        _require_exact_keys(
            projection,
            required={"schema_version", "as_of", "scenarios", "material_claims"},
            optional={"projection_note"},
            label="v4-projection",
        )
        if "projection_note" in projection:
            _required_text(projection, "projection_note", label="v4-projection")
    rows = _mapping_list(projection.get("scenarios"), label="v4-projection.scenarios")
    _unique_ids(rows, key="scenario_id", label="v4 projection scenario")
    probabilities: dict[str, float] = {}
    probability_steps: dict[str, int] = {}
    case_to_scenario: dict[str, str] = {}
    for row in rows:
        scenario_id = _required_text(row, "scenario_id", label="v4 projection")
        value = row.get("probability")
        if (
            not isinstance(value, int | float)
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or not 0 <= float(value) <= 1
        ):
            raise WorldModelValidationError(f"v4 probability is invalid for {scenario_id}")
        probabilities[scenario_id] = float(value)
        if projection_version == 2:
            steps = round(float(value) * 20)
            if steps not in range(1, 19) or not math.isclose(
                float(value), steps / 20, abs_tol=1e-12
            ):
                raise WorldModelValidationError(
                    f"v4 probability for {scenario_id} must lie on the 0.05 grid "
                    "between 0.05 and 0.90"
                )
            probability_steps[scenario_id] = steps
            _require_exact_keys(
                row,
                required={"scenario_id", "v4_case", "plausibility_rank", "probability"},
                label=f"v4 projection scenario {scenario_id}",
            )
            case = row.get("v4_case")
            if case not in {"base", "bear", "bull"}:
                raise WorldModelValidationError(
                    f"v4 projection scenario {scenario_id}.v4_case is invalid"
                )
            if case in case_to_scenario:
                raise WorldModelValidationError(f"duplicate v4_case in projection: {case}")
            case_to_scenario[case] = scenario_id
            if row.get("plausibility_rank") != world_index.scenario_ranks.get(scenario_id):
                raise WorldModelValidationError(
                    f"v4 projection scenario {scenario_id} has a stale plausibility_rank"
                )
    if set(probabilities) != set(world_index.scenario_ranks):
        raise WorldModelValidationError("v4 projection must cover every world-model scenario")
    if projection_version == 2 and set(case_to_scenario) != {"base", "bear", "bull"}:
        raise WorldModelValidationError(
            "v4 projection must map exactly one base, bear, and bull scenario"
        )
    if projection_version == 2 and sum(probability_steps.values()) != 20:
        raise WorldModelValidationError("v4 projection probabilities must sum to 1.00")
    ordered = sorted(world_index.scenario_ranks, key=world_index.scenario_ranks.__getitem__)
    for more_plausible, less_plausible in pairwise(ordered):
        if probabilities[more_plausible] < probabilities[less_plausible]:
            raise WorldModelValidationError(
                "world-model plausibility order contradicts v4 probability order: "
                f"{more_plausible} < {less_plausible}"
            )
    if projection_version == 1:
        return ProjectionMetrics(schema_version=1)
    if v4_document_path is None or revision_diff_path is None:
        raise WorldModelValidationError(
            "v4-projection v2 requires --v4-document and --revision-diff"
        )
    v4_document = _canonical_v4_document(_load_yaml(v4_document_path))
    revision_diff = _load_yaml(revision_diff_path)
    projection_as_of = _iso_date(projection.get("as_of"), label="v4-projection.as_of")
    assert projection_as_of is not None
    for label, document in (("world-model", world_model), ("v4 document", v4_document)):
        document_as_of = _iso_date(document.get("as_of"), label=f"{label}.as_of")
        if document_as_of != projection_as_of:
            raise WorldModelValidationError(f"{label} as_of differs from v4-projection")

    required_v4_paths, v4_scenario_cases = _required_v4_paths(v4_document)
    v4_case_paths: dict[str, str] = {}
    for prefix, case in v4_scenario_cases.items():
        if case not in {"base", "bear", "bull"}:
            raise WorldModelValidationError(f"v4 scenario case is invalid: {case}")
        if case in v4_case_paths:
            raise WorldModelValidationError(f"duplicate v4 scenario case: {case}")
        v4_case_paths[case] = prefix
    if set(v4_case_paths) != set(case_to_scenario):
        raise WorldModelValidationError(
            "v4 scenario cases do not match v4-projection scenario mappings"
        )
    for case, prefix in v4_case_paths.items():
        scenario_id = case_to_scenario[case]
        v4_probability = _resolve_json_pointer(
            v4_document,
            f"{prefix}/probability",
            label=f"v4 {case} probability",
        )
        if (
            not isinstance(v4_probability, int | float)
            or isinstance(v4_probability, bool)
            or not math.isfinite(float(v4_probability))
            or not math.isclose(
                float(v4_probability),
                probabilities[scenario_id],
                abs_tol=1e-12,
            )
        ):
            raise WorldModelValidationError(
                f"v4 {case} probability differs from v4-projection scenario {scenario_id}"
            )
    required_one_page_paths, one_page_sources = _required_one_page_paths(
        world_model,
        revision_diff,
        expected_as_of=projection_as_of,
    )
    claims = _mapping_list(projection.get("material_claims"), label="v4-projection.material_claims")
    claim_index = _unique_ids(claims, key="claim_id", label="material claim")
    if not claim_index:
        raise WorldModelValidationError("v4 projection material_claims are empty")
    state_evidence_ids = {
        state_id: frozenset(
            _strings(state.get("evidence_for"), label=f"state {state_id}.evidence_for")
            + _strings(state.get("evidence_against"), label=f"state {state_id}.evidence_against")
        )
        for state_id, state in state_index.items()
    }
    hypothesis_evidence_ids = {
        hypothesis_id: frozenset(
            _strings(
                hypothesis.get("evidence_for"),
                label=f"hypothesis {hypothesis_id}.evidence_for",
            )
            + _strings(
                hypothesis.get("evidence_against"),
                label=f"hypothesis {hypothesis_id}.evidence_against",
            )
        )
        for hypothesis_id, hypothesis in hypothesis_index.items()
    }

    mapped_targets: set[tuple[str, str]] = set()
    mapped_evidence: set[str] = set()
    claim_node_ids: set[str] = set()
    claim_edge_ids: set[str] = set()
    for claim_id, claim in claim_index.items():
        _require_exact_keys(
            claim,
            required={"claim_id", "summary", "upstream", "source_qualifier", "targets"},
            label=f"material claim {claim_id}",
        )
        _required_text(claim, "summary", label=f"material claim {claim_id}")
        upstream = _mapping(claim.get("upstream"), label=f"material claim {claim_id}.upstream")
        _require_exact_keys(
            upstream,
            required={
                "state_ids",
                "hypothesis_ids",
                "judgment_ids",
                "scenario_ids",
                "node_ids",
                "edge_ids",
                "evidence",
            },
            label=f"material claim {claim_id}.upstream",
        )
        claim_state_ids = _reference_ids(
            upstream,
            key="state_ids",
            known=set(state_index),
            label=f"material claim {claim_id}",
            minimum=1,
        )
        claim_hypothesis_ids = _reference_ids(
            upstream,
            key="hypothesis_ids",
            known=set(hypothesis_index),
            label=f"material claim {claim_id}",
            minimum=1,
        )
        claim_judgment_ids = _reference_ids(
            upstream,
            key="judgment_ids",
            known=world_index.judgment_ids,
            label=f"material claim {claim_id}",
            minimum=0,
        )
        claim_scenario_ids = _reference_ids(
            upstream,
            key="scenario_ids",
            known=world_index.scenario_ids,
            label=f"material claim {claim_id}",
            minimum=0,
        )
        nodes = _reference_ids(
            upstream,
            key="node_ids",
            known=world_index.graph.node_ids,
            label=f"material claim {claim_id}",
            minimum=1,
        )
        edges = _reference_ids(
            upstream,
            key="edge_ids",
            known=world_index.graph.edge_ids,
            label=f"material claim {claim_id}",
            minimum=1,
        )
        _validate_subgraph_refs(
            node_refs=nodes,
            edge_refs=edges,
            graph=world_index.graph,
            label=f"material claim {claim_id}",
        )
        claim_node_ids.update(nodes)
        claim_edge_ids.update(edges)

        evidence_rows = _mapping_list(
            upstream.get("evidence"), label=f"material claim {claim_id}.upstream.evidence"
        )
        if not evidence_rows:
            raise WorldModelValidationError(f"material claim {claim_id} requires evidence")
        evidence_index = _unique_ids(
            evidence_rows, key="evidence_id", label=f"material claim {claim_id} evidence"
        )
        source_units: set[str] = set()
        for evidence_id, evidence in evidence_index.items():
            _require_exact_keys(
                evidence,
                required={"evidence_id", "measure", "units"},
                label=f"material claim {claim_id} evidence {evidence_id}",
            )
            if evidence_id not in evidence_ids:
                raise WorldModelValidationError(
                    f"material claim {claim_id} references unknown evidence: {evidence_id}"
                )
            measure = _required_text(
                evidence,
                "measure",
                label=f"material claim {claim_id} evidence {evidence_id}",
            )
            units = _strings(
                evidence.get("units"),
                label=f"material claim {claim_id} evidence {evidence_id}.units",
            )
            if len(units) != len(set(units)):
                raise WorldModelValidationError(
                    f"material claim {claim_id} evidence {evidence_id}.units must be unique"
                )
            snapshot_measures = snapshot.measure_units.get(evidence_id)
            if snapshot_measures is not None:
                allowed = snapshot_measures.get(measure)
                if allowed is None:
                    raise WorldModelValidationError(
                        f"material claim {claim_id} uses unavailable snapshot measure "
                        f"{measure} for {evidence_id}"
                    )
                if frozenset(units) != allowed:
                    raise WorldModelValidationError(
                        f"material claim {claim_id} units for {evidence_id}.{measure} "
                        f"must equal {sorted(allowed)}"
                    )
            mapped_evidence.add(evidence_id)
            source_units.update(units)
        claim_evidence_ids = set(evidence_index)
        for references, evidence_by_reference, reference_kind in (
            (claim_state_ids, state_evidence_ids, "state"),
            (claim_hypothesis_ids, hypothesis_evidence_ids, "hypothesis"),
            (claim_judgment_ids, world_index.judgment_evidence_ids, "judgment"),
            (claim_scenario_ids, world_index.scenario_evidence_ids, "scenario"),
            (edges, world_index.graph.edge_evidence_ids, "edge"),
        ):
            _require_lineage_evidence_overlap(
                claim_id=claim_id,
                claim_evidence_ids=claim_evidence_ids,
                references=references,
                evidence_by_reference=evidence_by_reference,
                reference_kind=reference_kind,
            )

        source_qualifier = _parse_claim_qualifier(
            claim.get("source_qualifier"), label=f"material claim {claim_id}.source_qualifier"
        )
        if source_qualifier.units != frozenset(source_units):
            raise WorldModelValidationError(
                f"material claim {claim_id}.source_qualifier.units differ from evidence units"
            )
        if claim_state_ids and source_qualifier.uncertainty != frozenset(UNCERTAINTY_DIMENSIONS):
            raise WorldModelValidationError(
                f"material claim {claim_id} must carry all state uncertainty dimensions"
            )
        if edges:
            weakest_edge = min(
                (world_index.graph.edge_strengths[edge_id] for edge_id in edges),
                key=CLAIM_STRENGTH_ORDER.__getitem__,
            )
            if (
                CLAIM_STRENGTH_ORDER[source_qualifier.claim_strength]
                > CLAIM_STRENGTH_ORDER[weakest_edge]
            ):
                raise WorldModelValidationError(
                    f"material claim {claim_id} is stronger than its weakest edge"
                )

        targets = _mapping_list(claim.get("targets"), label=f"material claim {claim_id}.targets")
        if not targets:
            raise WorldModelValidationError(f"material claim {claim_id} requires targets")
        claim_targets: set[tuple[str, str]] = set()
        for target_index, target in enumerate(targets):
            target_label = f"material claim {claim_id} target {target_index}"
            _require_exact_keys(
                target,
                required={"artifact", "field_path", "output_qualifier"},
                optional={"unit_transform"},
                label=target_label,
            )
            artifact = target.get("artifact")
            if artifact not in PROJECTION_ARTIFACTS:
                raise WorldModelValidationError(f"{target_label}.artifact is invalid")
            field_path = _required_text(target, "field_path", label=target_label)
            target_key = (artifact, field_path)
            if target_key in claim_targets:
                raise WorldModelValidationError(f"{target_label} is duplicated")
            claim_targets.add(target_key)
            output_qualifier = _parse_claim_qualifier(
                target.get("output_qualifier"), label=f"{target_label}.output_qualifier"
            )
            _validate_qualifier_projection(
                source_qualifier,
                output_qualifier,
                unit_transform=target.get("unit_transform"),
                label=target_label,
            )
            if artifact == "v4":
                if field_path not in required_v4_paths:
                    raise WorldModelValidationError(
                        f"{target_label}.field_path is not judgment-bearing"
                    )
                resolved = _resolve_json_pointer(
                    v4_document, field_path, label=f"{target_label}.field_path"
                )
                if not (
                    isinstance(resolved, date)
                    or (isinstance(resolved, str) and resolved.strip())
                    or (
                        isinstance(resolved, int | float)
                        and not isinstance(resolved, bool)
                        and math.isfinite(float(resolved))
                    )
                ):
                    raise WorldModelValidationError(
                        f"{target_label}.field_path must resolve to a scalar claim value"
                    )
                for prefix, case in v4_scenario_cases.items():
                    if field_path == prefix or field_path.startswith(f"{prefix}/"):
                        mapped_scenario_id = case_to_scenario.get(case)
                        if (
                            mapped_scenario_id is None
                            or mapped_scenario_id not in claim_scenario_ids
                        ):
                            raise WorldModelValidationError(
                                f"{target_label} must reference the world-model scenario for {case}"
                            )
            else:
                if field_path not in required_one_page_paths:
                    raise WorldModelValidationError(
                        f"{target_label}.field_path is not rendered by one-page"
                    )
                bound = one_page_sources.get(field_path)
                if bound is not None:
                    kind, identifier = bound
                    references = claim_judgment_ids if kind == "judgment" else claim_scenario_ids
                    if identifier not in references:
                        raise WorldModelValidationError(
                            f"{target_label} must reference {kind} {identifier}"
                        )
            mapped_targets.add(target_key)

    missing_v4 = required_v4_paths - {
        field_path for artifact, field_path in mapped_targets if artifact == "v4"
    }
    if missing_v4:
        raise WorldModelValidationError(
            f"v4 judgment-bearing fields are unmapped: {sorted(missing_v4)}"
        )
    missing_one_page = required_one_page_paths - {
        field_path for artifact, field_path in mapped_targets if artifact == "one_page"
    }
    if missing_one_page:
        raise WorldModelValidationError(
            f"one-page judgment-bearing fields are unmapped: {sorted(missing_one_page)}"
        )
    unmapped_selected = set(snapshot.selected_ids) - mapped_evidence
    if unmapped_selected:
        raise WorldModelValidationError(
            f"selected evidence is absent from material claims: {sorted(unmapped_selected)}"
        )
    live_nodes = set(world_index.consumer_node_ids) | claim_node_ids
    live_edges = set(world_index.consumer_edge_ids) | claim_edge_ids
    orphan_nodes = set(world_index.graph.node_ids) - live_nodes
    orphan_edges = set(world_index.graph.edge_ids) - live_edges
    if orphan_nodes or orphan_edges:
        raise WorldModelValidationError(
            "world-model graph contains true orphans: "
            f"nodes={sorted(orphan_nodes)}, edges={sorted(orphan_edges)}"
        )
    return ProjectionMetrics(
        schema_version=2,
        lineage_enforced=True,
        material_claim_count=len(claim_index),
        mapped_target_count=len(mapped_targets),
        mapped_selected_evidence_count=len(set(snapshot.selected_ids) & mapped_evidence),
        true_orphan_node_count=len(orphan_nodes),
        true_orphan_edge_count=len(orphan_edges),
    )


def validate_workspace(
    workspace: Path,
    *,
    freeze_path: Path | None = None,
    v4_projection: Path | None = None,
    v4_document: Path | None = None,
    revision_diff: Path | None = None,
) -> dict[str, object]:
    charter = _load_yaml(workspace / "charter.yaml")
    snapshot = _load_json(workspace / "evidence-snapshot.json")
    evidence_packs = _load_yaml(workspace / "evidence-packs.yaml")
    states = _load_yaml(workspace / "states.yaml")
    hypotheses = _load_yaml(workspace / "hypotheses.yaml")
    matrix = _load_yaml(workspace / "evidence-matrix.yaml")
    world_model = _load_yaml(workspace / "world-model.yaml")
    blind_documents = {
        "charter": charter,
        "evidence_snapshot": snapshot,
        "evidence_packs": evidence_packs,
        "states": states,
        "hypotheses": hypotheses,
        "matrix": matrix,
        "world_model": world_model,
    }
    _reject_prior_material(blind_documents)
    _validate_charter(charter)
    snapshot_index = _snapshot_evidence_ids(snapshot)
    evidence_ids = _validate_evidence_packs(
        evidence_packs,
        snapshot_ids=set(snapshot_index.evidence_ids),
        selected_snapshot_ids=set(snapshot_index.selected_ids),
    )
    state_index = _validate_states(states, evidence_ids=evidence_ids)
    hypothesis_index = _validate_hypotheses(hypotheses, evidence_ids=evidence_ids)
    _validate_matrix(
        matrix,
        evidence_ids=evidence_ids,
        hypothesis_ids=set(hypothesis_index),
    )
    world_index = _validate_world_model(
        world_model,
        evidence_ids=evidence_ids,
        state_ids=set(state_index),
        hypothesis_ids=set(hypothesis_index),
    )
    if snapshot_index.schema_version == 2 or world_index.schema_version == 2:
        if snapshot_index.schema_version != world_index.schema_version:
            raise WorldModelValidationError(
                "evidence-snapshot and world-model schema versions must match"
            )
        cycle_as_of = _iso_date(charter.get("as_of"), label="charter.as_of")
        world_as_of = _iso_date(world_model.get("as_of"), label="world-model.as_of")
        if snapshot_index.as_of != cycle_as_of or world_as_of != cycle_as_of:
            raise WorldModelValidationError(
                "evidence-snapshot and world-model as_of must match charter.as_of"
            )
    else:
        workspace_sha256 = _aggregate_digest(blind_file_digests(workspace))
        if workspace_sha256 not in LEGACY_V1_WORKSPACE_SHA256:
            raise WorldModelValidationError(
                "schema v1 is restricted to the immutable cycle 1 and cycle 2 workspaces"
            )
    freeze_sha256 = verify_freeze(workspace, freeze_path) if freeze_path is not None else None
    projection_metrics = ProjectionMetrics()
    if v4_projection is not None:
        projection_metrics = _validate_v4_projection(
            v4_projection,
            world_model=world_model,
            world_index=world_index,
            state_index=state_index,
            hypothesis_index=hypothesis_index,
            evidence_ids=evidence_ids,
            snapshot=snapshot_index,
            v4_document_path=v4_document,
            revision_diff_path=revision_diff,
        )
    return {
        "status": "ok",
        "workspace_schema_version": world_index.schema_version,
        "evidence_count": len(evidence_ids),
        "state_count": len(state_index),
        "hypothesis_count": len(hypothesis_index),
        "scenario_count": len(world_index.scenario_ranks),
        "projection_schema_version": projection_metrics.schema_version,
        "lineage_enforced": projection_metrics.lineage_enforced,
        "material_claim_count": projection_metrics.material_claim_count,
        "mapped_target_count": projection_metrics.mapped_target_count,
        "mapped_selected_evidence_count": projection_metrics.mapped_selected_evidence_count,
        "true_orphan_node_count": projection_metrics.true_orphan_node_count,
        "true_orphan_edge_count": projection_metrics.true_orphan_edge_count,
        "blind_freeze_sha256": freeze_sha256,
    }


def _write_freeze(path: Path, record: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    except FileExistsError as error:
        raise WorldModelValidationError(
            f"refusing to overwrite existing blind freeze: {path}"
        ) from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    freeze = subparsers.add_parser("freeze", help="create an immutable blind workspace hash")
    freeze.add_argument("workspace", type=Path)
    freeze.add_argument("--output", required=True, type=Path)
    check = subparsers.add_parser("check", help="validate workspace structure and hashes")
    check.add_argument("workspace", type=Path)
    check.add_argument("--freeze", type=Path)
    check.add_argument("--v4-projection", type=Path)
    check.add_argument("--v4-document", type=Path)
    check.add_argument("--revision-diff", type=Path)
    return parser


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    args = _parser().parse_args(argv)
    output = stdout or sys.stdout
    try:
        if args.command == "freeze":
            validate_workspace(args.workspace)
            record = build_freeze_record(args.workspace)
            _write_freeze(args.output, record)
            result: Mapping[str, object] = {
                "status": "frozen",
                "output": str(args.output),
                "blind_freeze_sha256": record["workspace_sha256"],
            }
        else:
            result = validate_workspace(
                args.workspace,
                freeze_path=args.freeze,
                v4_projection=args.v4_projection,
                v4_document=args.v4_document,
                revision_diff=args.revision_diff,
            )
            if result["workspace_schema_version"] == 2:
                missing_options = [
                    option
                    for option, value in (
                        ("--freeze", args.freeze),
                        ("--v4-projection", args.v4_projection),
                        ("--v4-document", args.v4_document),
                        ("--revision-diff", args.revision_diff),
                    )
                    if value is None
                ]
                if missing_options:
                    raise WorldModelValidationError(
                        "schema v2 check requires final inputs: " + ", ".join(missing_options)
                    )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=output)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), file=output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
