"""保存済み評価から、原典時刻と反証を保持して今回の差分 draft を作る。"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from baibai_engine.investment_judgment.contracts import (
    AssessmentError,
)
from baibai_engine.primitives.time import JST

if TYPE_CHECKING:
    from baibai_engine.investment_judgment.stock_assessment.store import ReviewedAssessment


def scaffold_assessment_refresh(
    prior: ReviewedAssessment, *, as_of: date, assessed_at: datetime
) -> dict[str, object]:
    """原典時刻と仮定を保ち、今回の再確認用draftを作る。"""
    if assessed_at.tzinfo is None or assessed_at.utcoffset() is None:
        raise AssessmentError("refresh assessment time must include a timezone")
    if as_of < prior.document.input_snapshot.as_of or as_of > assessed_at.astimezone(JST).date():
        raise AssessmentError("refresh as_of cannot go backwards or follow assessed_at")
    if assessed_at < prior.document.conclusion.assessed_at:
        raise AssessmentError("refresh assessment time cannot precede the prior assessment")
    draft: dict[str, Any] = prior.document.model_dump(mode="json")
    draft["input_snapshot"]["as_of"] = as_of.isoformat()
    draft["conclusion"]["assessed_at"] = assessed_at.isoformat()
    valuation = draft["valuation"]
    # 日付更新だけで旧期間や取得済み分配権を新規投資へ再付与しない。
    valuation["horizon_months"] = None
    for name in ("base", "downside"):
        if valuation[name] is not None:
            valuation[name]["cash_distribution_per_share_yen"] = None
    draft["refresh"] = {
        "prior_assessment_ref": {
            "assessment_id": prior.assessment_id,
            "core_sha256": prior.core_sha256,
            "review_id": prior.review.review_id,
        },
        "checked_at": None,
        "material_information_check": None,
        "changed_sections": [],
        "unchanged_assumptions_reason": None,
        "horizon_reason": None,
        "distribution_reason": None,
        "corporate_action_reason": None,
    }
    return draft
