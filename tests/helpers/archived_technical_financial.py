"""合成の旧公表は順序を与えて組む。現在の市場から過去の注意順を再計算しない。"""

from baibai_engine.machine_analysis.generation.contracts import GenerationMetadata
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialPolicy,
    TechnicalFinancialSelection,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial_selection import (
    project_technical_financial_row,
)

# 旧発行形式を読むための二つの固定例。入力の金額・評価値から順番は作らない。
SIX_STOCK_ORDER = ("4567", "5678", "6789", "3456", "1234")
FOUR_STOCK_ORDER = ("3456", "2345", "1234")


def stored_result(rows, *, order, policy=None):
    return TechnicalFinancialSelection(
        policy or TechnicalFinancialPolicy(),
        "technical-discovery-v1",
        "financial-growth-research-trial-v1",
        tuple(rows),
        order,
        {"planned_stocks": len(rows)},
    )


def archived_from_models(fixed, *, order, page_size=2):
    metadata = GenerationMetadata.model_validate(fixed.model_dump(exclude={"models"}))
    tickers = tuple(sorted(t for t, plan in metadata.subject_plan.items() if plan.scope == "stock"))
    rows = tuple(
        project_technical_financial_row(
            t, models=tuple(m for m in fixed.models if m.subject == t), metadata=metadata
        )
        for t in tickers
    )
    return ScreeningSelection(
        metadata.generation_id,
        tickers,
        stored_result(rows, order=order, policy=TechnicalFinancialPolicy(page_size=page_size)),
        {},
    )
