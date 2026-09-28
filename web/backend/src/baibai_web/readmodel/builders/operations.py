"""保存済みOperationを表示する。"""

from __future__ import annotations

from baibai_web.readmodel.models import (
    OperationSessionView,
    OperationsView,
    PortfolioOutcomeView,
)
from baibai_web.sources.db_sources import DbOperationsSource


def build_operations_view(source: DbOperationsSource) -> OperationsView:
    return OperationsView(
        operations=[OperationSessionView.model_validate(item) for item in source.operations()],
        outcomes=[PortfolioOutcomeView.model_validate(item) for item in source.outcomes()],
    )
