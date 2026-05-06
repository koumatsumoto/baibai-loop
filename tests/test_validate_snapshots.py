from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.validate.snapshots import validate_snapshot_integrity


class SnapshotIntegrityValidationTests(unittest.TestCase):
    def test_rejects_snapshot_payload_self_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot = root / "records/_universe-snapshots/2026/05/universe.yaml"
            snapshot.parent.mkdir(parents=True)
            snapshot.write_text(
                f"snapshot_id: universe-20260501\ncontent_sha256: sha256:{'0' * 64}\nmembers: []\n",
                encoding="utf-8",
            )

            findings = validate_snapshot_integrity(root)

        self.assertIn("snapshot.self-hash", {finding.code for finding in findings})

    def test_rejects_snapshot_reference_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            policy = root / "records/01-policy/2026/05/policy.yaml"
            policy.parent.mkdir(parents=True)
            policy.write_text("policy_id: portfolio-policy\n", encoding="utf-8")
            research = root / "records/05-research/2026/05/research.md"
            research.parent.mkdir(parents=True)
            research.write_text(
                "---\n"
                "portfolio_policy_snapshot:\n"
                "  ref_path: records/01-policy/2026/05/policy.yaml\n"
                f"  content_sha256: sha256:{'1' * 64}\n"
                "---\n",
                encoding="utf-8",
            )

            findings = validate_snapshot_integrity(root)

        self.assertIn("snapshot.hash-mismatch", {finding.code for finding in findings})

    def test_rejects_migration_manifest_sidecar_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest = root / "records/_migrations/domain-model.jsonl"
            manifest.parent.mkdir(parents=True)
            manifest.write_text('{"migration_run_id":"run-1"}\n', encoding="utf-8")
            manifest.with_suffix(manifest.suffix + ".sha256").write_text(
                f"{'2' * 64}  domain-model.jsonl\n",
                encoding="utf-8",
            )

            findings = validate_snapshot_integrity(root)

        self.assertIn("migration.digest-mismatch", {finding.code for finding in findings})

    def test_rejects_target_payload_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            payload = root / "records/05-research/item.md"
            payload.parent.mkdir(parents=True)
            payload.write_text("---\nticker: '9682'\n---\n", encoding="utf-8")
            manifest = root / "records/_migrations/domain-model.jsonl"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                f'{{"target_payload_tree_sha":"sha256:{"3" * 64}"}}\n',
                encoding="utf-8",
            )
            manifest_digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
            manifest.with_suffix(manifest.suffix + ".sha256").write_text(
                f"{manifest_digest}  domain-model.jsonl\n",
                encoding="utf-8",
            )

            findings = validate_snapshot_integrity(root)

        self.assertIn("migration.payload-mismatch", {finding.code for finding in findings})

    def test_accepts_valid_snapshot_reference_and_manifest_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            policy = root / "records/01-policy/2026/05/policy.yaml"
            policy.parent.mkdir(parents=True)
            policy.write_text("policy_id: portfolio-policy\n", encoding="utf-8")
            digest = hashlib.sha256(policy.read_bytes()).hexdigest()
            research = root / "records/05-research/2026/05/research.md"
            research.parent.mkdir(parents=True)
            research.write_text(
                "---\n"
                "portfolio_policy_snapshot:\n"
                "  ref_path: records/01-policy/2026/05/policy.yaml\n"
                f"  content_sha256: sha256:{digest}\n"
                "---\n",
                encoding="utf-8",
            )
            manifest = root / "records/_migrations/domain-model.jsonl"
            manifest.parent.mkdir(parents=True)
            payload_hash = _payload_hash(root)
            manifest.write_text(
                f'{{"migration_run_id":"run-1","target_payload_tree_sha":"{payload_hash}"}}\n',
                encoding="utf-8",
            )
            manifest_digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
            manifest.with_suffix(manifest.suffix + ".sha256").write_text(
                f"{manifest_digest}  domain-model.jsonl\n",
                encoding="utf-8",
            )

            findings = validate_snapshot_integrity(root)

        self.assertEqual(findings, [])


def _payload_hash(root: Path) -> str:
    rows: list[str] = []
    for path in sorted((root / "records").rglob("*")):
        if not path.is_file() or "_migrations" in path.relative_to(root / "records").parts:
            continue
        rows.append(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(root).as_posix()}\n"
        )
    return f"sha256:{hashlib.sha256(''.join(rows).encode('utf-8')).hexdigest()}"


if __name__ == "__main__":
    unittest.main()
