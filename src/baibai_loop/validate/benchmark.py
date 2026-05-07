"""Benchmark manifest validation."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml

from .domain import load_markdown_front_matter, resolve_ref
from .errors import ValidationFinding

_VALID_LAYERS = {"L1", "L2", "L3a", "L3b", "L4", "L5"}
_REQUIRED_FIXTURE_IDS = {
    "screening-raw-output-anchors",
    "gate-policy-orthogonal-fields",
    "unreviewed-hit-anchor",
    "no-hit-false-negative-anchor",
    "e2e-research-selection-baseline",
    "e2e-liquidity-sensitive-selection",
    "e2e-sales-first-selection",
    "e2e-current-research-strategy",
}
_DOMAIN_FIXTURE_CONTRACTS: dict[str, Mapping[str, object]] = {
    "screening-raw-output-anchors": {
        "layer_id": "L1",
        "fixture_binding": {"candidates_ref": "records/04-candidates/2026/05/2026-05-01.yaml"},
    },
    "gate-policy-orthogonal-fields": {
        "layer_id": "L2",
        "fixture_binding": {"candidates_ref": "records/04-candidates/2026/05/2026-05-01.yaml"},
    },
    "unreviewed-hit-anchor": {
        "layer_id": "L5",
        "fixture_binding": {
            "scan_ref": "records/07-reviews/screening-false-negative-scan/2026-05.yaml"
        },
        "expected": {
            "scan_item_id": "screening-false-negative-2026-05-unreviewed-hit-anchor",
            "classification": "unreviewed_hit_anchor",
            "joins_to_decision_event": True,
            "candidate_playbook_screen_result": "hit",
        },
    },
    "no-hit-false-negative-anchor": {
        "layer_id": "L5",
        "fixture_binding": {
            "scan_ref": "records/07-reviews/screening-false-negative-scan/2026-05.yaml"
        },
        "expected": {
            "scan_item_id": "screening-false-negative-2026-05-no-hit-anchor",
            "classification": "screening_no_hit_anchor",
            "canonical_start_basis": "candidate_run_close_adjusted_close",
            "joins_to_decision_event": True,
            "no_candidate_ref": True,
        },
    },
    "e2e-research-selection-baseline": {
        "layer_id": "L3a",
        "fixture_binding": {
            "runs_ref": "records/_benchmarks/domain-model-2026-05/e2e-regeneration/runs.yaml"
        },
        "expected": {
            "run_id": "run-01-baseline",
            "screening_status": "partial_quality_warning",
        },
    },
    "e2e-liquidity-sensitive-selection": {
        "layer_id": "L3a",
        "fixture_binding": {
            "runs_ref": "records/_benchmarks/domain-model-2026-05/e2e-regeneration/runs.yaml"
        },
        "expected": {
            "run_id": "run-02-liquidity-3oku",
            "screening_status": "partial_quality_warning",
        },
    },
    "e2e-sales-first-selection": {
        "layer_id": "L3a",
        "fixture_binding": {
            "runs_ref": "records/_benchmarks/domain-model-2026-05/e2e-regeneration/runs.yaml"
        },
        "expected": {
            "run_id": "run-10-sales-first-selection",
            "screening_status": "partial_quality_warning",
        },
    },
    "e2e-current-research-strategy": {
        "layer_id": "L3a",
        "fixture_binding": {
            "runs_ref": "records/_benchmarks/domain-model-2026-05/e2e-regeneration/runs.yaml"
        },
        "expected": {
            "current_strategy": {
                "baseline_core_tickers": ["3632", "6835", "6932", "6310", "9470"],
                "liquidity_complement_tickers": ["5423", "6266", "6143", "5410", "6817"],
                "exploration_tickers": ["6619", "6753"],
                "exploration_max_initial_real_order_notional_yen": 75000,
                "exploration_requires_disconfirming_evidence": True,
                "exploration_requires_payoff_confirmation": True,
                "order_ready_tickers": [],
                "max_exploration_ticker_pct": 3.0,
                "evidence_count_one_requires_research_before_order": True,
            },
            "selected_research_coverage": {
                "run_id": "run-01-baseline",
                "ledger_ref": "records/_ledger/research-decisions/2026-05.jsonl",
            },
        },
    },
}


def discover_benchmark_manifest_files(root: Path) -> list[Path]:
    """Return domain model benchmark manifests."""
    if not root.exists():
        return []
    return sorted(root.glob("*/manifest.yaml"))


def validate_benchmark_manifest_file(path: Path) -> list[ValidationFinding]:
    """Validate benchmark manifest structure and fixture bindings."""
    try:
        raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="benchmark.parse",
                message=f"failed to read benchmark manifest: {exc}",
            )
        ]
    if not isinstance(raw, Mapping):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="benchmark.root",
                message="benchmark manifest must be a mapping",
            )
        ]
    findings: list[ValidationFinding] = []
    findings.extend(_check_required(path, raw))
    findings.extend(_check_layers(path, raw))
    findings.extend(_check_fixtures(path, raw))
    findings.extend(_check_merge_blocker_layer_coverage(path, raw))
    findings.extend(_check_required_fixture_ids(path, raw))
    findings.extend(_check_domain_fixture_contracts(path, raw))
    findings.extend(_check_business_invariants(path, raw))
    findings.extend(_check_fixture_expectations(path, raw))
    return findings


def _check_required(path: Path, manifest: Mapping[str, object]) -> list[ValidationFinding]:
    required = ("benchmark_id", "manifest_version", "input_snapshots", "fixtures")
    return [
        _finding(path, "benchmark.required", f"missing required manifest field: {field}", field)
        for field in required
        if field not in manifest
    ]


def _check_layers(path: Path, manifest: Mapping[str, object]) -> list[ValidationFinding]:
    layers = manifest.get("layers")
    if not isinstance(layers, list) or not layers:
        return [_finding(path, "benchmark.layers", "layers must be a non-empty list", "layers")]
    findings: list[ValidationFinding] = []
    seen: set[str] = set()
    for index, layer in enumerate(layers):
        if not isinstance(layer, Mapping):
            findings.append(
                _finding(path, "benchmark.layer", "layer must be a mapping", f"layers[{index}]")
            )
            continue
        layer_id = layer.get("layer_id")
        if not isinstance(layer_id, str) or layer_id not in _VALID_LAYERS:
            findings.append(
                _finding(
                    path, "benchmark.layer-id", "invalid layer_id", f"layers[{index}].layer_id"
                )
            )
            continue
        if layer_id in seen:
            findings.append(
                _finding(
                    path, "benchmark.layer-duplicate", "duplicate layer_id", f"layers[{index}]"
                )
            )
        seen.add(layer_id)
    return findings


def _check_fixtures(path: Path, manifest: Mapping[str, object]) -> list[ValidationFinding]:
    fixtures = manifest.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        return [
            _finding(path, "benchmark.fixtures", "fixtures must be a non-empty list", "fixtures")
        ]
    findings: list[ValidationFinding] = []
    seen: set[str] = set()
    for index, fixture in enumerate(fixtures):
        if not isinstance(fixture, Mapping):
            findings.append(
                _finding(
                    path, "benchmark.fixture", "fixture must be a mapping", f"fixtures[{index}]"
                )
            )
            continue
        fixture_id = fixture.get("fixture_id")
        if not isinstance(fixture_id, str) or not fixture_id:
            findings.append(
                _finding(
                    path,
                    "benchmark.fixture-id",
                    "fixture_id must be a non-empty string",
                    f"fixtures[{index}].fixture_id",
                )
            )
        elif fixture_id in seen:
            findings.append(
                _finding(
                    path,
                    "benchmark.fixture-duplicate",
                    "fixture_id must be unique",
                    f"fixtures[{index}].fixture_id",
                )
            )
        else:
            seen.add(fixture_id)
        if fixture.get("layer_id") not in _VALID_LAYERS:
            findings.append(
                _finding(
                    path,
                    "benchmark.fixture-layer",
                    "fixture layer_id must reference a known benchmark layer",
                    f"fixtures[{index}].layer_id",
                )
            )
        if not isinstance(fixture.get("fixture_binding"), Mapping):
            findings.append(
                _finding(
                    path,
                    "benchmark.fixture-binding",
                    "fixture_binding must be a mapping",
                    f"fixtures[{index}].fixture_binding",
                )
            )
    return findings


def _check_merge_blocker_layer_coverage(
    path: Path, manifest: Mapping[str, object]
) -> list[ValidationFinding]:
    layers = manifest.get("layers")
    fixtures = manifest.get("fixtures")
    if not isinstance(layers, list) or not isinstance(fixtures, list):
        return []
    fixture_layers = {
        str(fixture.get("layer_id"))
        for fixture in fixtures
        if isinstance(fixture, Mapping) and isinstance(fixture.get("layer_id"), str)
    }
    findings: list[ValidationFinding] = []
    for index, layer in enumerate(layers):
        if not isinstance(layer, Mapping) or layer.get("merge_blocker") is not True:
            continue
        layer_id = layer.get("layer_id")
        if isinstance(layer_id, str) and layer_id not in fixture_layers:
            findings.append(
                _finding(
                    path,
                    "benchmark.merge-blocker-fixture",
                    "merge-blocker layers must have at least one fixture",
                    f"layers[{index}].layer_id",
                )
            )
    return findings


def _check_required_fixture_ids(
    path: Path, manifest: Mapping[str, object]
) -> list[ValidationFinding]:
    if manifest.get("benchmark_id") != "domain-model-2026-05":
        return []
    fixtures = manifest.get("fixtures")
    if not isinstance(fixtures, list):
        return []
    fixture_ids = {
        str(fixture.get("fixture_id"))
        for fixture in fixtures
        if isinstance(fixture, Mapping) and isinstance(fixture.get("fixture_id"), str)
    }
    findings: list[ValidationFinding] = []
    for fixture_id in sorted(_REQUIRED_FIXTURE_IDS - fixture_ids):
        findings.append(
            _finding(
                path,
                "benchmark.required-fixture",
                f"business regression manifest must include fixture {fixture_id}",
                "fixtures",
            )
        )
    return findings


def _check_domain_fixture_contracts(
    path: Path, manifest: Mapping[str, object]
) -> list[ValidationFinding]:
    if manifest.get("benchmark_id") != "domain-model-2026-05":
        return []
    fixtures = manifest.get("fixtures")
    if not isinstance(fixtures, list):
        return []
    by_id = {
        str(fixture.get("fixture_id")): fixture
        for fixture in fixtures
        if isinstance(fixture, Mapping) and isinstance(fixture.get("fixture_id"), str)
    }
    findings: list[ValidationFinding] = []
    for fixture_id, contract in _DOMAIN_FIXTURE_CONTRACTS.items():
        fixture = by_id.get(fixture_id)
        if not isinstance(fixture, Mapping):
            continue
        for field in ("layer_id", "fixture_binding", "expected"):
            expected = contract.get(field)
            if expected is None:
                continue
            actual = fixture.get(field)
            if isinstance(expected, Mapping) and isinstance(actual, Mapping):
                for key, expected_value in expected.items():
                    if actual.get(key) != expected_value:
                        findings.append(
                            _finding(
                                path,
                                "benchmark.fixture-contract",
                                f"{fixture_id}.{field}.{key} must be {expected_value!r}",
                                f"fixtures[{fixture_id}]",
                            )
                        )
            elif actual != expected:
                findings.append(
                    _finding(
                        path,
                        "benchmark.fixture-contract",
                        f"{fixture_id}.{field} must be {expected!r}",
                        f"fixtures[{fixture_id}]",
                    )
                )
    return findings


def _check_business_invariants(
    path: Path, manifest: Mapping[str, object]
) -> list[ValidationFinding]:
    invariants = manifest.get("business_invariants")
    if not isinstance(invariants, list) or not invariants:
        return [
            _finding(
                path,
                "benchmark.business-invariants",
                "business_invariants must be a non-empty list",
                "business_invariants",
            )
        ]
    return []


def _check_fixture_expectations(
    path: Path, manifest: Mapping[str, object]
) -> list[ValidationFinding]:
    fixtures = manifest.get("fixtures")
    if not isinstance(fixtures, list):
        return []
    root = _repo_root(path)
    findings: list[ValidationFinding] = []
    for index, fixture in enumerate(fixtures):
        if not isinstance(fixture, Mapping):
            continue
        binding = fixture.get("fixture_binding")
        expected = fixture.get("expected")
        if not isinstance(binding, Mapping) or not isinstance(expected, Mapping):
            continue
        record_ref = binding.get("record_ref")
        scan_ref = binding.get("scan_ref")
        candidates_ref = binding.get("candidates_ref")
        runs_ref = binding.get("runs_ref")
        if isinstance(record_ref, str):
            record_path = resolve_ref(root, record_ref)
            if not record_path.is_file():
                findings.append(
                    _finding(
                        path,
                        "benchmark.fixture-record-missing",
                        f"fixture record does not exist: {record_ref}",
                        f"fixtures[{index}].fixture_binding.record_ref",
                    )
                )
                continue
            try:
                record = load_markdown_front_matter(record_path)
            except (OSError, ValueError, yaml.YAMLError) as exc:
                findings.append(
                    _finding(
                        path,
                        "benchmark.fixture-record-parse",
                        f"failed to parse fixture record: {exc}",
                        f"fixtures[{index}].fixture_binding.record_ref",
                    )
                )
                continue
            findings.extend(_check_record_expected(path, index, record, expected))
        if isinstance(scan_ref, str):
            scan_path = resolve_ref(root, scan_ref)
            if not scan_path.is_file():
                findings.append(
                    _finding(
                        path,
                        "benchmark.fixture-scan-missing",
                        f"fixture scan does not exist: {scan_ref}",
                        f"fixtures[{index}].fixture_binding.scan_ref",
                    )
                )
                continue
            scan = yaml.safe_load(scan_path.read_text(encoding="utf-8"))
            if isinstance(scan, Mapping):
                findings.extend(_check_scan_expected(path, index, scan, expected))
        if isinstance(candidates_ref, str):
            candidates_path = resolve_ref(root, candidates_ref)
            if not candidates_path.is_file():
                findings.append(
                    _finding(
                        path,
                        "benchmark.fixture-candidates-missing",
                        f"fixture candidates file does not exist: {candidates_ref}",
                        f"fixtures[{index}].fixture_binding.candidates_ref",
                    )
                )
                continue
            candidates = yaml.safe_load(candidates_path.read_text(encoding="utf-8"))
            if isinstance(candidates, Mapping):
                findings.extend(_check_candidates_expected(path, index, candidates, expected))
        if isinstance(runs_ref, str):
            runs_path = resolve_ref(root, runs_ref)
            if not runs_path.is_file():
                findings.append(
                    _finding(
                        path,
                        "benchmark.fixture-runs-missing",
                        f"fixture runs file does not exist: {runs_ref}",
                        f"fixtures[{index}].fixture_binding.runs_ref",
                    )
                )
                continue
            runs = yaml.safe_load(runs_path.read_text(encoding="utf-8"))
            if isinstance(runs, Mapping):
                findings.extend(_check_runs_expected(path, index, runs, expected))
    return findings


def _check_record_expected(
    path: Path,
    index: int,
    record: Mapping[str, object],
    expected: Mapping[str, object],
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    if expected.get("ticker") is not None and record.get("ticker") != expected.get("ticker"):
        findings.append(
            _finding(
                path,
                "benchmark.expected-ticker",
                "fixture ticker mismatch",
                f"fixtures[{index}].expected.ticker",
            )
        )
    if "outcome" in expected:
        decision = record.get("research_decision")
        actual = decision.get("outcome") if isinstance(decision, Mapping) else None
        if actual != expected["outcome"]:
            findings.append(
                _finding(
                    path,
                    "benchmark.expected-outcome",
                    "fixture outcome mismatch",
                    f"fixtures[{index}].expected.outcome",
                )
            )
    if "rejection_reason" in expected:
        decision = record.get("research_decision")
        actual = decision.get("rejection_reason") if isinstance(decision, Mapping) else None
        if actual != expected["rejection_reason"]:
            findings.append(
                _finding(
                    path,
                    "benchmark.expected-rejection-reason",
                    "fixture rejection_reason mismatch",
                    f"fixtures[{index}].expected.rejection_reason",
                )
            )
    for field in ("conviction_tier", "independent_evidence_count"):
        if field in expected and record.get(field) != expected[field]:
            findings.append(
                _finding(
                    path,
                    f"benchmark.expected-{field.replace('_', '-')}",
                    f"fixture {field} mismatch",
                    f"fixtures[{index}].expected.{field}",
                )
            )
    if "real_order_intent_yen" in expected:
        actual = _nested_value(record, ("position_sizing_overlay", "real_order_intent_yen"))
        if actual != expected["real_order_intent_yen"]:
            findings.append(
                _finding(
                    path,
                    "benchmark.expected-real-order-intent-yen",
                    "fixture real_order_intent_yen mismatch",
                    f"fixtures[{index}].expected.real_order_intent_yen",
                )
            )
    if "target_quantity" in expected:
        intent = record.get("order_intent")
        actual = intent.get("quantity") if isinstance(intent, Mapping) else None
        if actual != expected["target_quantity"]:
            findings.append(
                _finding(
                    path,
                    "benchmark.expected-quantity",
                    "fixture target quantity mismatch",
                    f"fixtures[{index}].expected.target_quantity",
                )
            )
    if "order_price_guard_yen" in expected:
        intent = record.get("order_intent")
        actual = intent.get("order_price_guard_yen") if isinstance(intent, Mapping) else None
        if actual != expected["order_price_guard_yen"]:
            findings.append(
                _finding(
                    path,
                    "benchmark.expected-guard",
                    "fixture order_price_guard_yen mismatch",
                    f"fixtures[{index}].expected.order_price_guard_yen",
                )
            )
    for field in ("guarded_max_notional_yen", "estimated_real_order_notional_yen"):
        if field in expected:
            actual = _nested_value(record, ("position_sizing_overlay", field))
            if actual != expected[field]:
                findings.append(
                    _finding(
                        path,
                        f"benchmark.expected-{field.replace('_', '-')}",
                        f"fixture {field} mismatch",
                        f"fixtures[{index}].expected.{field}",
                    )
                )
    return findings


def _nested_value(record: Mapping[str, object], path: Sequence[str]) -> object:
    current: object = record
    for part in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _check_scan_expected(
    path: Path,
    index: int,
    scan: Mapping[str, object],
    expected: Mapping[str, object],
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    scan_item = _scan_item(scan, expected.get("scan_item_id"))
    if expected.get("scan_item_id") is not None and scan_item is None:
        findings.append(
            _finding(
                path,
                "benchmark.expected-scan-item",
                "scan fixture must contain expected scan_item_id",
                f"fixtures[{index}].expected.scan_item_id",
            )
        )
        return findings
    if expected.get("canonical_start_basis") is not None:
        actual = (
            scan_item.get("start_price_basis")
            if isinstance(scan_item, Mapping) and scan_item.get("start_price_basis") is not None
            else scan.get("start_price_basis")
        )
        if actual != expected["canonical_start_basis"]:
            findings.append(
                _finding(
                    path,
                    "benchmark.expected-start-basis",
                    "fixture canonical start basis mismatch",
                    f"fixtures[{index}].expected.canonical_start_basis",
                )
            )
    if expected.get("joins_to_decision_event") is True:
        target_items = [scan_item] if isinstance(scan_item, Mapping) else as_list(scan.get("items"))
        if not any(
            isinstance(item, Mapping) and item.get("decision_event_id") for item in target_items
        ):
            findings.append(
                _finding(
                    path,
                    "benchmark.expected-decision-anchor",
                    "scan fixture item must contain decision_event_id",
                    f"fixtures[{index}].expected.joins_to_decision_event",
                )
            )
    if (
        expected.get("classification") is not None
        and isinstance(scan_item, Mapping)
        and scan_item.get("classification") != expected["classification"]
    ):
        findings.append(
            _finding(
                path,
                "benchmark.expected-classification",
                "scan fixture classification mismatch",
                f"fixtures[{index}].expected.classification",
            )
        )
    if (
        expected.get("no_candidate_ref") is True
        and isinstance(scan_item, Mapping)
        and "candidate_ref" in scan_item
    ):
        findings.append(
            _finding(
                path,
                "benchmark.expected-no-candidate-ref",
                "no-hit scan fixture must not point at a candidate row",
                f"fixtures[{index}].expected.no_candidate_ref",
            )
        )
    expected_playbook = expected.get("candidate_playbook_screen_result")
    if isinstance(expected_playbook, str) and isinstance(scan_item, Mapping):
        actual = _scan_candidate_playbook_result(path, scan_item)
        if actual != expected_playbook:
            findings.append(
                _finding(
                    path,
                    "benchmark.expected-candidate-playbook-result",
                    "scan fixture candidate playbook_screen_result mismatch",
                    f"fixtures[{index}].expected.candidate_playbook_screen_result",
                )
            )
    return findings


def as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _scan_item(scan: Mapping[str, object], scan_item_id: object) -> Mapping[str, object] | None:
    items = scan.get("items")
    if not isinstance(items, list):
        return None
    if not isinstance(scan_item_id, str):
        return None
    for item in items:
        if isinstance(item, Mapping) and item.get("scan_item_id") == scan_item_id:
            return item
    return None


def _scan_candidate_playbook_result(path: Path, item: Mapping[str, object]) -> object:
    candidate_ref = item.get("candidate_ref")
    if not isinstance(candidate_ref, Mapping):
        return None
    candidates_ref = candidate_ref.get("candidates_ref")
    ticker = candidate_ref.get("ticker")
    if not isinstance(candidates_ref, str) or not isinstance(ticker, str):
        return None
    candidates_path = resolve_ref(_repo_root(path), candidates_ref)
    if not candidates_path.is_file():
        return None
    candidates = yaml.safe_load(candidates_path.read_text(encoding="utf-8"))
    rows = candidates.get("candidates") if isinstance(candidates, Mapping) else None
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, Mapping) and row.get("ticker") == ticker:
            return row.get("playbook_screen_result")
    return None


def _check_candidates_expected(
    path: Path,
    index: int,
    candidates: Mapping[str, object],
    expected: Mapping[str, object],
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    if expected.get("run_id") is not None and candidates.get("run_id") != expected["run_id"]:
        findings.append(
            _finding(
                path,
                "benchmark.expected-run-id",
                "fixture run_id mismatch",
                f"fixtures[{index}].expected.run_id",
            )
        )
    rows = candidates.get("candidates")
    candidate_rows = (
        [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []
    )
    if expected.get("orthogonal_gate_fields_present") is True:
        required = {
            "playbook_screen_result",
            "policy_gate_result",
            "liquidity_gate_result",
            "macro_regime_gate_result",
        }
        for row_index, row in enumerate(candidate_rows):
            missing = [field for field in required if field not in row]
            if missing:
                findings.append(
                    _finding(
                        path,
                        "benchmark.expected-orthogonal-gates",
                        f"candidate row missing orthogonal gate fields: {', '.join(missing)}",
                        f"fixtures[{index}].fixture_binding.candidates_ref[{row_index}]",
                    )
                )
                break
    return findings


def _check_runs_expected(
    path: Path,
    index: int,
    runs_document: Mapping[str, object],
    expected: Mapping[str, object],
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    run_id = expected.get("run_id")
    run = _e2e_run(runs_document, run_id)
    if isinstance(run_id, str) and run is None:
        return [
            _finding(
                path,
                "benchmark.expected-e2e-run",
                f"fixture runs must contain expected run_id {run_id}",
                f"fixtures[{index}].expected.run_id",
            )
        ]
    strategy_expected = expected.get("current_strategy")
    if isinstance(strategy_expected, Mapping):
        findings.extend(
            _check_current_strategy_expected(path, index, runs_document, strategy_expected)
        )
    coverage_expected = expected.get("selected_research_coverage")
    if isinstance(coverage_expected, Mapping):
        findings.extend(
            _check_selected_research_coverage(path, index, runs_document, coverage_expected)
        )
    if run is None:
        return findings
    if (
        expected.get("screening_status") is not None
        and run.get("screening_status") != expected["screening_status"]
    ):
        findings.append(
            _finding(
                path,
                "benchmark.expected-screening-status",
                "E2E run screening_status mismatch",
                f"fixtures[{index}].expected.screening_status",
            )
        )
    if (
        "selected_tickers" in expected
        and run.get("selected_tickers") != expected["selected_tickers"]
    ):
        findings.append(
            _finding(
                path,
                "benchmark.expected-selected-tickers",
                "E2E run selected_tickers mismatch",
                f"fixtures[{index}].expected.selected_tickers",
            )
        )
    return findings


def _check_current_strategy_expected(
    path: Path,
    index: int,
    runs_document: Mapping[str, object],
    expected: Mapping[str, object],
) -> list[ValidationFinding]:
    strategy = runs_document.get("current_research_strategy")
    if not isinstance(strategy, Mapping):
        return [
            _finding(
                path,
                "benchmark.expected-current-strategy",
                "runs document must contain current_research_strategy",
                f"fixtures[{index}].expected.current_strategy",
            )
        ]
    checks = {
        "baseline_core_tickers": _nested_value(strategy, ("baseline_core", "tickers")),
        "liquidity_complement_tickers": _nested_value(
            strategy, ("liquidity_complement", "tickers")
        ),
        "exploration_tickers": _nested_value(strategy, ("exploration", "tickers")),
        "exploration_max_initial_real_order_notional_yen": _nested_value(
            strategy, ("exploration", "max_initial_real_order_notional_yen")
        ),
        "exploration_requires_disconfirming_evidence": _nested_value(
            strategy, ("exploration", "requires_disconfirming_evidence")
        ),
        "exploration_requires_payoff_confirmation": _nested_value(
            strategy, ("exploration", "requires_payoff_confirmation")
        ),
        "order_ready_tickers": strategy.get("order_ready_tickers", []),
        "max_exploration_ticker_pct": _nested_value(
            strategy, ("allocation_guardrails", "max_exploration_ticker_pct")
        ),
        "evidence_count_one_requires_research_before_order": _nested_value(
            strategy,
            (
                "allocation_guardrails",
                "evidence_count_one_requires_research_before_order",
            ),
        ),
    }
    findings: list[ValidationFinding] = []
    for field, actual in checks.items():
        if field in expected and actual != expected[field]:
            findings.append(
                _finding(
                    path,
                    f"benchmark.expected-current-strategy-{field.replace('_', '-')}",
                    f"current_research_strategy.{field} mismatch",
                    f"fixtures[{index}].expected.current_strategy.{field}",
                )
            )
    return findings


def _check_selected_research_coverage(
    path: Path,
    index: int,
    runs_document: Mapping[str, object],
    expected: Mapping[str, object],
) -> list[ValidationFinding]:
    run = _e2e_run(runs_document, expected.get("run_id"))
    if not isinstance(run, Mapping):
        return []
    selected = [str(ticker) for ticker in as_list(run.get("selected_tickers"))]
    ledger_ref = expected.get("ledger_ref")
    if not isinstance(ledger_ref, str):
        return []
    ledger_path = resolve_ref(_repo_root(path), ledger_ref)
    records = _load_decision_register_rows(ledger_path)
    covered = {
        str(record.get("ticker"))
        for record in records
        if record.get("decision_scope") in {"research_memo", "candidate_screen"}
        and (record.get("research_ref") or record.get("candidate_decision") == "not_reviewed")
    }
    missing = [ticker for ticker in selected if ticker not in covered]
    if not missing:
        return []
    return [
        _finding(
            path,
            "benchmark.expected-selected-research-coverage",
            f"selected tickers lack research_memo or not_reviewed anchor: {', '.join(missing)}",
            f"fixtures[{index}].expected.selected_research_coverage",
        )
    ]


def _load_decision_register_rows(path: Path) -> list[Mapping[str, object]]:
    rows: list[Mapping[str, object]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping):
            rows.append(payload)
    return rows


def _e2e_run(
    runs_document: Mapping[str, object],
    run_id: object,
) -> Mapping[str, object] | None:
    runs = runs_document.get("runs")
    if not isinstance(runs, list) or not isinstance(run_id, str):
        return None
    for run in runs:
        if isinstance(run, Mapping) and run.get("run_id") == run_id:
            return run
    return None


def _repo_root(path: Path) -> Path:
    for parent in (path.parent, *path.parents):
        if (parent / "records").is_dir():
            return parent
    return path.parents[2]


def _finding(path: Path, code: str, message: str, location: str) -> ValidationFinding:
    return ValidationFinding(
        severity="error",
        target=path,
        code=code,
        message=message,
        location=location,
    )
