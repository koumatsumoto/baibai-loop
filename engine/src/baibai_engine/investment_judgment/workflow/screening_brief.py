"""提案では、選定時に記録した対象・対照銘柄の探索根拠を読む。"""

from __future__ import annotations

from copy import deepcopy
from typing import cast

from pydantic import JsonValue, TypeAdapter

from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedResearchSelectionV5,
    GeneratedSelection,
)
from baibai_engine.investment_judgment.workflow.selection import (
    ResearchSelectionRecord as ResearchSelection,
)
from baibai_engine.machine_analysis.screening.discovery_contract import (
    DiscoverySelection,
    DiscoverySelectionV2,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialRow,
    TechnicalFinancialSelection,
)


def saved_discovery_context(
    selection: ResearchSelection, *, selection_hash: str
) -> dict[str, JsonValue] | None:
    if not isinstance(selection, GeneratedSelection):
        return None
    saved = selection.inputs.screening_selection
    if saved is None or not isinstance(
        saved.result, (TechnicalFinancialSelection, DiscoverySelection, DiscoverySelectionV2)
    ):
        return None
    result = saved.result
    used = set(selection.evidence_subjects())
    policies: dict[str, JsonValue] = {}
    if isinstance(result, (DiscoverySelection, DiscoverySelectionV2)):
        if isinstance(result, DiscoverySelectionV2):
            policies["price_session"] = result.price_session.model_dump(mode="json")
        rows = {
            r["ticker"]: cast(JsonValue, deepcopy(r)) for r in result.rows if r["ticker"] in used
        }
    else:
        codec = TypeAdapter(TechnicalFinancialRow)
        rows = {
            r.ticker: codec.dump_python(r, mode="json") for r in result.rows if r.ticker in used
        }
        policies = {
            "technical_policy_id": result.technical_policy_id,
            "financial_policy_id": result.financial_policy_id,
        }
    # 選定時の支持・不利・欠測を照合する。全社展開や現在政策での再判定はしない。
    return {
        "role": "selection_time_discovery_evidence_not_final_judgment",
        "selection_sha256": selection_hash,
        "screening_selection_id": selection.inputs.screening_selection_id,
        "generation_id": saved.generation_id,
        "generation_as_of": selection.inputs.generation.as_of.isoformat(),
        "published_at": selection.inputs.screening_published_at.isoformat()
        if selection.inputs.screening_published_at is not None
        else None,
        "research_as_of": selection.research_as_of.isoformat(),
        "input_known_at": selection.input_known_at.isoformat(),
        "policy": result.policy.model_dump(mode="json"),
        **policies,
        "rows": [rows[target.ticker] for target in selection.targets if target.ticker in rows],
        "not_in_saved_rows": [
            target.ticker for target in selection.targets if target.ticker not in rows
        ],
        **(
            {
                "source_sha256": selection.inputs.screening_selection.source_sha256,
                "detail_scope": "finite_case_not_full_screening_publication",
                "comparison_rows": [rows[t] for t in selection.comparison_tickers if t in rows],
                "comparisons_not_in_saved_rows": [
                    t for t in selection.comparison_tickers if t not in rows
                ],
            }
            if isinstance(selection, GeneratedResearchSelectionV5)
            and selection.inputs.screening_selection is not None
            else {"full_result_ref": "/inputs/screening_selection/result"}
        ),
    }
