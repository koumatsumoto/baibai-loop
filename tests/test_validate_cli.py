from __future__ import annotations

import io
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

from baibai_loop.validate.cli import _format_finding, main, run_validation
from baibai_loop.validate.errors import ValidationFinding


def _make_screened_payload() -> dict[str, object]:
    return {
        "run_date": "2026-04-24",
        "asof_date": "2026-04-24",
        "universe_size": 1,
        "filters": {"min_market_cap_oku": 300, "min_avg_turnover_oku": 2.0},
        "generated_by": "screening-cli-v1",
        "data_sources": ["j-quants-light"],
        "run_at": "2026-04-24T09:00:00+09:00",
        "run_id": "screening-20260424-a1b2c3d4",
        "config_hash": "a1b2c3d4e5f6a7b8",
        "cache_manifest_hash": "9988776655443322",
        "tickers": [
            {
                "ticker": "130A",
                "name": "Sample",
                "sector_33": "情報・通信業",
                "ttm_quality": {
                    "ev_ebitda": "exact",
                    "p_s": "approximated",
                    "pcfr": "unavailable",
                },
                "threshold_hit": ["sector_median_under_20pct_and_self_range_bottom_20pct"],
            }
        ],
    }


def _make_view_text() -> str:
    front = yaml.safe_dump(
        {
            "ai-draft": True,
            "published_at": "2026-04-27T09:00:00+09:00",
            "sectors": {"機械": "neutral"},
            "regions": {"us": "neutral"},
        },
        allow_unicode=True,
        sort_keys=False,
    )
    return f"---\n{front}---\n# View\n"


def _make_research_text() -> str:
    front = yaml.safe_dump(
        {
            "ticker": "2767",
            "name": "Sample",
            "playbook": "valuation-mean-reversion-v1",
            "decision": "accepted",
            "screened_ref": "screened/2026/04/2026-04-24.yaml",
            "view_ref": "view/2026/04/view-2026-04-24-bootstrap.md",
            "brief_refs": [],
            "ai-draft": True,
            "published_at": "2026-04-25T22:00:00+09:00",
            "tradable_at": "2026-05-15T09:00:00+09:00",
            "macro_gate": "neutral",
            "position_size_oku": 0.01,
            "valuation": {"per_trailing": 6.63},
        },
        allow_unicode=True,
        sort_keys=False,
    )
    body = textwrap.dedent(
        """
        # Research

        ## 1. Thesis
        ## 2. Macro gate
        ## 3. Valuation snapshot
        ## 4. 一時的割安の原因仮説
        ## 5. 反対仮説
        ## 6. Catalyst
        ## 7. Price reaction
        ## 8. Crowding
        ## 9. ミクロ
        ## 10. Entry
        ## 11. Exit
        ## 12. Invalidation
        ## 13. Position size
        """
    )
    return f"---\n{front}---\n{body}"


def _seed_repo(root: Path, *, screened_overrides: dict[str, object] | None = None) -> None:
    screened_dir = root / "screened" / "2026" / "04"
    view_dir = root / "view" / "2026" / "04"
    research_dir = root / "research" / "2026" / "04"
    playbooks_dir = root / "playbooks"
    for directory in (screened_dir, view_dir, research_dir, playbooks_dir):
        directory.mkdir(parents=True, exist_ok=True)

    payload = _make_screened_payload()
    if screened_overrides:
        payload.update(screened_overrides)
    (screened_dir / "2026-04-24.yaml").write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    (view_dir / "view-2026-04-24-bootstrap.md").write_text(_make_view_text(), encoding="utf-8")
    (research_dir / "2026-04-25-2767-valuation-mean-reversion-v1.md").write_text(
        _make_research_text(), encoding="utf-8"
    )

    schema_source = ROOT / "playbooks" / "valuation-mean-reversion-v1.schema.yaml"
    (playbooks_dir / "valuation-mean-reversion-v1.schema.yaml").write_text(
        schema_source.read_text(encoding="utf-8"), encoding="utf-8"
    )


class ValidateCliTests(unittest.TestCase):
    def test_valid_repository_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _seed_repo(root)
            stdout = io.StringIO()
            stderr = io.StringIO()
            exit_code = run_validation(
                root=root,
                targets=("screened", "view", "research", "ledger", "review"),
                stdout=stdout,
                stderr=stderr,
            )
            self.assertEqual(exit_code, 0, msg=stderr.getvalue())
            self.assertIn("0 error(s)", stdout.getvalue())

    def test_invalid_screened_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _seed_repo(root, screened_overrides={"run_id": "screening-20260424-XYZ"})
            stdout = io.StringIO()
            stderr = io.StringIO()
            exit_code = run_validation(
                root=root,
                targets=("screened",),
                stdout=stdout,
                stderr=stderr,
            )
            self.assertEqual(exit_code, 1)
            self.assertIn("screened.pattern", stderr.getvalue())

    def test_target_filter_skips_other_artefact_types(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _seed_repo(root)
            (root / "screened" / "2026" / "04" / "2026-04-24.yaml").write_text(
                "not-a-mapping\n", encoding="utf-8"
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            # screened は壊れているが target=view のみなので通る
            exit_code = run_validation(
                root=root,
                targets=("view",),
                stdout=stdout,
                stderr=stderr,
            )
            self.assertEqual(exit_code, 0, msg=stderr.getvalue())

    def test_main_against_repository_exits_zero(self) -> None:
        # Smoke: run via main() with --root pointed at the actual repo so the
        # CLI matches what CI will execute.
        argv = ["--root", str(ROOT)]
        exit_code = main(argv)
        self.assertEqual(exit_code, 0)

    def test_nonexistent_root_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "no-such-dir"
            stdout = io.StringIO()
            stderr = io.StringIO()
            exit_code = run_validation(
                root=missing,
                targets=("screened",),
                stdout=stdout,
                stderr=stderr,
            )
            self.assertEqual(exit_code, 1)
            self.assertIn("does not exist", stderr.getvalue())

    def test_root_pointing_to_file_exits_nonzero(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
            file_root = Path(tmp.name)
        try:
            stdout = io.StringIO()
            stderr = io.StringIO()
            exit_code = run_validation(
                root=file_root,
                targets=("screened",),
                stdout=stdout,
                stderr=stderr,
            )
            self.assertEqual(exit_code, 1)
            self.assertIn("not a directory", stderr.getvalue())
        finally:
            file_root.unlink()


class FormatFindingTests(unittest.TestCase):
    def test_format_uses_path_relative_to_root_when_inside(self) -> None:
        finding = ValidationFinding(
            severity="error",
            target=Path("/repo/screened/2026-04-24.yaml"),
            code="screened.required",
            message="missing run_id",
            location="run_id",
        )
        line = _format_finding(finding, Path("/repo"))
        self.assertIn("screened/2026-04-24.yaml", line)
        self.assertIn("@ run_id", line)
        self.assertIn("[error]", line)

    def test_format_falls_back_to_absolute_path_when_outside_root(self) -> None:
        finding = ValidationFinding(
            severity="warning",
            target=Path("/elsewhere/orphan.yaml"),
            code="screened.unknown-sector",
            message="unknown sector",
        )
        line = _format_finding(finding, Path("/repo"))
        self.assertIn("/elsewhere/orphan.yaml", line)
        self.assertNotIn("@ ", line)  # no location suffix when location is None
        self.assertIn("[warning]", line)


if __name__ == "__main__":
    unittest.main()
