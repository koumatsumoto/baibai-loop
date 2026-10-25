"""保存済み運用成績だけを表示する。"""

from pathlib import Path

from baibai_engine.position.outcome_store import PortfolioOutcomeStore
from baibai_web.readmodel.models import OutcomesView, PortfolioOutcomeView


def build_outcomes(db_path: Path) -> OutcomesView:
    return OutcomesView(
        outcomes=[
            PortfolioOutcomeView.model_validate(value)
            for value in PortfolioOutcomeStore(db_path).list()
        ]
    )
