"""Application publishers must admit the SQLite file opened for the write."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import ModuleType

import pytest
from tests.helpers.assessment_v1 import assessment_id, pair_payload
from tests.helpers.generated_selection import seed_generated_selection
from tests.helpers.outcome_publication import _publication

from baibai_engine.investment_judgment.company_research import store as research
from baibai_engine.investment_judgment.company_research.document import ResearchDraft
from baibai_engine.investment_judgment.market_assessment import service as market
from baibai_engine.investment_judgment.market_assessment.models import MarketContextAssessment
from baibai_engine.investment_judgment.stock_assessment import store as stock
from baibai_engine.investment_judgment.workflow.operation import service as operation
from baibai_engine.investment_judgment.workflow.operation.models import OperationPayload
from baibai_engine.portfolio.performance import store as portfolio
from baibai_engine.storage.sqlite.application.schema import APPLICATION_SCHEMA_VERSION
from baibai_engine.storage.sqlite.application.write import initialize_database

NOW = datetime.fromisoformat("2026-10-14T18:00:00+09:00")


@pytest.mark.parametrize(
    "publisher",
    [research, market, stock, operation, portfolio],
    ids=["company-research", "market-assessment", "stock-assessment", "operation", "outcome"],
)
def test_app_publication_rejects_obsolete_replacement_after_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, publisher: ModuleType
) -> None:
    database = tmp_path / "app.sqlite"
    replacement = tmp_path / "previous.sqlite"
    initialize_database(replacement)
    # Match table shape so the ordinary INSERT would succeed if admission were skipped.
    with closing(sqlite3.connect(replacement)) as connection, connection:
        connection.execute(f"PRAGMA user_version={APPLICATION_SCHEMA_VERSION - 1}")
    before = replacement.read_bytes()

    def initialize_then_replace(path: Path, **options: bool) -> int:
        version = initialize_database(path, **options)
        assert path == database
        assert version == APPLICATION_SCHEMA_VERSION
        replacement.replace(database)
        return version

    def publish() -> None:
        if publisher == research:
            draft = ResearchDraft.model_validate(
                {
                    "research_id": "app-admission-test",
                    "document": {
                        "ticker": "1234",
                        "as_of": NOW.date(),
                        "investigated_scope": "fixture search",
                        "unresolved_questions": ["fixture question"],
                    },
                }
            )
            research.ResearchStoreService(database, clock=lambda: NOW).save(draft)
        elif publisher == market:
            assessment = MarketContextAssessment.model_validate(
                {
                    "context_id": "market-context-assessment-2026-10-14-app-admission",
                    "as_of": NOW.date(),
                    "known_at": NOW.isoformat(),
                    "published_at": NOW.isoformat(),
                    "summary": "fixture assessment",
                    "inputs": [],
                }
            )
            market.AssessmentService(database, clock=lambda: NOW).publish(
                assessment, expected_head=None
            )
        elif publisher == stock:
            assessment_payload, review = pair_payload()
            stock.AssessmentStoreService(database, clock=lambda: NOW).publish_reviewed_assessment(
                assessment_id(), assessment_payload, review
            )
        elif publisher == operation:
            runs = tmp_path / "runs.sqlite"
            selected = seed_generated_selection(runs=runs, now=NOW, tickers=("1234",))
            operation.OperationService(database, runs_path=runs).start(
                session_kind="capital-allocation",
                as_of=NOW.date(),
                started_at=NOW,
                payload=OperationPayload(
                    checkpoint="start", artifacts=(selected.model_dump(mode="json"),)
                ),
            )
        else:
            portfolio.PortfolioOutcomeStore(database).publish(_publication())

    monkeypatch.setattr(publisher, "initialize_database", initialize_then_replace)
    with pytest.raises(
        RuntimeError, match="application append requires the current existing schema"
    ):
        publish()
    assert not replacement.exists()
    assert database.read_bytes() == before
