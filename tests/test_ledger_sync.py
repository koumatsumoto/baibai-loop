from __future__ import annotations

import json
import os
import textwrap
from datetime import date
from pathlib import Path

import pytest
import yaml

from baibai_loop.ledger.cli import _discover_decision_dates, _load_market_data, main
from baibai_loop.position.io import diff_jsonl, read_jsonl, write_jsonl
from baibai_loop.position.sync import sync_ledger


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
                        "decision_event_id": "decision-20260425-2767-research",
                        "ticker": "2767",
                        "name": "Sample",
                        "market_cap_oku": 936,
                        "avg_turnover_oku": 4.9,
                        "last_price": 1431.0,
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
            "playbook_ref": {
                "ref_path": "records/_playbooks/valuation-reversion/2026-05-01T000000+0900.md",
            },
            "research_decision": {"outcome": "approved", "posture": "act_now"},
            "candidate_ref": {
                "candidates_ref": str(candidates_path.relative_to(root)),
                "ticker": "2767",
            },
            "published_at": "2026-04-25T22:00:00+09:00",
            "recorded_at": "2026-04-25T22:00:00+09:00",
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
            "trade_execution_state": "submitted",
            "order_intent": {
                "order_intent_id": "intent-20260425-2767-buy",
                "decision_event_id": "decision-20260425-2767-trade",
                "side": "buy",
                "quantity": 100,
                "order_price_guard_yen": 1500,
                "uses_margin": False,
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
    assert "candidate_decision" not in record
    assert record["baseline_price"] == 1431.0
    assert record["market_cap_oku"] == 936.0
    assert record["avg_turnover_oku"] == 4.9
    assert record["tracking"] == {
        "mode": "post_approval",
        "plus_15bd": None,
        "plus_30bd": None,
    }


def test_sync_ledger_includes_trade_execution_from_current_contract(tmp_path: Path) -> None:
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


def test_ledger_cli_warns_when_jquants_token_is_missing(tmp_path: Path) -> None:
    _seed(tmp_path)
    calendar, bars, warnings = _load_market_data(tmp_path, {})
    assert calendar == ()
    assert bars == ()
    assert warnings == (
        "JQUANTS_API_KEY is unset and SQLite has no bars",
        "no J-Quants bars were loaded; tracking prices stay unfilled",
    )


def test_ledger_cli_discovers_research_decision_dates(tmp_path: Path) -> None:
    _seed(tmp_path)
    assert _discover_decision_dates(tmp_path) == (date(2026, 4, 25),)


def test_ledger_cli_require_market_data_fails_without_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("JQUANTS_API_KEY", raising=False)
    assert main(["sync", "--root", str(tmp_path), "--dry-run", "--require-market-data"]) == 1


def test_ledger_cli_loads_dotenv_from_root_not_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _seed(repo_root)
    (repo_root / ".env").write_text("JQUANTS_API_KEY=from_root_dotenv\n", encoding="utf-8")

    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()
    monkeypatch.chdir(other_dir)
    monkeypatch.delenv("JQUANTS_API_KEY", raising=False)

    main(["sync", "--root", str(repo_root), "--dry-run", "--require-market-data"])
    assert os.environ["JQUANTS_API_KEY"] == "from_root_dotenv"


def test_ledger_cli_require_market_data_emits_diagnostic_when_no_research(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JQUANTS_API_KEY", "dummy-token")
    (tmp_path / "records/05-research").mkdir(parents=True)
    assert main(["sync", "--root", str(tmp_path), "--dry-run", "--require-market-data"]) == 1
    assert "no market data" in capsys.readouterr().err


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
                "stale_field": {"old_id": "old-20260425-2767"},
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
