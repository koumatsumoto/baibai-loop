"""最終比較へ固定市場判断を渡し、後発判断や現在の確認済みに置き換えない。"""

from datetime import timedelta

import pytest
from tests.helpers.assessment_v1 import assessment_id, pair_payload
from tests.helpers.db_seed import seed_ledger
from tests.helpers.generated_selection import generated_draft_for, seed_generated_selection
from tests.helpers.proposal import PREPARED, PROPOSAL_ID
from tests.helpers.screening_sqlite import seed_daily_bars

from baibai_engine.investment_judgment.market_assessment.models import MarketContextAssessment
from baibai_engine.investment_judgment.market_assessment.projection import assessment_projection
from baibai_engine.investment_judgment.market_assessment.service import AssessmentService
from baibai_engine.investment_judgment.stock_assessment.store import AssessmentStoreService
from baibai_engine.investment_judgment.trade_proposal.inputs import build_proposal_context
from baibai_engine.investment_judgment.trade_proposal.scaffold import scaffold_proposal
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.selection import (
    saved_market_assessment,
    selection_sha256,
)
from baibai_engine.investment_judgment.workflow.workspace import prepare_selected_workspace
from baibai_engine.portfolio.ledger.events import PortfolioLedgerDocument

START = PREPARED - timedelta(minutes=3)


def _market_document(*, suffix="fixed", published_at=None):
    return MarketContextAssessment.model_validate(
        {
            "context_id": f"market-context-assessment-2026-10-14-{suffix}",
            "as_of": START.date(),
            "known_at": START - timedelta(minutes=10),
            "published_at": published_at or START - timedelta(minutes=5),
            "summary": "合成：金利高止まり。価格下落だけで価値の増加を認定しない。",
            "inputs": [
                {
                    "input_id": "rates",
                    "reference": "https://example.invalid/synthetic-rates",
                    "layer": "primary_source",
                    "observation_as_of": START.date() - timedelta(days=1),
                    "available_at": START - timedelta(minutes=12),
                }
            ],
            "changes": [{"summary": "金利水準は高いが直近変化は小さい。", "source_ids": ["rates"]}],
            "counter_evidence": [
                {"summary": "固定金利なら短期の影響は小さい。", "source_ids": ["rates"]}
            ],
            "uncertainties": [{"summary": "個社の変動負債・借換期は未確認。"}],
            "implications": [
                {"summary": "借換費用とcash維持・待機案を比較する。", "source_ids": ["rates"]}
            ],
        }
    )


def _prepare(tmp_path, *, market_document=None):
    app, market, runs = (tmp_path / name for name in ("app.sqlite", "market.sqlite", "runs.sqlite"))
    seed_ledger(
        app,
        PortfolioLedgerDocument.model_validate(
            {
                "schema_version": 3,
                "portfolio_scope": "repository_only",
                "as_of": START,
                "events": [
                    {
                        "type": "opening_balance",
                        "event_id": "synthetic-opening",
                        "occurred_at": START,
                        "amount_yen": 300000,
                    }
                ],
            }
        ),
    )
    if market_document is not None:
        AssessmentService(app).publish(market_document, expected_head=None)
    selected = seed_generated_selection(runs=runs, now=START, tickers=("1234",), db=app)
    prepared = prepare_selected_workspace(
        draft=generated_draft_for(selected),
        db_path=app,
        runs_path=runs,
        workspace=tmp_path / "workspace",
        now=START,
    )
    document, review = pair_payload()
    pair = AssessmentStoreService(app, clock=lambda: PREPARED).publish_reviewed_assessment(
        assessment_id(), document, review
    )
    operations = OperationService(app)
    operation = operations.record_assessment(
        prepared.operation_id, pair.assessment_id, now=PREPARED
    )
    seed_daily_bars(market, [("1234", START.date().isoformat(), 1100.0, 1.0)])
    return app, market, runs, operation


def _context(app, market, operation):
    return build_proposal_context(
        proposal_id=PROPOSAL_ID,
        operation_id=operation.operation_id,
        db_path=app,
        sqlite_path=market,
        now=PREPARED,
    )


def test_final_context_keeps_frozen_market_evidence_and_ignores_later_head(tmp_path, monkeypatch):
    document = _market_document()
    app, market, runs, operation = _prepare(tmp_path, market_document=document)
    selected = selection_binding(operation.payload)
    expected = saved_market_assessment(selected)
    original_selection_hash = selection_sha256(selected)
    before = app.read_bytes()
    context = _context(app, market, operation)
    payload = context.payload()
    assert payload["saved_market_context_assessment"] == expected
    assert payload["market_context_assessment_id"] == document.context_id
    assert expected["sources"] == document.payload()["inputs"]
    assert expected["known_at"] != expected["published_at"]
    for field in ("changes", "counter_evidence", "uncertainties", "implications"):
        assert expected[field] == document.payload()[field]
    assert app.read_bytes() == before

    later = _market_document(suffix="later", published_at=PREPARED)
    AssessmentService(app).publish(later, expected_head=document.context_id)
    runs.rename(tmp_path / "retained-runs.sqlite")
    monkeypatch.setattr(
        "baibai_engine.investment_judgment.market_assessment.query.latest_assessment",
        lambda *_a, **_kw: pytest.fail("proposal reselected the live market judgment"),
    )
    before = app.read_bytes()
    assert _context(app, market, operation).payload()["saved_market_context_assessment"] == expected
    assert selection_sha256(selected) == original_selection_hash
    assert app.read_bytes() == before
    payload["saved_market_context_assessment"]["uncertainties"].clear()
    assert context.payload()["saved_market_context_assessment"] == expected


@pytest.mark.parametrize("future", [False, True])
def test_absent_at_selection_is_not_backfilled_for_final_comparison(tmp_path, future):
    later = _market_document(suffix="later", published_at=PREPARED)
    app, market, _runs, operation = _prepare(tmp_path, market_document=later if future else None)
    if not future:
        AssessmentService(app).publish(later, expected_head=None)
    before = app.read_bytes()
    payload = _context(app, market, operation).payload()
    assert payload["saved_market_context_assessment"] is None
    assert payload["market_context_assessment_id"] is None
    assert app.read_bytes() == before


def test_legacy_projection_keeps_unrecorded_cutoff_and_draft_unchanged(tmp_path, monkeypatch):
    def historical_projection(record, *, as_of):
        value = assessment_projection(record, as_of=as_of)
        value.pop("known_at", None)
        return value

    with monkeypatch.context() as legacy:
        legacy.setattr(
            "baibai_engine.investment_judgment.workflow.operation.admission.assessment_projection",
            historical_projection,
        )
        app, market, _runs, operation = _prepare(tmp_path, market_document=_market_document())
    expected = saved_market_assessment(selection_binding(operation.payload))
    before = app.read_bytes()
    context = _context(app, market, operation)
    assert context.payload()["saved_market_context_assessment"] == expected
    assert "known_at" not in context.payload()["saved_market_context_assessment"]
    draft = scaffold_proposal(context)
    assert "saved_market_context_assessment" not in draft
    assert draft["market_context_assessment_id"] == expected["context_id"]
    assert draft["items"][0]["action"] is None
    assert draft["review"] is None
    assert app.read_bytes() == before
