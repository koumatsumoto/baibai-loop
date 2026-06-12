from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.ledger.sync import sync_ledger
from baibai_loop.screening.cli import select_sweep_command
from baibai_loop.validate.cli import run_validation
from baibai_loop.validate.ledger import validate_ledger_file
from baibai_loop.validate.research import validate_research_file
from baibai_loop.validate.trade import validate_trade_file

_RESEARCH_BODY = """
# Research

## 1. Thesis
E2E fixture for a current recommended ticker.

## 2. Macro context
E2E fixture.

## 3. Valuation snapshot
E2E fixture.

## 4. 一時的割安の原因仮説
E2E fixture.

## 5. 反対仮説
E2E fixture.

## 6. Catalyst
E2E fixture.

## 7. Price reaction
E2E fixture.

## 8. Positioning / liquidity
E2E fixture.

## 9. Shareholder return
E2E fixture.

## 10. Entry 条件
E2E fixture.

## 11. Exit 条件
E2E fixture.

## 12. Invalidation
E2E fixture.

## 13. Position size
E2E fixture.
"""


class OperationalE2ETests(unittest.TestCase):
    @unittest.skipUnless(
        (ROOT / "records/04-candidates/2026/05/2026-05-08.yaml").is_file(),
        "weekly candidates live in the local store; skip where absent",
    )
    def test_selection_profiles_and_existing_decision_records_validate(self) -> None:
        candidates_path = ROOT / "records/04-candidates/2026/05/2026-05-08.yaml"
        candidates_doc = yaml.safe_load(candidates_path.read_text(encoding="utf-8"))
        candidate_tickers = {
            str(candidate["ticker"]) for candidate in candidates_doc.get("candidates", [])
        }

        buffer = io.StringIO()
        exit_code = select_sweep_command(
            asof_date=date(2026, 5, 8),
            macro_context_path=(
                ROOT / "records/01-macro-context/2026/05/macro-context-2026-05-04-screening.yaml"
            ),
            top=10,
            profiles=("balanced",),
            candidates_root=ROOT / "records/04-candidates",
            macro_context_root=ROOT / "records/01-macro-context",
            stdout=buffer,
        )
        self.assertEqual(exit_code, 0)
        payload = yaml.safe_load(buffer.getvalue())
        profiles = {profile["profile"]: profile for profile in payload["profiles"]}
        self.assertEqual(set(profiles), {"balanced"})
        recommended_tickers: set[str] = set()
        for profile in profiles.values():
            self.assertEqual(profile["recommended_count"], 5)
            self.assertEqual(len(profile["recommended_tickers"]), 5)
            self.assertTrue(set(profile["recommended_tickers"]) <= candidate_tickers)
            self.assertTrue(set(profile["warnings"]) <= {"recommendations_high_previous_overlap"})
            recommended_tickers.update(str(ticker) for ticker in profile["recommended_tickers"])
        recommended_candidates = [
            candidate
            for candidate in candidates_doc["candidates"]
            if str(candidate["ticker"]) in recommended_tickers
        ]
        self.assertEqual(
            {str(candidate["ticker"]) for candidate in recommended_candidates},
            recommended_tickers,
        )
        self._assert_recommended_candidates_can_reach_decision_records(
            recommended_candidates,
            candidates_path=candidates_path,
            macro_context_path=(
                ROOT / "records/01-macro-context/2026/05/macro-context-2026-05-04-screening.yaml"
            ),
        )

        checked_paths = [
            ROOT / "records/05-research/2026/05/2026-05-13-8255-cashflow-yield-discount.md",
            ROOT / "records/06-trades/2026/05/2026-05-13-8255.md",
            ROOT / "records/_ledger/research-decisions/2026-05.jsonl",
        ]
        findings = [
            *validate_research_file(checked_paths[0]),
            *validate_trade_file(checked_paths[1]),
            *validate_ledger_file(checked_paths[2]),
        ]
        errors = [finding for finding in findings if finding.severity == "error"]
        self.assertEqual(errors, [], f"unexpected validation errors: {errors}")

        research_front = yaml.safe_load(
            checked_paths[0].read_text(encoding="utf-8").split("---", 2)[1]
        )
        self.assertEqual(
            research_front["candidate_ref"]["candidates_ref"],
            candidates_path.relative_to(ROOT).as_posix(),
        )
        self.assertIn(research_front["candidate_ref"]["ticker"], candidate_tickers)

    def _assert_recommended_candidates_can_reach_decision_records(
        self,
        candidates: list[dict[str, object]],
        *,
        candidates_path: Path,
        macro_context_path: Path,
    ) -> None:
        candidates_ref = candidates_path.relative_to(ROOT).as_posix()
        macro_context_ref = macro_context_path.relative_to(ROOT).as_posix()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            shutil.copytree(ROOT / "records/_playbooks", root / "records/_playbooks")
            self._write_yaml(
                root / candidates_ref,
                {"run_id": "screening-20260508", "candidates": candidates},
            )
            (root / macro_context_ref).parent.mkdir(parents=True, exist_ok=True)
            (root / macro_context_ref).write_text(
                macro_context_path.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            playbook_ref = "records/_playbooks/valuation-reversion/2026-05-01T000000+0900.md"
            expected_tickers = {str(candidate["ticker"]) for candidate in candidates}

            for candidate in candidates:
                ticker = str(candidate["ticker"])
                research_ref = f"records/05-research/2026/05/2026-05-08-{ticker}-e2e.md"
                trade_ref = f"records/06-trades/2026/05/2026-05-08-{ticker}.md"
                research_front = self._research_front(
                    candidate,
                    candidates_ref=candidates_ref,
                    macro_context_ref=macro_context_ref,
                    playbook_ref=playbook_ref,
                )
                research_path = root / research_ref
                research_path.parent.mkdir(parents=True, exist_ok=True)
                research_path.write_text(
                    "---\n"
                    + yaml.safe_dump(research_front, allow_unicode=True, sort_keys=False)
                    + "---\n"
                    + _RESEARCH_BODY,
                    encoding="utf-8",
                )

                trade_path = root / trade_ref
                trade_path.parent.mkdir(parents=True, exist_ok=True)
                trade_path.write_text(
                    "---\n"
                    + yaml.safe_dump(
                        self._trade_front(ticker=ticker, research_ref=research_ref),
                        allow_unicode=True,
                        sort_keys=False,
                    )
                    + "---\n\n# Trade\n",
                    encoding="utf-8",
                )

            sync_result = sync_ledger(root)
            self.assertEqual(sync_result.warnings, ())
            self.assertEqual(sync_result.decision_count, len(candidates) * 2)

            stdout = io.StringIO()
            stderr = io.StringIO()
            exit_code = run_validation(
                root=root,
                targets=("research", "trade", "ledger"),
                stdout=stdout,
                stderr=stderr,
            )
            self.assertEqual(exit_code, 0, stderr.getvalue())

            ledger_path = root / "records/_ledger/research-decisions/2026-05.jsonl"
            rows = [
                json.loads(line)
                for line in ledger_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            scopes_by_ticker = {
                ticker: {row["decision_scope"] for row in rows if row["ticker"] == ticker}
                for ticker in expected_tickers
            }
            self.assertEqual(
                scopes_by_ticker,
                {ticker: {"research_memo", "trade_execution"} for ticker in expected_tickers},
            )
            for row in rows:
                self.assertIn(row["ticker"], expected_tickers)
                if row["decision_scope"] == "research_memo":
                    self.assertEqual(
                        row["candidate_ref"],
                        {"candidates_ref": candidates_ref, "ticker": row["ticker"]},
                    )
                    self.assertEqual(
                        row["research_ref"],
                        f"records/05-research/2026/05/2026-05-08-{row['ticker']}-e2e.md",
                    )
                if row["decision_scope"] == "trade_execution":
                    self.assertEqual(
                        row["trade_ref"],
                        f"records/06-trades/2026/05/2026-05-08-{row['ticker']}.md",
                    )

    @staticmethod
    def _write_yaml(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    @staticmethod
    def _research_front(
        candidate: dict[str, object],
        *,
        candidates_ref: str,
        macro_context_ref: str,
        playbook_ref: str,
    ) -> dict[str, object]:
        ticker = str(candidate["ticker"])
        avg_turnover_oku = float(candidate.get("avg_turnover_oku") or 1)
        paper_yen = 450_000
        real_order_intent_yen = 90_000
        sector = str(candidate.get("sector_33") or "")
        metrics = candidate.get("metrics") if isinstance(candidate.get("metrics"), dict) else {}
        assert isinstance(metrics, dict)
        valuation = {
            field: metrics[field]
            for field in ("p_s", "ocf_yield", "fcf_yield", "net_cash_to_market_cap")
            if field in metrics
        }
        return {
            "ticker": ticker,
            "name": str(candidate.get("name") or "E2E Candidate"),
            "playbook_id": "valuation-reversion",
            "playbook_ref": {
                "ref_path": playbook_ref,
                "effective_from": "2026-05-01T00:00:00+09:00",
            },
            "candidate_ref": {"candidates_ref": candidates_ref, "ticker": ticker},
            "research_decision": {
                "outcome": "approved",
                "posture": "act_now",
                "reason_code": "e2e_recommended_candidate",
            },
            "macro_context_ref": macro_context_ref,
            "macro_context_fit": {
                "context_freshness": "current",
                "fit": OperationalE2ETests._macro_fit_for_sector(sector),
                "decision_effect": "proceed",
                "required_checks": [],
                "sizing_caution": [],
            },
            "position_sizing_overlay": {
                "paper_proxy_position_size_yen": paper_yen,
                "real_order_intent_yen": real_order_intent_yen,
                "adv_participation_pct": round(
                    paper_yen / (avg_turnover_oku * 100_000_000) * 100, 4
                ),
            },
            "thesis_payoff": {
                "max_entry_price_yen": 900,
                "target_price_yen": 1170,
                "stop_loss_yen": 810,
                "expected_upside_pct": 30.0,
                "expected_downside_pct": 10.0,
                "risk_reward_ratio": 3.0,
                "time_horizon_bd": 30,
                "invalidation_conditions": ["E2E invalidation"],
            },
            "corporate_action_check": {"checked": True, "result": "none"},
            "sector_33": sector,
            "avg_turnover_oku": avg_turnover_oku,
            "market_cap_oku": candidate.get("market_cap_oku"),
            "valuation": valuation,
            "published_at": "2026-05-08T20:00:00+09:00",
        }

    @staticmethod
    def _trade_front(*, ticker: str, research_ref: str) -> dict[str, object]:
        quantity = 100
        guard = 900
        order_intent_id = f"intent-20260508-{ticker}-entry"
        return {
            "trade_id": f"trade-20260508-{ticker}",
            "ticker": ticker,
            "playbook_id": "valuation-reversion",
            "research_ref": research_ref,
            "position_state": "none",
            "review_state": "not_due",
            "trade_execution_state": "submitted",
            "order_intent": {
                "order_intent_id": order_intent_id,
                "decision_event_id": f"decision-20260508-{ticker}-trade",
                "side": "buy",
                "quantity": quantity,
                "order_price_guard_yen": guard,
                "uses_margin": False,
            },
            "position_sizing_overlay": {
                "estimated_real_order_notional_yen": quantity * guard,
                "guarded_max_notional_yen": quantity * guard,
            },
            "orders": [
                {
                    "order_id": f"order-20260508-{ticker}-entry",
                    "origin_order_intent_id": order_intent_id,
                    "side": "buy",
                    "state": "submitted",
                    "submitted_quantity": quantity,
                    "filled_quantity": 0,
                    "order_price_guard_yen": guard,
                }
            ],
            "executions": [],
        }

    @staticmethod
    def _macro_fit_for_sector(sector: str) -> str:
        return {
            "情報・通信業": "neutral",
            "機械": "mixed",
            "サービス業": "neutral",
        }.get(sector, "not_matched")


if __name__ == "__main__":
    unittest.main()
