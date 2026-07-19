from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.macro.indicators.db import initialize_database as initialize_indicators_db
from baibai_engine.position.ledger import load_portfolio_ledger
from baibai_engine.research.store import ResearchStoreService
from baibai_engine.screening.run_store import ScreeningRunStore
from baibai_engine.tasks.models import Task
from tests.helpers.db_seed import seed_ledger, seed_tasks

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def app_records_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    config_dir = root / "records/_config"
    config_dir.mkdir(parents=True)
    (config_dir / "macro-dashboard.yaml").write_text(
        Path("records/_config/macro-dashboard.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    indicators_dir = root / "data/indicators"
    indicators_dir.mkdir(parents=True, exist_ok=True)
    initialize_indicators_db(indicators_dir / "macro.sqlite").close()
    db_path = root / "data/app/baibai.sqlite"
    ledger = load_portfolio_ledger(FIXTURES / "portfolio-ledger/representative.yaml")
    seed_ledger(
        db_path,
        ledger.model_copy(
            update={
                "market_prices": tuple(
                    price.model_copy(update={"source_kind": "licensed_dataset"})
                    for price in ledger.market_prices
                )
            }
        ),
    )
    packet = safe_load(
        (FIXTURES / "decision-packet/2331-decision.yaml").read_text(encoding="utf-8")
    )
    review = safe_load(
        (FIXTURES / "decision-packet/2331-decision-review.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(packet, dict)
    assert isinstance(review, dict)
    ResearchStoreService(db_path).publish_packet_with_review(
        "packet-20260714-2331-r1", packet, review
    )
    tasks = yaml.safe_load(_TASKS)["tasks"]
    seed_tasks(db_path, (Task.model_validate(item) for item in tasks))
    run_store = ScreeningRunStore(root / "data/screening/runs.sqlite")
    for text in (
        _CANDIDATES.replace("screening-20260708", "screening-20260701").replace(
            "2026-07-08", "2026-07-01"
        ),
        _CANDIDATES,
    ):
        payload = yaml.safe_load(text)
        assert isinstance(payload, dict)
        run_store.publish_run(payload)
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
