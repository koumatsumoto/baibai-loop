from __future__ import annotations

from datetime import date
from pathlib import Path

from tools.measure_deployment_opportunity import build_measurement

from baibai_engine.position.ledger import PortfolioLedgerDocument


def _ledger() -> PortfolioLedgerDocument:
    return PortfolioLedgerDocument.model_validate(
        {
            "schema_version": 2,
            "portfolio_scope": "repository_only",
            "as_of": "2026-08-02T12:00:00+09:00",
            "events": [
                {
                    "type": "opening_balance",
                    "event_id": "opening",
                    "occurred_at": "2026-05-01T08:00:00+09:00",
                    "amount_yen": 200_000,
                },
                {
                    "type": "reservation",
                    "event_id": "reserve-1111",
                    "occurred_at": "2026-05-02T08:00:00+09:00",
                    "reservation_id": "reservation-1111",
                    "order_id": "order-1111",
                    "ticker": "1111",
                    "sector": "サービス業",
                    "quantity": 100,
                    "price_guard_yen": "1000",
                    "expires_at": "2026-05-03T15:30:00+09:00",
                },
                {
                    "type": "execution",
                    "event_id": "buy-1111",
                    "occurred_at": "2026-05-02T09:00:00+09:00",
                    "execution_id": "execution-1111",
                    "reservation_id": "reservation-1111",
                    "ticker": "1111",
                    "side": "buy",
                    "quantity": 100,
                    "price_yen": "1000",
                },
            ],
            "market_prices": [],
        }
    )


def test_unknown_cycle_is_counted_instead_of_silently_skipped(tmp_path: Path) -> None:
    operation = {
        "operation_id": "op-20260701-opportunity-1",
        "session_kind": "opportunity",
        "status": "completed",
        "as_of": "2026-07-01",
        "started_at": "2026-07-01T10:00:00+09:00",
        "completed_at": "2026-07-01T11:00:00+09:00",
        "payload": {
            "result": "human wording without a canonical disposition",
            "canonical_refs": [],
        },
    }

    result = build_measurement(
        operations=[operation],
        shortlists=[],
        assessments=[],
        ledger=_ledger(),
        runs_db_path=None,
        market_db_path=tmp_path / "unused.sqlite",
        start=date(2026, 5, 1),
        horizons=["3m"],
    )

    assert result["coverage"]["opportunity_cycle_count"] == 1
    assert result["coverage"]["unknown_conclusion_count"] == 1
    assert result["coverage"]["counterfactual_unavailable_count"] == 1
    assert len(result["cycles"]) == 1
    cycle = result["cycles"][0]
    assert cycle["conclusion"] == "unknown"
    assert cycle["counterfactual"] == {"status": "shortlist_binding_missing"}
    assert cycle["capital"]["available_cash_yen"] == 100_000
    assert cycle["capital"]["deployed_cost_yen"] == 100_000
    assert cycle["capital"]["deployment_ratio_pct"] == 50.0


def test_bargain_assessment_is_the_cycle_conclusion_authority(tmp_path: Path) -> None:
    operation = {
        "operation_id": "op-20260701-opportunity-1",
        "session_kind": "opportunity",
        "status": "completed",
        "as_of": "2026-07-01",
        "started_at": "2026-07-01T10:00:00+09:00",
        "completed_at": "2026-07-01T11:00:00+09:00",
        "payload": {
            "result": "free prose says something else",
            "canonical_refs": ["bargain_assessment: assessment-1"],
        },
    }
    assessment = {
        "assessment_id": "assessment-1",
        "result": "defer",
        "shortlist_id": "missing-shortlist",
    }

    result = build_measurement(
        operations=[operation],
        shortlists=[],
        assessments=[assessment],
        ledger=_ledger(),
        runs_db_path=None,
        market_db_path=tmp_path / "unused.sqlite",
        start=date(2026, 5, 1),
        horizons=["3m"],
    )

    cycle = result["cycles"][0]
    assert cycle["conclusion"] == "defer"
    assert cycle["conclusion_source"] == "bargain_assessment"
