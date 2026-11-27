"""保存済み運用成績だけを表示する。"""

import sqlite3
from pathlib import Path

from baibai_engine.portfolio.performance.store import PortfolioOutcomeStore
from baibai_engine.storage.sqlite.read import is_unwritten_database, read_store_if_written
from baibai_web.readmodel.models import OutcomesView, PortfolioOutcomeView


def build_outcomes(db_path: Path) -> OutcomesView:
    try:
        written = read_store_if_written(db_path, lambda: not is_unwritten_database(db_path))
        if not written:
            return OutcomesView(error="保存済み運用成績のstoreがありません。", outcomes=[])
        outcomes = [
            PortfolioOutcomeView.model_validate(value)
            for value in PortfolioOutcomeStore(db_path).list()
        ]
    except (OSError, ValueError, TypeError, RuntimeError, sqlite3.Error):
        # A broken outcome must not block independent ledger/proposal publication.
        # Do not expose stored payloads or reader diagnostics to the browser.
        return OutcomesView(error="保存済み運用成績を読み取れません。", outcomes=[])
    return OutcomesView(error=None, outcomes=outcomes)
