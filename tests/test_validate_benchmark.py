from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.validate.benchmark import (
    discover_benchmark_manifest_files,
    validate_benchmark_manifest_file,
)


class BenchmarkManifestValidationTests(unittest.TestCase):
    def test_discovers_benchmark_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest = root / "domain-model/manifest.yaml"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(_manifest(), encoding="utf-8")

            self.assertEqual(discover_benchmark_manifest_files(root), [manifest])

    def test_accepts_repository_manifest(self) -> None:
        findings = validate_benchmark_manifest_file(
            ROOT / "records/_benchmarks/domain-model-2026-05/manifest.yaml"
        )

        self.assertEqual(findings, [])

    def test_rejects_duplicate_fixture_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "manifest.yaml"
            path.write_text(
                _manifest().replace(
                    "business_invariants:",
                    _fixture("fixture-1") + "business_invariants:",
                    1,
                ),
                encoding="utf-8",
            )

            findings = validate_benchmark_manifest_file(path)

        self.assertIn("benchmark.fixture-duplicate", {finding.code for finding in findings})

    def test_rejects_removed_reference_and_hash_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "manifest.yaml"
            path.write_text(
                _manifest().replace(
                    "input_refs: {}",
                    "input_refs:\n"
                    "  policy:\n"
                    "    ref_path: records/01-policy/2026/05/policy.md\n"
                    "    content_sha256: sha256:bad\n"
                    "input_snapshots:\n"
                    "  policy:\n"
                    "    ref_path: records/01-policy/2026/05/policy.md",
                ),
                encoding="utf-8",
            )

            findings = validate_benchmark_manifest_file(path)

        codes = {finding.code for finding in findings}
        self.assertIn("benchmark.removed-reference-field", codes)
        self.assertIn("benchmark.removed-hash-field", codes)

    def test_rejects_invalid_fixture_layer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "manifest.yaml"
            path.write_text(
                _manifest().replace("  layer_id: L1", "  layer_id: L9", 1),
                encoding="utf-8",
            )

            findings = validate_benchmark_manifest_file(path)

        self.assertIn("benchmark.fixture-layer", {finding.code for finding in findings})

    def test_rejects_merge_blocker_layer_without_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "manifest.yaml"
            path.write_text(
                textwrap.dedent(
                    """\
                    benchmark_id: test
                    manifest_version: 1
                    input_refs: {}
                    layers:
                    - layer_id: L1
                      merge_blocker: true
                    - layer_id: L2
                      merge_blocker: true
                    fixtures:
                    - fixture_id: fixture-1
                      layer_id: L1
                      fixture_binding:
                        test: true
                    business_invariants:
                    - id: invariant-1
                      fixture_binding: pytest
                    """
                ),
                encoding="utf-8",
            )

            findings = validate_benchmark_manifest_file(path)

        self.assertIn("benchmark.merge-blocker-fixture", {finding.code for finding in findings})

    def test_domain_model_manifest_requires_current_e2e_fixture_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "manifest.yaml"
            path.write_text(
                _manifest().replace("benchmark_id: test", "benchmark_id: domain-model-2026-05"),
                encoding="utf-8",
            )

            findings = validate_benchmark_manifest_file(path)

        self.assertIn("benchmark.required-fixture", {finding.code for finding in findings})

    def test_rejects_record_expected_outcome_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            record = root / "records/05-research/2026/05/research.md"
            record.parent.mkdir(parents=True)
            record.write_text(
                "---\nticker: '1111'\nresearch_decision:\n  outcome: approved\n---\n# Research\n",
                encoding="utf-8",
            )
            manifest = root / "records/_benchmarks/domain-model/manifest.yaml"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(
                _manifest_with_expected(
                    record_ref="records/05-research/2026/05/research.md",
                    expected={"ticker": "1111", "outcome": "rejected"},
                ),
                encoding="utf-8",
            )

            findings = validate_benchmark_manifest_file(manifest)

        self.assertIn("benchmark.expected-outcome", {finding.code for finding in findings})

    def test_rejects_fixture_candidates_wrong_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            wrong = root / "records/01-policy/2026/05/policy.yaml"
            wrong.parent.mkdir(parents=True)
            wrong.write_text("policy_id: test\n", encoding="utf-8")
            manifest = root / "records/_benchmarks/domain-model/manifest.yaml"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(
                _manifest_with_expected(
                    candidates_ref="records/01-policy/2026/05/policy.yaml",
                    expected={},
                ),
                encoding="utf-8",
            )

            findings = validate_benchmark_manifest_file(manifest)

        self.assertIn("benchmark.fixture-candidates-ref", {finding.code for finding in findings})

    def test_rejects_non_string_fixture_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest = root / "records/_benchmarks/domain-model/manifest.yaml"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(
                _manifest_with_expected(
                    candidates_ref="{ref_path: records/06-trades/x.md}",
                    expected={},
                ).replace(
                    "    candidates_ref: {ref_path: records/06-trades/x.md}",
                    "    candidates_ref:\n      ref_path: records/06-trades/x.md",
                ),
                encoding="utf-8",
            )

            findings = validate_benchmark_manifest_file(manifest)

        self.assertIn("benchmark.fixture-candidates-ref", {finding.code for finding in findings})

    def test_rejects_non_list_playbook_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "manifest.yaml"
            path.write_text(
                _manifest() + "playbook_refs: records/_playbooks/test.md\n",
                encoding="utf-8",
            )

            findings = validate_benchmark_manifest_file(path)

        self.assertIn("benchmark.playbook-ref", {finding.code for finding in findings})

    def test_rejects_trade_expected_quantity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            record = root / "records/06-trades/2026/05/trade.md"
            record.parent.mkdir(parents=True)
            record.write_text(
                "---\n"
                "ticker: '2222'\n"
                "order_intent:\n"
                "  quantity: 100\n"
                "  order_price_guard_yen: 1050\n"
                "---\n"
                "# Trade\n",
                encoding="utf-8",
            )
            manifest = root / "records/_benchmarks/domain-model/manifest.yaml"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(
                _manifest_with_expected(
                    record_ref="records/06-trades/2026/05/trade.md",
                    expected={"ticker": "2222", "target_quantity": 200},
                ),
                encoding="utf-8",
            )

            findings = validate_benchmark_manifest_file(manifest)

        self.assertIn("benchmark.expected-quantity", {finding.code for finding in findings})

    def test_rejects_scan_missing_decision_event_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scan = root / "records/07-reviews/screening-false-negative-scan/2026-05.yaml"
            scan.parent.mkdir(parents=True)
            scan.write_text(
                "start_price_basis: candidate_run_close_adjusted_close\nitems:\n- ticker: '9999'\n",
                encoding="utf-8",
            )
            manifest = root / "records/_benchmarks/domain-model/manifest.yaml"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(
                _manifest_with_expected(
                    scan_ref="records/07-reviews/screening-false-negative-scan/2026-05.yaml",
                    expected={
                        "canonical_start_basis": "candidate_run_close_adjusted_close",
                        "joins_to_decision_event": True,
                    },
                ),
                encoding="utf-8",
            )

            findings = validate_benchmark_manifest_file(manifest)

        self.assertIn("benchmark.expected-decision-anchor", {finding.code for finding in findings})

    def test_rejects_e2e_selected_ticker_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runs = root / "records/_benchmarks/domain-model/e2e/runs.yaml"
            runs.parent.mkdir(parents=True)
            runs.write_text(
                "runs:\n"
                "- run_id: run-01-baseline\n"
                "  screening_status: partial_quality_warning\n"
                "  selected_tickers:\n"
                "  - '1111'\n",
                encoding="utf-8",
            )
            manifest = root / "records/_benchmarks/domain-model/manifest.yaml"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(
                _manifest_with_expected(
                    runs_ref="records/_benchmarks/domain-model/e2e/runs.yaml",
                    expected={
                        "run_id": "run-01-baseline",
                        "screening_status": "partial_quality_warning",
                        "selected_tickers": ["2222"],
                    },
                ),
                encoding="utf-8",
            )

            findings = validate_benchmark_manifest_file(manifest)

        self.assertIn("benchmark.expected-selected-tickers", {finding.code for finding in findings})

    def test_rejects_current_strategy_bucket_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runs = root / "records/_benchmarks/domain-model/e2e/runs.yaml"
            runs.parent.mkdir(parents=True, exist_ok=True)
            runs.write_text(
                "current_research_strategy:\n"
                "  baseline_core:\n"
                "    tickers: ['1111']\n"
                "  liquidity_complement:\n"
                "    tickers: []\n"
                "  exploration:\n"
                "    tickers: []\n"
                "    max_initial_real_order_notional_yen: 75000\n"
                "    requires_disconfirming_evidence: true\n"
                "    requires_payoff_confirmation: true\n"
                "  order_ready_tickers: []\n"
                "  allocation_guardrails:\n"
                "    max_exploration_ticker_pct: 3.0\n"
                "    evidence_count_one_requires_research_before_order: true\n"
                "runs:\n"
                "- run_id: run-01-baseline\n"
                "  screening_status: partial_quality_warning\n"
                "  selected_tickers: ['1111']\n",
                encoding="utf-8",
            )
            manifest = root / "records/_benchmarks/domain-model/manifest.yaml"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(
                _manifest_with_expected(
                    runs_ref="records/_benchmarks/domain-model/e2e/runs.yaml",
                    expected={"current_strategy": {"baseline_core_tickers": ["2222"]}},
                ),
                encoding="utf-8",
            )

            findings = validate_benchmark_manifest_file(manifest)

        self.assertIn(
            "benchmark.expected-current-strategy-baseline-core-tickers",
            {finding.code for finding in findings},
        )

    def test_rejects_current_strategy_exploration_cap_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runs = root / "records/_benchmarks/domain-model/e2e/runs.yaml"
            runs.parent.mkdir(parents=True, exist_ok=True)
            runs.write_text(
                "current_research_strategy:\n"
                "  baseline_core:\n"
                "    tickers: []\n"
                "  liquidity_complement:\n"
                "    tickers: []\n"
                "  exploration:\n"
                "    tickers: ['1111']\n"
                "    max_initial_real_order_notional_yen: 100000\n"
                "    requires_disconfirming_evidence: true\n"
                "    requires_payoff_confirmation: true\n"
                "  order_ready_tickers: []\n"
                "  allocation_guardrails:\n"
                "    max_exploration_ticker_pct: 3.0\n"
                "    evidence_count_one_requires_research_before_order: true\n"
                "runs:\n"
                "- run_id: run-01-baseline\n"
                "  screening_status: partial_quality_warning\n"
                "  selected_tickers: ['1111']\n",
                encoding="utf-8",
            )
            manifest = root / "records/_benchmarks/domain-model/manifest.yaml"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(
                _manifest_with_expected(
                    runs_ref="records/_benchmarks/domain-model/e2e/runs.yaml",
                    expected={
                        "current_strategy": {
                            "exploration_max_initial_real_order_notional_yen": 75000
                        }
                    },
                ),
                encoding="utf-8",
            )

            findings = validate_benchmark_manifest_file(manifest)

        self.assertIn(
            "benchmark.expected-current-strategy-exploration-max-initial-real-order-notional-yen",
            {finding.code for finding in findings},
        )

    def test_rejects_selected_ticker_without_research_or_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runs = root / "records/_benchmarks/domain-model/e2e/runs.yaml"
            runs.parent.mkdir(parents=True, exist_ok=True)
            runs.write_text(
                "runs:\n"
                "- run_id: run-01-baseline\n"
                "  screening_status: partial_quality_warning\n"
                "  selected_tickers: ['1111', '2222']\n",
                encoding="utf-8",
            )
            ledger = root / "records/_ledger/research-decisions/2026-05.jsonl"
            ledger.parent.mkdir(parents=True, exist_ok=True)
            ledger.write_text(
                '{"ticker":"1111","decision_scope":"candidate_screen",'
                '"candidate_decision":"not_reviewed"}\n',
                encoding="utf-8",
            )
            manifest = root / "records/_benchmarks/domain-model/manifest.yaml"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(
                _manifest_with_expected(
                    runs_ref="records/_benchmarks/domain-model/e2e/runs.yaml",
                    expected={
                        "selected_research_coverage": {
                            "run_id": "run-01-baseline",
                            "ledger_ref": "records/_ledger/research-decisions/2026-05.jsonl",
                        }
                    },
                ),
                encoding="utf-8",
            )

            findings = validate_benchmark_manifest_file(manifest)

        self.assertIn(
            "benchmark.expected-selected-research-coverage",
            {finding.code for finding in findings},
        )


