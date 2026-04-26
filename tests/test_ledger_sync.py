from __future__ import annotations

import json
import textwrap
from datetime import date
from pathlib import Path

import yaml

from baibai_loop.ledger.cli import _discover_decision_dates, _load_market_data
from baibai_loop.ledger.sync import sync_ledger
from baibai_loop.screening.providers.jquants import JQuantsDailyBar


def _seed(root: Path) -> None:
    screened_dir = root / "screened" / "2026" / "04"
    research_dir = root / "research" / "2026" / "04"
    playbooks_dir = root / "playbooks"
    screened_dir.mkdir(parents=True)
    research_dir.mkdir(parents=True)
    playbooks_dir.mkdir()
    (screened_dir / "2026-04-24.yaml").write_text(
        yaml.safe_dump(
            {
                "asof_date": "2026-04-24",
                "tickers": [
                    {
                        "ticker": "2767",
                        "name": "Sample",
                        "market_cap_oku": 936,
                        "avg_turnover_oku": 4.9,
                        "threshold_hit": ["a", "b"],
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
            "playbook": "valuation-mean-reversion-v1",
            "decision": "accepted",
            "screened_ref": "screened/2026/04/2026-04-24.yaml",
            "published_at": "2026-04-25T22:00:00+09:00",
            "macro_gate": "neutral",
            "position_size_oku": 0.01,
        },
        allow_unicode=True,
        sort_keys=False,
    )
    body = textwrap.dedent(
        """
        # Research

        | 最新 adj close (2026-04-24) | 1,431 円 | — |
        """
    )
    (research_dir / "2026-04-25-2767-valuation-mean-reversion-v1.md").write_text(
        f"---\n{front}---\n{body}",
        encoding="utf-8",
    )


def test_sync_ledger_writes_idempotent_paper_records(tmp_path: Path) -> None:
    _seed(tmp_path)
    first = sync_ledger(tmp_path)
    second = sync_ledger(tmp_path)
    assert first.paper_count == 1
    assert second.paper_count == 1
    ledger_path = tmp_path / "ledger" / "paper" / "2026-04.jsonl"
    lines = ledger_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["ledger_id"] == "paper-20260425-2767-vmean"
    assert record["tracking"] == {"plus_15bd": None, "plus_30bd": None}
    assert record["adjustment_applied"] is False


def test_sync_ledger_dry_run_reports_existing_packets(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = sync_ledger(tmp_path, dry_run=True)
    assert result.paper_count == 1
    assert result.diff_lines == ("+ paper-20260425-2767-vmean",)


def test_sync_ledger_uses_adjusted_price_when_bar_available(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = sync_ledger(
        tmp_path,
        bars=(
            JQuantsDailyBar(
                ticker="2767",
                traded_at=date(2026, 4, 25),
                close=100.0,
                adjustment_close=90.0,
                turnover_value=None,
            ),
        ),
    )
    assert result.paper_count == 1
    record = json.loads((tmp_path / "ledger" / "paper" / "2026-04.jsonl").read_text().strip())
    assert record["baseline_price"] == 90.0
    assert record["adjustment_applied"] is True


def test_sync_ledger_adds_select_candidates_without_research_to_skipped(tmp_path: Path) -> None:
    _seed(tmp_path)
    select_dir = tmp_path / "select"
    select_dir.mkdir()
    (select_dir / "2026-04-25.yaml").write_text(
        yaml.safe_dump(
            {
                "screened_ref": "screened/2026/04/2026-04-24.yaml",
                "candidates": [
                    {
                        "ticker": "9999",
                        "name": "Skipped Co",
                        "playbook": "valuation-mean-reversion-v1",
                    }
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    result = sync_ledger(tmp_path)
    assert result.skipped_count == 1
    record = json.loads((tmp_path / "ledger" / "skipped" / "2026-04.jsonl").read_text().strip())
    assert record["ledger_id"] == "skipped-20260425-9999-vmean"
    assert record["research_ref"] is None


def test_ledger_cli_warns_when_jquants_token_is_missing(tmp_path: Path) -> None:
    _seed(tmp_path)
    calendar, bars, warnings = _load_market_data(tmp_path, {})
    assert calendar == ()
    assert bars == ()
    assert warnings == ("JQUANTS_REFRESH_TOKEN is unset; tracking prices remain null",)


def test_ledger_cli_discovers_research_decision_dates(tmp_path: Path) -> None:
    _seed(tmp_path)
    assert _discover_decision_dates(tmp_path) == (date(2026, 4, 25),)
