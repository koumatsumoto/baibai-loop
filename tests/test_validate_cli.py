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
        "content_sha256": "sha256:" + "1" * 64,
    }
    return {
        "run_date": "2026-04-24",
        "asof_date": "2026-04-24",
        "universe_size": 1,
        "filters": {"min_market_cap_oku": 200, "min_avg_turnover_oku": 3.0},
        "generated_by": "screening-cli-v1",
        "data_sources": ["j-quants-light"],
        "run_at": "2026-04-24T09:00:00+09:00",
        "run_id": "screening-20260424-a1b2c3d4",
        "screening_rules_snapshot": snapshot,
        "metric_catalog_snapshot": {
            **snapshot,
            "ref_path": "records/_config/metric-catalog/2026-05-01T000000+0900.yaml",
        },
        "policy_snapshot": {
            **snapshot,
            "ref_path": "records/01-policy/2026/05/2026-05-01T000000+0900-portfolio-policy.md",
        },
        "universe_snapshot_ref": {
            **snapshot,
            "ref_path": "records/_universe-snapshots/2026/04/2026-04-24.yaml",
        },
        "cache_manifest_hash": "9988776655443322",
        "candidates": [
            {
                "ticker": "2767",
                "name": "Sample",
                "screen_run_id": "screening-20260424-a1b2c3d4",
                "candidate_id": "candidate-2026-04-24-2767",
                "candidate_key": "screening-20260424-a1b2c3d4:2767",
                "playbook_screen_result": "hit",
                "policy_gate_result": "pass",
                "liquidity_gate_result": "pass",
                "macro_regime_gate_result": "pass",
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


def _make_outlook_yaml_text() -> str:
    judgement = {"status": "neutral", "rationale": "neutral", "source_refs": []}
    payload: dict[str, object] = {
        "schema_version": 1,
        "ai_draft": True,
        "published_at": "2026-04-27T09:00:00+09:00",
        "horizon": "1-6m",
        "updated_from": ["records/02-brief/2026/04/2026-04-19-world-weekly-x.yaml"],
        "summary": "summary",
        "sectors": {sector: dict(judgement) for sector in _TSE_33_SECTORS},
        "exposure_buckets": {
            "us": dict(judgement),
            "japan-domestic": dict(judgement),
            "japan-external-demand": {**judgement, "status": "supportive"},
            "emerging": {**judgement, "status": None},
        },
        "changes": [],
        "next_triggers": [{"date": "2026-05-12", "text": "BoJ"}],
    }
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


def _make_brief_yaml_text() -> str:
    payload: dict[str, object] = {
        "schema_version": 1,
        "kind": "world-weekly",
        "type": "periodic",
        "scope": "world",
        "ai_draft": True,
        "published_at": "2026-04-19T18:00:00+09:00",
        "observation_date": "2026-04-19",
        "period": {"start": "2026-04-13", "end": "2026-04-19"},
        "sources": [
            {
                "id": "fed-h15",
                "name": "Federal Reserve H.15",
                "url": "https://www.federalreserve.gov/releases/h15/",
                "accessed_at": "2026-04-19",
                "status": "ok",
            }
        ],
        "layers": {
            "world": {
                "market_indicators": [
                    {
                        "name": "米10Y",
                        "value": "4.31%",
                        "source_ids": ["fed-h15"],
                    }
                ]
            },
            "japan": {},
            "japan_equity": {},
        },
        "deltas": {"threshold_breaches": [], "direction_history": []},
        "next_events": [{"date": "2026-04-28", "text": "FOMC"}],
    }
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


def _make_research_text() -> str:
    front = yaml.safe_dump(
        {
            "ticker": "2767",
            "name": "Sample",
            "playbook_id": "valuation-reversion",
            "playbook_snapshot": {
                "ref_path": "records/_playbooks/valuation-reversion/2026-05-01T000000+0900.md",
                "content_sha256": "sha256:" + "1" * 64,
            },
            "policy_snapshot": {
                "ref_path": "records/01-policy/2026/05/2026-05-01T000000+0900-portfolio-policy.md",
                "content_sha256": "sha256:" + "2" * 64,
            },
            "portfolio_exposure_snapshot_ref": {
                "ref_path": "records/_portfolio-exposure/2026/05/2026-05-05T133000+0900.yaml",
                "content_sha256": "sha256:" + "3" * 64,
            },
            "research_decision": {"outcome": "approved", "posture": "act_now"},
            "candidate_ref": {
                "candidates_ref": "records/04-candidates/2026/04/2026-04-24.yaml",
                "screen_run_id": "screening-20260424-a1b2c3d4",
                "ticker": "2767",
                "candidate_id": "candidate-2026-04-24-2767",
            },
            "market_cap_oku": 600,
            "sector_33": "情報・通信業",
            "candidates_ref": "records/04-candidates/2026/04/2026-04-24.yaml",
            "outlook_ref": "records/03-outlook/2026/04/outlook-2026-04-24-bootstrap.yaml",
            "brief_refs": [],
            "ai_draft": True,
            "published_at": "2026-04-25T22:00:00+09:00",
            "recorded_at": "2026-04-25T22:00:00+09:00",
            "tradable_at": "2026-05-15T09:00:00+09:00",
            "macro_regime_gate": {
                "aggregate_status": "neutral",
                "decision_effect": "pass",
                "source_scope": "sector",
            },
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
            "position_sizing_overlay": {"paper_proxy_position_size_oku": 0.01},
            "thesis_payoff": {
                "max_entry_price_yen": 1000,
                "target_price_yen": 1200,
                "stop_loss_yen": 900,
                "expected_upside_pct": 20.0,
                "expected_downside_pct": 11.11,
                "risk_reward_ratio": 1.8,
            },
            "valuation": {"per_trailing": 6.63},
        },
        allow_unicode=True,
        sort_keys=False,
    )
    body = textwrap.dedent(
        """
        # Research

        ## 1. Thesis
        ## 2. Macro regime gate
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
    brief_dir = root / "records/02-brief" / "2026" / "04"
    outlook_dir = root / "records/03-outlook" / "2026" / "04"
    candidates_dir = root / "records/04-candidates" / "2026" / "04"
    research_dir = root / "records/05-research" / "2026" / "04"
    playbooks_dir = root / "records/_playbooks"
    for directory in (brief_dir, outlook_dir, candidates_dir, research_dir, playbooks_dir):
        directory.mkdir(parents=True, exist_ok=True)

    payload = _make_candidates_payload()
    if candidates_overrides:
        payload.update(candidates_overrides)
    (candidates_dir / "2026-04-24.yaml").write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    (brief_dir / "2026-04-19-world-weekly-x.yaml").write_text(
        _make_brief_yaml_text(), encoding="utf-8"
    )
    (outlook_dir / "outlook-2026-04-24-bootstrap.yaml").write_text(
        _make_outlook_yaml_text(), encoding="utf-8"
    )
    (research_dir / "2026-04-25-2767-valuation-reversion.md").write_text(
        _make_research_text(), encoding="utf-8"
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
                targets=("brief", "candidates", "outlook", "research", "ledger", "review"),
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
            # candidates は壊れているが target=outlook のみなので通る
            exit_code = run_validation(
                root=root,
                targets=("outlook",),
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
