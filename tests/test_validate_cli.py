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


def _make_candidates_payload() -> dict[str, object]:
    snapshot = {
        "ref_path": "records/_config/screening-rules/2026-05-01T000000+0900.yaml",
    }
    return {
        "run_date": "2026-04-24",
        "asof_date": "2026-04-24",
        "universe_size": 1,
        "filters": {"min_market_cap_oku": 200, "min_avg_turnover_oku": 3.0},
        "generated_by": "screening-cli-v1",
        "data_sources": ["j-quants-light"],
        "run_at": "2026-04-24T09:00:00+09:00",
        "run_id": "screening-20260424",
        "universe_ref": {
            **snapshot,
            "ref_path": "records/_universe-snapshots/2026/04/2026-04-24.yaml",
        },
        "candidates": [
            {
                "ticker": "2767",
                "name": "Sample",
                "screen_run_id": "screening-20260424",
                "candidate_id": "candidate-2026-04-24-2767",
                "candidate_key": "screening-20260424:2767",
                "playbook_screen_result": "hit",
                "policy_gate_result": "pass",
                "liquidity_gate_result": "pass",
                "sector_33": "情報・通信業",
                "metrics": {},
                "ttm_quality": {
                    "ev_ebitda": "exact",
                    "p_s": "approximated",
                    "pcfr": "unavailable",
                    "ocf_yield": "unavailable",
                    "sales": "approximated",
                    "fcf_yield": "unavailable",
                    "net_cash": "unavailable",
                },
                "evidence_hits": [
                    {
                        "evidence_hit_id": "candidate-2026-04-24-2767-valuation-reversion",
                        "playbook_id": "valuation-reversion",
                        "claim_id": "2767-valuation-reversion",
                        "claim_type": "valuation_reversion",
                        "evidence_family_set": ["valuation"],
                        "decision_role": "sizing_evidence",
                        "evidence_polarity": "supports",
                        "source_status": "ok",
                        "sizing_eligible": True,
                        "independence_component_id": "valuation-reversion",
                        "reasons": ["sector_median_discount_and_self_range_bottom"],
                        "metrics": {},
                    }
                ],
            }
        ],
    }


_TSE_33_SECTORS: tuple[str, ...] = (
    "水産・農林業",
    "鉱業",
    "建設業",
    "食料品",
    "繊維製品",
    "パルプ・紙",
    "化学",
    "医薬品",
    "石油・石炭製品",
    "ゴム製品",
    "ガラス・土石製品",
    "鉄鋼",
    "非鉄金属",
    "金属製品",
    "機械",
    "電気機器",
    "輸送用機器",
    "精密機器",
    "その他製品",
    "電気・ガス業",
    "陸運業",
    "海運業",
    "空運業",
    "倉庫・運輸関連業",
    "情報・通信業",
    "卸売業",
    "小売業",
    "銀行業",
    "証券、商品先物取引業",
    "保険業",
    "その他金融業",
    "不動産業",
    "サービス業",
)


def _make_macro_context_yaml_text() -> str:
    payload: dict[str, object] = {
        "kind": "macro-context",
        "context_id": "macro-context-2026-04-24-test",
        "as_of": "2026-04-24",
        "valid_until": "2026-05-01",
        "published_at": "2026-04-24T09:00:00+09:00",
        "summary": "summary",
        "inputs": {
            "articles": [],
            "stats_series": [{"series_id": "usd_jpy", "window": "1m", "used_for": "test"}],
        },
        "sector_tilts": {
            "items": [
                {
                    "id": "sector-info",
                    "scope": "sector_33",
                    "key": "情報・通信業",
                    "stance": "neutral",
                    "strength": "medium",
                    "confidence": "medium",
                    "rationale": "test",
                }
            ]
        },
        "research_questions": ["question"],
        "refresh_triggers": ["trigger"],
        "changes_since_previous": [],
    }
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


