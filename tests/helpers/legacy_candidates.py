"""旧候補replayと保存run testの共有合成入力。現行候補政策を代行しない。"""

from __future__ import annotations

import json
from pathlib import Path

from tests.helpers.screening_run import saved_candidate, seed_saved_run

from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.discovery.candidates import (
    build_candidate_contexts,
)
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.discovery.contracts import (
    CandidateDiscoveryResult,
)
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.rule_config import (
    load_screening_rules,
)
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.rules_identity import (
    production_rules_contract_hash,
)

SCREENING_RULES = load_screening_rules()
RULES = SCREENING_RULES.candidate_discovery
REQUIRED_JPX_FLAGS = SCREENING_RULES.universe.required_jpx_flags
RULES_HASH = production_rules_contract_hash(SCREENING_RULES.model_dump_json())
GOLDEN = json.loads(Path("tests/fixtures/screening/candidate-discovery-golden.json").read_text())


def _discover(rows: list[dict[str, object]]) -> CandidateDiscoveryResult:
    return build_candidate_contexts(rows, rules=RULES, required_jpx_flags=REQUIRED_JPX_FLAGS)


def _analysis(
    ticker: str,
    *,
    per: float = 10.0,
    normalized_per: float = 10.0,
    asset_ratio: float = 0.5,
    p_s: float = 1.0,
) -> dict[str, object]:
    return {
        "ticker": ticker,
        "name": ticker,
        "sector_33": "情報・通信業",
        "market_cap_oku": 500,
        "avg_turnover_oku": 5.0,
        "price_history_span_days": 1000,
        "jpx_flags": [],
        "per_forward": per,
        "per_trailing": per + 1,
        "pbr": 0.8,
        "p_s": p_s,
        "ev_ebitda": 5.0,
        "pcfr": 8.0,
        "metrics": {
            "per_forward_sector_gap": per / 20.0 - 1.0,
            "normalized_per_3fy": normalized_per,
            "fcf_yield": 0.08,
            "ocf_yield": 0.10,
            "asset_backed_ratio": asset_ratio,
            "net_cash_to_market_cap": 0.25,
            "pbr_sector_gap": -0.3,
            "equity_ratio": 0.6,
            "p_s_sector_gap": -0.4,
            "sales_yoy": 0.05,
            "operating_profit_ttm": 12.0,
            "sales_ttm": 100.0,
            "total_assets": 200.0,
            "same_state_equity_yen": 120.0,
            "debt": 20.0,
            "cash": 30.0,
            "er_annual": 0.1,
        },
    }


def _run(*, rows=None, asof="2026-10-14", at=None):
    return {
        "schema_version": 1,
        "kind": "stock_analysis_run",
        "run_id": f"screening-{asof.replace('-', '')}",
        "run_date": asof,
        "asof_date": asof,
        "run_at": at or f"{asof}T18:00:00+09:00",
        "universe_size": len(rows) if rows is not None else 1,
        "screening_rules_hash": RULES_HASH,
        "er_model_version": "expected-return-v1",
        "stock_analyses": [{**_analysis("1111"), "candidate": saved_candidate(rank=1)}]
        if rows is None
        else rows,
    }


def _publish(path, payload=None, *, revision=None):
    payload = _run() if payload is None else payload
    identity = revision or f"saved-{payload['asof_date']}-{payload['run_at']}"
    return seed_saved_run(path, payload, run_revision_id=identity)