def _manifest() -> str:
    return (
        textwrap.dedent(
            """\
        benchmark_id: test
        manifest_version: 1
        input_refs: {}
        layers:
        - layer_id: L1
          merge_blocker: true
        fixtures:
        """
        )
        + _fixture("fixture-1")
        + textwrap.dedent(
            """\
        business_invariants:
        - id: invariant-1
          fixture_binding: pytest
        """
        )
    )


def _fixture(fixture_id: str) -> str:
    return textwrap.dedent(
        f"""\
        - fixture_id: {fixture_id}
          layer_id: L1
          fixture_binding:
            test: true
        """
    )


def _manifest_with_expected(
    *,
    expected: dict[str, object],
    record_ref: str | None = None,
    scan_ref: str | None = None,
    candidates_ref: str | None = None,
    runs_ref: str | None = None,
) -> str:
    binding_lines: list[str] = []
    if record_ref is not None:
        binding_lines.append(f"    record_ref: {record_ref}\n")
    if scan_ref is not None:
        binding_lines.append(f"    scan_ref: {scan_ref}\n")
    if candidates_ref is not None:
        binding_lines.append(f"    candidates_ref: {candidates_ref}\n")
    if runs_ref is not None:
        binding_lines.append(f"    runs_ref: {runs_ref}\n")
    expected_yaml = textwrap.indent(
        yaml.safe_dump(expected, allow_unicode=True, sort_keys=False),
        "    ",
    )
    return (
        "benchmark_id: test\n"
        "manifest_version: 1\n"
        "input_refs: {}\n"
        "layers:\n"
        "- layer_id: L1\n"
        "  merge_blocker: true\n"
        "fixtures:\n"
        "- fixture_id: fixture-1\n"
        "  layer_id: L1\n"
        "  fixture_binding:\n"
        + "".join(binding_lines)
        + "  expected:\n"
        + expected_yaml
        + "business_invariants:\n"
        "- id: invariant-1\n"
        "  fixture_binding: pytest\n"
    )


if __name__ == "__main__":
    unittest.main()
