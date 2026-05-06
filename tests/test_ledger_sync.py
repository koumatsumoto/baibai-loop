from __future__ import annotations

import json
import os
import textwrap
from datetime import date
from pathlib import Path

import pytest
import yaml

from baibai_loop.ledger.cli import _discover_decision_dates, _load_market_data, main
from baibai_loop.ledger.io import diff_jsonl, read_jsonl, write_jsonl
from baibai_loop.ledger.retro import build_monthly_retro
from baibai_loop.ledger.sync import sync_ledger
from baibai_loop.validate.review import validate_review_file


def _seed(root: Path) -> None:
    candidates_dir = root / "records/04-candidates" / "2026" / "04"
    research_dir = root / "records/05-research" / "2026" / "04"
    candidates_dir.mkdir(parents=True)
    research_dir.mkdir(parents=True)
    candidates_path = candidates_dir / "2026-04-24.yaml"
    candidates_path.write_text(
        yaml.safe_dump(
            {
                "asof_date": "2026-04-24",
                "candidates": [
                    {
                        "ticker": "2767",
                        "name": "Sample",
                        "market_cap_oku": 936,
                        "avg_turnover_oku": 4.9,
                        "last_price": 1431.0,
                        "screen_run_id": "screening-20260424",
                        "candidate_id": "candidate-2026-04-24-2767",
                        "candidate_key": "screening-20260424:2767",
                    }
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
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
                "ref_path": "records/01-policy/2026/05/policy.md",
                "content_sha256": "sha256:" + "2" * 64,
            },
            "research_decision": {"outcome": "approved", "posture": "act_now"},
            "candidates_ref": str(candidates_path.relative_to(root)),
            "candidate_ref": {
                "candidates_ref": str(candidates_path.relative_to(root)),
                "screen_run_id": "screening-20260424",
                "ticker": "2767",
                "candidate_id": "candidate-2026-04-24-2767",
            },
            "published_at": "2026-04-25T22:00:00+09:00",
            "recorded_at": "2026-04-25T22:00:00+09:00",
            "independent_evidence_count": 1,
            "conviction_tier": "medium",
            "tracking": {"mode": "post_approval", "plus_15bd": None, "plus_30bd": None},
        },
        allow_unicode=True,
        sort_keys=False,
    )
    body = textwrap.dedent(
        """
        # Investment memo

        | 最新 adj close (2026-04-24) | 1,431 円 | — |
        """
    )
    (research_dir / "2026-04-25-2767-valuation-reversion.md").write_text(
        f"---\n{front}---\n{body}",
        encoding="utf-8",
    )


def _seed_trade(root: Path) -> None:
    trade_dir = root / "records/06-trades" / "2026" / "04"
    trade_dir.mkdir(parents=True)
    front = yaml.safe_dump(
        {
            "trade_id": "trade-20260425-2767",
            "ticker": "2767",
            "name": "Sample",
            "research_ref": "records/05-research/2026/04/2026-04-25-2767-valuation-reversion.md",
            "policy_snapshot": {
                "ref_path": "records/01-policy/2026/05/policy.md",
                "content_sha256": "sha256:" + "2" * 64,
            },
            "trade_execution_state": "submitted",
            "order_intent": {
                "order_intent_id": "intent-20260425-2767-buy",
                "decision_event_id": "decision-20260425-2767-trade",
                "side": "buy",
                "quantity": 100,
                "order_price_guard_yen": 1500,
            },
            "orders": [
                {
                    "order_id": "order-20260425-2767-buy-1",
                    "origin_order_intent_id": "intent-20260425-2767-buy",
                    "side": "buy",
                    "state": "submitted",
                    "submitted_quantity": 100,
                    "filled_quantity": 0,
                    "order_price_guard_yen": 1500,
                    "events": [
                        {
                            "event_id": "order-20260425-2767-buy-1-submit",
                            "event_type": "submit",
                            "at": "2026-04-25T22:30:00+09:00",
                            "quantity": 100,
                        }
                    ],
                }
            ],
            "executions": [],
        },
        allow_unicode=True,
        sort_keys=False,
    )
    (trade_dir / "2026-04-25-2767.md").write_text(
        f"---\n{front}---\n# Trade\n",
        encoding="utf-8",
    )


def test_sync_ledger_writes_idempotent_decision_register(tmp_path: Path) -> None:
    _seed(tmp_path)
    first = sync_ledger(tmp_path)
    second = sync_ledger(tmp_path)
    assert first.decision_count == 1
    assert second.decision_count == 1
    register_path = tmp_path / "records/_ledger" / "research-decisions" / "2026-04.jsonl"
    lines = register_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["decision_event_id"] == "decision-20260425-2767-research"
    assert record["candidate_decision"] == "selected"
    assert record["tracking"] == {
        "mode": "post_approval",
        "plus_15bd": None,
        "plus_30bd": None,
    }


def test_sync_ledger_includes_trade_execution_events(tmp_path: Path) -> None:
    _seed(tmp_path)
    _seed_trade(tmp_path)
    result = sync_ledger(tmp_path)

    assert result.decision_count == 2
    register_path = tmp_path / "records/_ledger" / "research-decisions" / "2026-04.jsonl"
    rows = [json.loads(line) for line in register_path.read_text(encoding="utf-8").splitlines()]
    trade = next(row for row in rows if row["decision_scope"] == "trade_execution")
    assert trade["decision_event_id"] == "decision-20260425-2767-trade"
    assert trade["trade_execution_state"] == "submitted"
    assert trade["order_intent"]["order_intent_id"] == "intent-20260425-2767-buy"
    assert trade["trade_ref"] == "records/06-trades/2026/04/2026-04-25-2767.md"


def test_sync_ledger_dry_run_reports_existing_decisions(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = sync_ledger(tmp_path, dry_run=True)
    assert result.decision_count == 1
    assert result.diff_lines == ("+ decision-20260425-2767-research",)


def test_sync_ledger_adds_not_reviewed_candidate_screen_events(tmp_path: Path) -> None:
    candidates_dir = tmp_path / "records/04-candidates" / "2026" / "04"
    candidates_dir.mkdir(parents=True)
    (candidates_dir / "2026-04-24.yaml").write_text(
        yaml.safe_dump(
            {
                "run_id": "screening-20260424-test",
                "asof_date": "2026-04-24",
                "run_at": "2026-04-24T23:59:59+09:00",
                "requires_decision_coverage": True,
                "candidates": [
                    {
                        "ticker": "1111",
                        "name": "Reviewable",
                        "screen_run_id": "screening-20260424-test",
                        "candidate_id": "candidate-2026-04-24-1111",
                        "playbook_screen_result": "hit",
                        "policy_gate_result": "pass",
                        "liquidity_gate_result": "pass",
                        "macro_regime_gate_result": "pass",
                        "market_cap_oku": 120,
                        "avg_turnover_oku": 1.5,
                        "evidence_hits": [
                            {
                                "name": "strict-net-cash-discount",
                                "source_status": "ok",
                                "sizing_eligible": True,
                            }
                        ],
                    },
                    {
                        "ticker": "2222",
                        "name": "Hard excluded",
                        "screen_run_id": "screening-20260424-test",
                        "candidate_id": "candidate-2026-04-24-2222",
                        "playbook_screen_result": "hit",
                        "policy_gate_result": "excluded",
                        "liquidity_gate_result": "pass",
                        "macro_regime_gate_result": "pass",
                    },
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = sync_ledger(tmp_path)

    assert result.decision_count == 1
    register_path = tmp_path / "records/_ledger" / "research-decisions" / "2026-04.jsonl"
    rows = [json.loads(line) for line in register_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    [record] = rows
    assert record["decision_scope"] == "candidate_screen"
    assert record["candidate_decision"] == "not_reviewed"
    assert record["not_reviewed_reason"] == "review_capacity"
    assert record["tracking"] == {
        "mode": "missed_opportunity",
        "plus_15bd": None,
        "plus_30bd": None,
    }
    assert record["candidate_ref"] == {
        "candidates_ref": "records/04-candidates/2026/04/2026-04-24.yaml",
        "screen_run_id": "screening-20260424-test",
        "ticker": "1111",
        "candidate_id": "candidate-2026-04-24-1111",
    }


def test_sync_ledger_adds_false_negative_scan_anchor_events(tmp_path: Path) -> None:
    scan_dir = tmp_path / "records/07-reviews/screening-false-negative-scan"
    scan_dir.mkdir(parents=True)
    (scan_dir / "2026-04.yaml").write_text(
        yaml.safe_dump(
            {
                "scan_id": "screening-false-negative-2026-04",
                "scan_month": "2026-04",
                "start_price_basis": "candidate_run_close_adjusted_close",
                "items": [
                    {
                        "scan_item_id": "screening-false-negative-2026-04-no-hit",
                        "ticker": "9999",
                        "decision_event_id": "decision-20260430-9999-no-hit-false-negative",
                        "classification": "screening_no_hit_control",
                        "flagged_at": "2026-04-30T00:00:00+09:00",
                    }
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = sync_ledger(tmp_path)

    assert result.decision_count == 1
    assert result.diff_lines == ()
    register_path = tmp_path / "records/_ledger" / "research-decisions" / "2026-04.jsonl"
    rows = [json.loads(line) for line in register_path.read_text(encoding="utf-8").splitlines()]
    [record] = rows
    assert record["decision_event_id"] == "decision-20260430-9999-no-hit-false-negative"
    assert record["decision_scope"] == "candidate_screen"
    assert record["candidate_decision"] == "not_reviewed"
    assert record["not_reviewed_reason"] == "screening_no_hit_control"
    assert record["tracking"] == {
        "mode": "missed_opportunity",
        "plus_15bd": None,
        "plus_30bd": None,
    }


def test_ledger_cli_warns_when_jquants_token_is_missing(tmp_path: Path) -> None:
    _seed(tmp_path)
    calendar, bars, warnings = _load_market_data(tmp_path, {})
    assert calendar == ()
    assert bars == ()
    assert warnings == ("JQUANTS_REFRESH_TOKEN is unset; tracking prices remain null",)


def test_ledger_cli_discovers_research_decision_dates(tmp_path: Path) -> None:
    _seed(tmp_path)
    assert _discover_decision_dates(tmp_path) == (date(2026, 4, 25),)


def test_ledger_cli_require_market_data_fails_without_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("JQUANTS_REFRESH_TOKEN", raising=False)
    assert main(["sync", "--root", str(tmp_path), "--dry-run", "--require-market-data"]) == 1


def test_ledger_cli_loads_dotenv_from_root_not_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _seed(repo_root)
    (repo_root / ".env").write_text("JQUANTS_REFRESH_TOKEN=from_root_dotenv\n", encoding="utf-8")

    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()
    monkeypatch.chdir(other_dir)
    monkeypatch.delenv("JQUANTS_REFRESH_TOKEN", raising=False)

    main(["sync", "--root", str(repo_root), "--dry-run", "--require-market-data"])
    assert os.environ["JQUANTS_REFRESH_TOKEN"] == "from_root_dotenv"


def test_ledger_cli_require_market_data_emits_diagnostic_when_no_research(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JQUANTS_REFRESH_TOKEN", "dummy-token")
    (tmp_path / "records/05-research").mkdir(parents=True)
    assert main(["sync", "--root", str(tmp_path), "--dry-run", "--require-market-data"]) == 1
    assert "no calendar/bars" in capsys.readouterr().err


def test_monthly_retro_draft_uses_decision_register_fallback(tmp_path: Path) -> None:
    _seed(tmp_path)
    sync_ledger(tmp_path)
    draft = build_monthly_retro(tmp_path, "2026-04")
    assert draft.path == tmp_path / "records/07-reviews" / "2026" / "retro-202604.md"
    assert "decision-register fallback" in draft.warnings[0]
    assert "price_missing_counts:" in draft.content
    assert "## Missed opportunity / screening false negative tracking の分析" in draft.content
    draft.path.parent.mkdir(parents=True)
    draft.path.write_text(draft.content, encoding="utf-8")
    assert validate_review_file(draft.path) == []


def test_ledger_cli_retro_writes_draft_and_refuses_overwrite(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed(tmp_path)
    sync_ledger(tmp_path)
    assert main(["retro", "--root", str(tmp_path), "--month", "2026-04"]) == 0
    assert (tmp_path / "records/07-reviews" / "2026" / "retro-202604.md").exists()
    assert main(["retro", "--root", str(tmp_path), "--month", "2026-04"]) == 1
    assert "already exists" in capsys.readouterr().err


def test_monthly_retro_counts_review_classes(tmp_path: Path) -> None:
    _seed(tmp_path)
    sync_ledger(tmp_path)
    reviews = tmp_path / "records/07-reviews" / "2026" / "04"
    reviews.mkdir(parents=True)
    (reviews / "2026-04-30-2767.md").write_text(
        """---
decision_event_id: decision-20260425-2767-research
research_ref: records/05-research/2026/04/2026-04-25-2767-valuation-reversion.md
trade_ref: null
classification: success
verified_at: "2026-04-30"
success_class: "仮説的中"
---

# Review

## Outcome
## Hypothesis check
## Process check
## Lessons
## Next actions
""",
        encoding="utf-8",
    )
    draft = build_monthly_retro(tmp_path, "2026-04")
    front = yaml.safe_load(draft.content.split("---", 2)[1])
    assert front["closed_positions"] == 1
    assert front["success_class_counts"]["仮説的中"] == 1


def test_diff_jsonl_marks_orphan_existing_records(tmp_path: Path) -> None:
    register_path = tmp_path / "records/_ledger" / "research-decisions" / "2026-04.jsonl"
    register_path.parent.mkdir(parents=True)
    register_path.write_text(
        json.dumps({"decision_event_id": "decision-20260425-2767-research"}) + "\n",
        encoding="utf-8",
    )
    assert diff_jsonl(register_path, []) == ["! decision-20260425-2767-research"]


def test_ledger_sync_rewrites_register_from_sources(tmp_path: Path) -> None:
    register_path = tmp_path / "records/_ledger" / "research-decisions" / "2026-04.jsonl"
    register_path.parent.mkdir(parents=True)
    register_path.write_text(
        json.dumps(
            {
                "decision_event_id": "decision-20260425-2767-research",
                "ticker": "2767",
                "stale_field": {"legacy_id": "legacy-20260425-2767"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    incoming = {
        "decision_event_id": "decision-20260425-2767-research",
        "ticker": "2767",
        "trade_execution_state": "none",
    }

    assert diff_jsonl(register_path, [incoming]) == ["~ decision-20260425-2767-research"]
    assert write_jsonl(register_path, [incoming]) == 1

    [record] = read_jsonl(register_path)
    assert record == incoming
