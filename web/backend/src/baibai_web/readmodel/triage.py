"""指定IDの保存Triageを、判断時の本文と入力のまま表示する。"""

from __future__ import annotations

from baibai_web.readmodel.models import ResearchTriageDetailView
from baibai_web.readmodel.stocks.research import _research_triage_view
from baibai_web.sources.protocols import ResearchSource


def build_research_triage_detail(
    research: ResearchSource, *, research_triage_id: str
) -> ResearchTriageDetailView | None:
    raw = research.research_triage(research_triage_id)
    if raw is None:
        return None
    return ResearchTriageDetailView(**_research_triage_view(raw).model_dump(), payload=raw)
