"""Market source別writerの入口。Screeningの必要入力判定はここに置かない。"""

from baibai_engine.market.sqlite import (
    SCHEMA_VERSION,
    SQLITE_SCHEMA_VERSION,
    SQLiteSchemaError,
    open_connection,
    store_jquants_daily_bars,
    store_jquants_market_calendar,
    validate_current_schema,
)
from baibai_engine.market.sqlite.ingest.edinet import store_edinet_documents, store_edinet_metrics
from baibai_engine.market.sqlite.ingest.jpx import (
    store_jpx_earnings_calendar_snapshot,
    store_jpx_regulations,
)
from baibai_engine.market.sqlite.ingest.jquants import (
    store_jquants_all_issues_daily_margin,
    store_jquants_fin_summaries,
    store_jquants_margin_alerts,
    store_jquants_master,
    store_jquants_short_sale_reports,
    store_jquants_weekly_margin,
)

__all__ = [
    "SCHEMA_VERSION",
    "SQLITE_SCHEMA_VERSION",
    "SQLiteSchemaError",
    "open_connection",
    "store_edinet_documents",
    "store_edinet_metrics",
    "store_jpx_earnings_calendar_snapshot",
    "store_jpx_regulations",
    "store_jquants_all_issues_daily_margin",
    "store_jquants_daily_bars",
    "store_jquants_fin_summaries",
    "store_jquants_margin_alerts",
    "store_jquants_market_calendar",
    "store_jquants_master",
    "store_jquants_short_sale_reports",
    "store_jquants_weekly_margin",
    "validate_current_schema",
]
