"""Validate or freeze a Stage A Macro World Model workspace."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from itertools import pairwise
from pathlib import Path
from typing import TextIO, cast

from tools.experiments.macro_world_model.build_evidence_snapshot import (
    canonical_payload_sha256,
)

from baibai_engine.foundation.yaml_io import safe_load

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
SIGNS = {"positive", "negative", "nonlinear", "ambiguous"}
PRIOR_REFERENCE = re.compile(r"macro-context-\d{4}-\d{2}-\d{2}")


class WorldModelValidationError(ValueError):
    """Raised when a workspace violates the Stage A contract."""


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
    serialized = json.dumps(files, ensure_ascii=False, sort_keys=True)
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


def _snapshot_evidence_ids(snapshot: Mapping[str, object]) -> tuple[set[str], set[str]]:
    stored_hash = snapshot.get("canonical_payload_sha256")
    if not isinstance(stored_hash, str) or stored_hash != canonical_payload_sha256(snapshot):
        raise WorldModelValidationError("evidence snapshot canonical hash is invalid")
    scan = _mapping_list(snapshot.get("coverage_scan"), label="evidence-snapshot.coverage_scan")
    identifiers = _unique_ids(scan, key="evidence_id", label="evidence")
    if not identifiers:
        raise WorldModelValidationError("evidence snapshot is empty")
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
    return set(identifiers), selected


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


def _validate_graph(
    graph: Mapping[str, object], *, evidence_ids: set[str]
) -> tuple[set[str], set[str]]:
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
    for edge_id, edge in edge_index.items():
        source = _required_text(edge, "from_node", label=f"edge {edge_id}")
        target = _required_text(edge, "to_node", label=f"edge {edge_id}")
        if source not in node_index or target not in node_index:
            raise WorldModelValidationError(f"edge {edge_id} references an unknown node")
        if edge.get("relation_kind") not in RELATION_KINDS:
            raise WorldModelValidationError(f"edge {edge_id} has invalid relation_kind")
        if edge.get("claim_strength") not in CLAIM_STRENGTHS:
            raise WorldModelValidationError(f"edge {edge_id} has invalid claim_strength")
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
        _require_evidence_refs(
            edge,
            key="evidence_ids",
            label=f"edge {edge_id}",
            evidence_ids=evidence_ids,
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
    return set(node_index), set(edge_index)


def _validate_world_model(
    document: Mapping[str, object],
    *,
    evidence_ids: set[str],
    state_ids: set[str],
    hypothesis_ids: set[str],
) -> dict[str, int]:
    graph = _mapping(document.get("graph"), label="world-model.graph")
    node_ids, edge_ids = _validate_graph(graph, evidence_ids=evidence_ids)
    judgments = _mapping_list(document.get("key_judgments"), label="world-model.key_judgments")
    if not 3 <= len(judgments) <= 5:
        raise WorldModelValidationError("world model requires 3 to 5 key judgments")
    _unique_ids(judgments, key="judgment_id", label="key judgment")
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
        if not node_refs <= node_ids:
            raise WorldModelValidationError(f"judgment {judgment_id} references unknown nodes")
        edge_refs = set(
            _strings(judgment.get("edge_ids"), label=f"judgment {judgment_id}.edge_ids")
        )
        if not edge_refs <= edge_ids:
            raise WorldModelValidationError(f"judgment {judgment_id} references unknown edges")
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
        _require_evidence_refs(
            judgment,
            key="evidence_ids",
            label=f"judgment {judgment_id}",
            evidence_ids=evidence_ids,
        )

    baseline = _mapping(document.get("baseline_path"), label="world-model.baseline_path")
    if set(baseline) != set(HORIZONS):
        raise WorldModelValidationError(f"baseline_path must equal the horizons {list(HORIZONS)}")
    for horizon in HORIZONS:
        _strings(baseline[horizon], label=f"baseline_path.{horizon}")

    scenarios = _mapping_list(document.get("scenarios"), label="world-model.scenarios")
    if not 2 <= len(scenarios) <= 4:
        raise WorldModelValidationError("world model requires 2 to 4 scenarios")
    _unique_ids(scenarios, key="scenario_id", label="scenario")
    ranks: set[int] = set()
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
    if ranks != set(range(1, len(scenarios) + 1)):
        raise WorldModelValidationError("scenario plausibility ranks must be contiguous and unique")

    _strings(document.get("unresolved_tensions"), label="world-model.unresolved_tensions")
    _strings(document.get("signposts"), label="world-model.signposts")
    return {str(row["scenario_id"]): cast(int, row["plausibility_rank"]) for row in scenarios}


def _validate_v4_projection(path: Path, *, scenario_ranks: Mapping[str, int]) -> None:
    projection = _load_yaml(path)
    rows = _mapping_list(projection.get("scenarios"), label="v4-projection.scenarios")
    _unique_ids(rows, key="scenario_id", label="v4 projection scenario")
    probabilities: dict[str, float] = {}
    for row in rows:
        scenario_id = _required_text(row, "scenario_id", label="v4 projection")
        value = row.get("probability")
        if not isinstance(value, int | float) or isinstance(value, bool):
            raise WorldModelValidationError(f"v4 probability is invalid for {scenario_id}")
        probabilities[scenario_id] = float(value)
    if set(probabilities) != set(scenario_ranks):
        raise WorldModelValidationError("v4 projection must cover every world-model scenario")
    ordered = sorted(scenario_ranks, key=scenario_ranks.__getitem__)
    for more_plausible, less_plausible in pairwise(ordered):
        if probabilities[more_plausible] < probabilities[less_plausible]:
            raise WorldModelValidationError(
                "world-model plausibility order contradicts v4 probability order: "
                f"{more_plausible} < {less_plausible}"
            )


def validate_workspace(
    workspace: Path,
    *,
    freeze_path: Path | None = None,
    v4_projection: Path | None = None,
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
    snapshot_ids, selected_snapshot_ids = _snapshot_evidence_ids(snapshot)
    evidence_ids = _validate_evidence_packs(
        evidence_packs,
        snapshot_ids=snapshot_ids,
        selected_snapshot_ids=selected_snapshot_ids,
    )
    state_index = _validate_states(states, evidence_ids=evidence_ids)
    hypothesis_index = _validate_hypotheses(hypotheses, evidence_ids=evidence_ids)
    _validate_matrix(
        matrix,
        evidence_ids=evidence_ids,
        hypothesis_ids=set(hypothesis_index),
    )
    scenario_ranks = _validate_world_model(
        world_model,
        evidence_ids=evidence_ids,
        state_ids=set(state_index),
        hypothesis_ids=set(hypothesis_index),
    )
    freeze_sha256 = verify_freeze(workspace, freeze_path) if freeze_path is not None else None
    if v4_projection is not None:
        _validate_v4_projection(v4_projection, scenario_ranks=scenario_ranks)
    return {
        "status": "ok",
        "evidence_count": len(evidence_ids),
        "state_count": len(state_index),
        "hypothesis_count": len(hypothesis_index),
        "scenario_count": len(scenario_ranks),
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
            )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=output)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), file=output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