def _make_research_text() -> str:
    front = yaml.safe_dump(
        {
            "ticker": "2767",
            "name": "Sample",
            "playbook_id": "valuation-reversion",
            "playbook_ref": {
                "ref_path": "records/_playbooks/valuation-reversion/2026-05-01T000000+0900.md",
            },
            "research_decision": {"outcome": "approved", "posture": "act_now"},
            "candidate_ref": {
                "candidates_ref": "records/04-candidates/2026/04/2026-04-24.yaml",
                "screen_run_id": "screening-20260424",
                "ticker": "2767",
                "candidate_id": "candidate-2026-04-24-2767",
            },
            "market_cap_oku": 600,
            "sector_33": "情報・通信業",
            "macro_context_ref": (
                "records/01-macro-context/2026/04/macro-context-2026-04-24-test.yaml"
            ),
            "macro_context_fit": {
                "context_freshness": "current",
                "fit": "neutral",
                "decision_effect": "proceed",
                "required_checks": [],
                "sizing_caution": [],
            },
            "ai_draft": True,
            "published_at": "2026-04-25T22:00:00+09:00",
            "recorded_at": "2026-04-25T22:00:00+09:00",
            "tradable_at": "2026-05-15T09:00:00+09:00",
            "candidate_evidence_decisions": [
                {
                    "evidence_hit_id": "candidate-2026-04-24-2767-valuation-reversion",
                    "effective_sizing_eligible": True,
                    "evaluated_at": "2026-04-25T22:00:00+09:00",
                    "reason_code": "source_status_ok",
                }
            ],
            "selected_supporting_evidence_refs": [
                {
                    "source": "candidate",
                    "evidence_hit_id": "candidate-2026-04-24-2767-valuation-reversion",
                }
            ],
            "research_evidence_hits": [
                {
                    "evidence_hit_id": "research-2767-risk-review",
                    "decision_role": "risk_evidence",
                    "evidence_polarity": "risk",
                    "evidence_family_set": ["fundamental"],
                    "source_status": "ok",
                    "sizing_eligible": False,
                }
            ],
            "independent_evidence_count": 1,
            "conviction_tier": "medium",
            "conviction_tier_path": "count_breadth",
            "position_sizing_overlay": {
                "paper_proxy_position_size_oku": 0.01,
                "paper_proxy_position_size_yen": 1000000,
                "real_order_intent_yen": 0,
                "adv_participation_pct": 0.5,
            },
            "thesis_payoff": {
                "max_entry_price_yen": 1000,
                "target_price_yen": 1200,
                "stop_loss_yen": 900,
                "expected_upside_pct": 20.0,
                "expected_downside_pct": 10.0,
                "risk_reward_ratio": 2.0,
            },
            "avg_turnover_oku": 2.0,
            "valuation": {"per_trailing": 6.63},
        },
        allow_unicode=True,
        sort_keys=False,
    )
    body = textwrap.dedent(
        """
        # Research

        ## 1. Thesis
        ## 2. Macro context
        ## 3. Valuation snapshot
        ## 4. 一時的割安の原因仮説
        ## 5. 反対仮説
        ## 6. Catalyst
        ## 7. Price reaction
        ## 8. Positioning / liquidity
        ## 9. Shareholder return
        ## 10. Entry
        ## 11. Exit
        ## 12. Invalidation
        ## 13. Position size
        """
    )
    return f"---\n{front}---\n{body}"


