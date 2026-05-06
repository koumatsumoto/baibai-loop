from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

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
            manifest.parent.mkdir(parents=True)
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

    def test_rejects_invalid_fixture_layer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "manifest.yaml"
            path.write_text(
                _manifest().replace("  layer_id: L1", "  layer_id: L9", 1),
                encoding="utf-8",
            )

            findings = validate_benchmark_manifest_file(path)

        self.assertIn("benchmark.fixture-layer", {finding.code for finding in findings})


def _manifest() -> str:
    return (
        textwrap.dedent(
            """\
        benchmark_id: test
        manifest_version: 1
        input_snapshots: {}
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


if __name__ == "__main__":
    unittest.main()
