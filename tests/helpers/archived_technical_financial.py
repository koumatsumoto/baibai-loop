"""合成の旧公表は順序を与えて組む。現在の市場から過去の注意順を再計算しない。"""

from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialPolicy,
    TechnicalFinancialRow,
    TechnicalFinancialSelection,
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


def stored_row(
    ticker, *, lane, technical=None, gate=None, paths=None, name=None, sector=None, related=None
):
    """合成の保存行。欠けた根拠をモデルや政策から復元しない。"""
    return TechnicalFinancialRow(
        ticker=ticker,
        name=name,
        sector=sector,
        technical={"state": "unavailable", "matched_routes": [], "routes": {}, **(technical or {})},
        financial_gate={
            "state": "input_unresolved",
            "matched_routes": [],
            "routes": {},
            "checks": {},
            **(gate or {}),
        },
        financial_paths={"matched_routes": [], "paths": [], **(paths or {})},
        screening_lane=lane,
        related_subject_context=related,
    )


def stored_publication(generation_id, rows, *, order, page_size=2, price_inputs=None):
    """当時受け取ったという明示的な入力。株別モデル評価は行わない。"""
    return ScreeningSelection(
        generation_id,
        tuple(r.ticker for r in rows),
        stored_result(rows, order=order, policy=TechnicalFinancialPolicy(page_size=page_size)),
        {} if price_inputs is None else price_inputs,
    )
