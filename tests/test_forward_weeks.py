from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from baibai_loop.screening.forward.weeks import WeekSpec, discover_week_specs, load_week_candidates


def _write_week(root: Path, asof: str, body: str) -> Path:
    path = root / asof[:4] / asof[5:7] / f"{asof}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


class DiscoverWeekSpecsTests(unittest.TestCase):
    def test_discover_week_specs_sorts_by_asof_and_skips_non_dates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_week(root, "2026-05-08", "candidates: []\n")
            _write_week(root, "2026-05-01", "candidates: []\n")
            (root / "notes.yaml").write_text("x: 1\n", encoding="utf-8")

            specs = discover_week_specs(root)

            self.assertEqual([spec.asof for spec in specs], [date(2026, 5, 1), date(2026, 5, 8)])

    def test_discover_week_specs_flags_trailing_holdout_weeks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for asof in ("2026-05-01", "2026-05-08", "2026-05-15"):
                _write_week(root, asof, "candidates: []\n")

            specs = discover_week_specs(root, holdout_weeks=1)

            self.assertEqual([spec.is_holdout for spec in specs], [False, False, True])


class LoadWeekCandidatesTests(unittest.TestCase):
    def test_load_week_candidates_returns_only_mappings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_week(
                Path(tmpdir),
                "2026-05-08",
                "candidates:\n  - ticker: '7203'\n  - not-a-mapping\n",
            )

            candidates = load_week_candidates(path)

            self.assertEqual(candidates, ({"ticker": "7203"},))

    def test_load_week_candidates_with_non_mapping_root_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_week(Path(tmpdir), "2026-05-08", "- just\n- a list\n")

            with self.assertRaisesRegex(ValueError, "invalid candidates YAML"):
                load_week_candidates(path)

    def test_load_week_candidates_without_candidates_list_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_week(Path(tmpdir), "2026-05-08", "candidates: not-a-list\n")

            with self.assertRaisesRegex(ValueError, "candidates list missing"):
                load_week_candidates(path)

    def test_week_spec_is_immutable(self) -> None:
        spec = WeekSpec(asof=date(2026, 5, 8), candidates_path=Path("x.yaml"))
        with self.assertRaises(AttributeError):
            spec.is_holdout = True  # type: ignore[misc]  # frozen dataclass の検証


if __name__ == "__main__":
    unittest.main()