def _seed_repo(root: Path, *, candidates_overrides: dict[str, object] | None = None) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    macro_context_dir = root / "records/01-macro-context" / "2026" / "04"
    candidates_dir = root / "records/04-candidates" / "2026" / "04"
    playbooks_dir = root / "records/_playbooks"
    docs_dir = root / "docs"
    support_dirs = (
        docs_dir,
        root / "records/_universe-snapshots/2026/04",
        root / "records/_playbooks/valuation-reversion",
        root / "records/05-research/2026/04",
        root / "records/_calendars/business-days",
        root / "records/_calendars/events",
        root / "records/_calendars/corporate-actions",
    )
    for directory in (
        macro_context_dir,
        candidates_dir,
        playbooks_dir,
        *support_dirs,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    payload = _make_candidates_payload()
    if candidates_overrides:
        payload.update(candidates_overrides)
    (candidates_dir / "2026-04-24.yaml").write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    (macro_context_dir / "macro-context-2026-04-24-test.yaml").write_text(
        _make_macro_context_yaml_text(), encoding="utf-8"
    )
    (root / "records/05-research/2026/04/2026-04-25-2767-valuation-reversion.md").write_text(
        "---\n"
        "macro_context_ref: records/01-macro-context/2026/04/macro-context-2026-04-24-test.yaml\n"
        "---\n# Research\n",
        encoding="utf-8",
    )
    (root / "records/_universe-snapshots/2026/04/2026-04-24.yaml").write_text(
        "snapshot_id: universe-20260424\n"
        "as_of: '2026-04-24'\n"
        "universe_size: 1\n"
        "members_scope: full_universe\n"
        "members_recorded: 1\n"
        "members:\n"
        "- ticker: '2767'\n"
        "  sector_33: 情報・通信業\n",
        encoding="utf-8",
    )
    (root / "records/_playbooks/valuation-reversion/2026-05-01T000000+0900.md").write_text(
        "---\nplaybook_id: valuation-reversion\n---\n# Playbook\n", encoding="utf-8"
    )
    policy_source = ROOT / "docs/portfolio-policy.md"
    (root / "docs/portfolio-policy.md").write_text(
        policy_source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (root / "records/_calendars/business-days/2026-05.yaml").write_text(
        "business_days: []\n", encoding="utf-8"
    )
    (root / "records/_calendars/events/2026-05.yaml").write_text("events: []\n", encoding="utf-8")
    (root / "records/_calendars/corporate-actions/2026-05.yaml").write_text(
        "events: []\n", encoding="utf-8"
    )
    playbook_schema_dir = playbooks_dir / "valuation-reversion"
    playbook_schema_dir.mkdir(parents=True, exist_ok=True)
    schema_source = ROOT / "records/_playbooks" / "valuation-reversion" / "body-schema.yaml"
    (playbook_schema_dir / "body-schema.yaml").write_text(
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
                targets=("macro-context", "policy", "candidates", "references"),
                stdout=stdout,
                stderr=stderr,
            )
            self.assertEqual(exit_code, 0, msg=stderr.getvalue())
            self.assertIn("0 error(s)", stdout.getvalue())

    def test_invalid_candidates_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _seed_repo(root, candidates_overrides={"run_id": "screening-20260424-XYZ"})
            stdout = io.StringIO()
            stderr = io.StringIO()
            exit_code = run_validation(
                root=root,
                targets=("candidates",),
                stdout=stdout,
                stderr=stderr,
            )
            self.assertEqual(exit_code, 1)
            self.assertIn("candidates.pattern", stderr.getvalue())

    def test_target_filter_skips_other_artefact_types(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _seed_repo(root)
            (root / "records/04-candidates" / "2026" / "04" / "2026-04-24.yaml").write_text(
                "not-a-mapping\n", encoding="utf-8"
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            # candidates は壊れているが target=macro-context のみなので通る
            exit_code = run_validation(
                root=root,
                targets=("macro-context",),
                stdout=stdout,
                stderr=stderr,
            )
            self.assertEqual(exit_code, 0, msg=stderr.getvalue())

    def test_policy_target_requires_repository_policy_doc(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _seed_repo(root)
            (root / "docs/portfolio-policy.md").unlink()
            stdout = io.StringIO()
            stderr = io.StringIO()

            exit_code = run_validation(
                root=root,
                targets=("policy",),
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(exit_code, 1)
            self.assertIn("policy.io", stderr.getvalue())

    def test_references_target_validates_repository_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _seed_repo(root)
            (root / "records/_universe-snapshots/2026/04/2026-04-24.yaml").unlink()
            stdout = io.StringIO()
            stderr = io.StringIO()

            exit_code = run_validation(
                root=root,
                targets=("references",),
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(exit_code, 1)
            self.assertIn("reference.ref-not-found", stderr.getvalue())

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
                targets=("candidates",),
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
                targets=("candidates",),
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
            target=Path("/repo/records/04-candidates/2026-04-24.yaml"),
            code="candidates.required",
            message="missing run_id",
            location="run_id",
        )
        line = _format_finding(finding, Path("/repo"))
        self.assertIn("records/04-candidates/2026-04-24.yaml", line)
        self.assertIn("@ run_id", line)
        self.assertIn("[error]", line)

    def test_format_falls_back_to_absolute_path_when_outside_root(self) -> None:
        finding = ValidationFinding(
            severity="warning",
            target=Path("/elsewhere/orphan.yaml"),
            code="candidates.unknown-sector",
            message="unknown sector",
        )
        line = _format_finding(finding, Path("/repo"))
        self.assertIn("/elsewhere/orphan.yaml", line)
        self.assertNotIn("@ ", line)  # no location suffix when location is None
        self.assertIn("[warning]", line)


if __name__ == "__main__":
    unittest.main()
