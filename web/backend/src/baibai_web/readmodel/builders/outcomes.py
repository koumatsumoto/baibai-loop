"""保存済み運用成績だけを表示する。"""

import sqlite3
from pathlib import Path

from baibai_engine.portfolio.performance.store import PortfolioOutcomeStore
from baibai_engine.storage.sqlite.read import is_unwritten_database
from baibai_web.readmodel.models import OutcomesView, PortfolioOutcomeView


def build_outcomes(db_path: Path) -> OutcomesView:
    try:
        if not db_path.is_file() or is_unwritten_database(db_path):
            return OutcomesView(error="保存済み運用成績のstoreがありません。", outcomes=[])
        outcomes = [
            PortfolioOutcomeView.model_validate(value)
            for value in PortfolioOutcomeStore(db_path).list()
        ]
    except (ValueError, TypeError, sqlite3.Error):
        # A broken outcome must not block independent ledger/proposal publication.
        # Do not expose stored payloads or reader diagnostics to the browser.
        return OutcomesView(error="保存済み運用成績を読み取れません。", outcomes=[])
    return OutcomesView(error=None, outcomes=outcomes)
