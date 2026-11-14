"""保存Generationの研究診断を、候補政策ownerで再生する入口。"""

from __future__ import annotations

from pydantic import JsonValue

from baibai_engine.machine_analysis.generation.contracts import GeneratedModel, GenerationMetadata
from baibai_engine.machine_analysis.screening.policies import financial_growth

POLICY_ID = financial_growth.POLICY_ID


def evaluate_financial_growth_gate(
    ticker: str, *, models: tuple[GeneratedModel, ...], metadata: GenerationMetadata
) -> dict[str, JsonValue]:
    """Keep the original research output; production adoption belongs to its caller."""
    return {
        "policy_id": POLICY_ID,
        "adoption_status": "unadopted_research_only",
        **financial_growth.evaluate_financial_growth_gate(ticker, models=models, metadata=metadata),
    }
