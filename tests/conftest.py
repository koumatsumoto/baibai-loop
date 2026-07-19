from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from baibai_engine.screening.run_store import import_screening_runs
from baibai_engine.tasks.importer import import_task_file

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def app_records_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    ledger_dir = root / "records/04-position"
    thesis_dir = root / "records/03-thesis/2026/07"
    task_dir = root / "records/05-task"
    candidates_dir = root / "records/02-candidates/2026/07"
    config_dir = root / "records/_config"
    for path in (ledger_dir, thesis_dir, task_dir, candidates_dir, config_dir):
        path.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path("records/_config/macro-dashboard.yaml"), config_dir)
    indicators_dir = root / "data/indicators"
    indicators_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path("data/indicators/macro.sqlite"), indicators_dir)
    shutil.copy2(
        FIXTURES / "portfolio-ledger/representative.yaml",
        ledger_dir / "portfolio-ledger.yaml",
    )
    ledger_path = ledger_dir / "portfolio-ledger.yaml"
    ledger_path.write_text(
        ledger_path.read_text(encoding="utf-8").replace(
            "source_kind: test_fixture", "source_kind: licensed_dataset"
        ),
        encoding="utf-8",
    )
    shutil.copy2(
        FIXTURES / "decision-packet/2331-decision.yaml",
        thesis_dir / "2026-07-14-2331-decision.yaml",
    )
    shutil.copy2(
        FIXTURES / "decision-packet/2331-decision-review.yaml",
        thesis_dir / "2026-07-14-2331-decision-review.yaml",
    )
    (task_dir / "tasks.yaml").write_text(_TASKS, encoding="utf-8")
    import_task_file(task_dir / "tasks.yaml", db_path=root / "data/app/baibai.sqlite")
    (candidates_dir / "2026-07-01.yaml").write_text(
        _CANDIDATES.replace("screening-20260708", "screening-20260701").replace(
            "2026-07-08", "2026-07-01"
        ),
        encoding="utf-8",
    )
    (candidates_dir / "2026-07-08.yaml").write_text(_CANDIDATES, encoding="utf-8")
    import_screening_runs(
        root / "records/02-candidates",
        db_path=root / "data/screening/runs.sqlite",
    )
    return root


_TASKS = """\
schema_version: 1
tasks:
  - task_id: task-20260730-2331-review
    title: 2331 review
    kind: earnings-review
    status: open
    ticker: "2331"
    due_date: "2026-07-30"
    event_label: Q1 earnings
    event_date: "2026-07-30"
    related_refs: []
    created_at: "2026-07-18"
  - task_id: task-20260801-ops
    title: ops check
    kind: ops
    status: open
    due_date: "2026-08-01"
    related_refs: []
    created_at: "2026-07-18"
  - task_id: task-20260701-done
    title: completed
    kind: other
    status: done
    due_date: "2026-07-01"
    related_refs: []
    created_at: "2026-06-01"
    closed_at: "2026-07-01"
"""

_CANDIDATES = """\
run_id: screening-20260708
run_date: "2026-07-08"
asof_date: "2026-07-08"
universe_size: 3
run_at: "2026-07-08T12:00:00+09:00"
candidates:
  - ticker: "2331"
    name: ALSOK
    sector_33: サービス業
    per_trailing: 12.0
    metrics:
      er_annual: 0.12
    evidence_hits: []
  - ticker: "0001"
    name: Sample One
    sector_33: 情報・通信業
    metrics: {}
    evidence_hits: []
  - ticker: "0002"
    name: Sample Two
    sector_33: 小売業
    evidence_hits: []
"""
