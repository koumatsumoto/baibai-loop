"""Finite production dataset inventory composed from L1 and observation owners."""

from types import MappingProxyType

from baibai_engine.data_collection.receipts.articles.snapshot_contracts import ARTICLE_RECEIPTS
from baibai_engine.machine_analysis.observations.articles.snapshot_contracts import (
    ARTICLE_PROJECTION_RUNS,
    ARTICLE_OBSERVATIONS,
    ARTICLE_EXTRACTION_ARTIFACTS,
)
from baibai_engine.data_collection.providers.edinet.snapshot_contracts import (
    EDINET_LIST_RESPONSES,
    EDINET_SOURCE_ARCHIVES,
)
from baibai_engine.data_collection.providers.jpx.snapshot_contracts import (
    JPX_SOURCE_BUNDLES,
    JPX_SOURCE_RESPONSES,
)
from baibai_engine.data_collection.providers.jquants.snapshot_contracts import (
    JQUANTS_SOURCE_PAGES,
)
from baibai_engine.data_collection.providers.tradingview.snapshot_contracts import (
    TRADINGVIEW_SOURCE_RECEIPTS,
)
from baibai_engine.data_collection.receipts.public_pages.snapshot_contracts import (
    EQUITY_WEB_SOURCE_RESPONSES,
)
from baibai_engine.machine_analysis.observations.disclosures.snapshot_contracts import (
    EDINET_DOCUMENT_LISTS,
    EDINET_DOCUMENTS,
    JPX_DELISTINGS,
    JPX_EARNINGS_CALENDAR,
    JPX_REGULATION_FLAGS,
    JPX_REGULATION_SOURCES,
    TENDER_OFFER_EXIT_VALUES,
    TSE_CAPITAL_POLICY_SNAPSHOTS,
)
from baibai_engine.machine_analysis.observations.external_assessments.snapshot_contracts import (
    EXTERNAL_ASSESSMENT_ANALYST_FORECAST_OBSERVATIONS,
    EXTERNAL_ASSESSMENT_ANALYST_RATING_OBSERVATIONS,
    EXTERNAL_ASSESSMENT_MODEL_VALUATION_OBSERVATIONS,
    EXTERNAL_ASSESSMENT_QUANTS_SCORE_OBSERVATIONS,
    TRADINGVIEW_FORECAST_SNAPSHOTS,
)
from baibai_engine.machine_analysis.observations.financials.snapshot_contracts import (
    EDINET_DEBT_SCHEDULE,
    EDINET_METRICS,
    EDINET_SEGMENT_FACTS,
    JQUANTS_FIN_SUMMARIES,
)
from baibai_engine.machine_analysis.observations.prices.snapshot_contracts import (
    JQUANTS_DAILY_BARS,
    JQUANTS_MARKET_CALENDAR,
)
from baibai_engine.machine_analysis.observations.stocks.snapshot_contracts import (
    JQUANTS_MASTER_SNAPSHOTS,
)
from baibai_engine.machine_analysis.observations.trading_activity.snapshot_contracts import (
    JQUANTS_ALL_ISSUES_DAILY_MARGIN,
    JQUANTS_MARGIN_ALERTS,
    JQUANTS_SHORT_SALE_REPORTS,
    JQUANTS_WEEKLY_MARGIN,
)
from baibai_engine.storage.snapshots.contracts import LakeDataset

LAKE_DATASETS = MappingProxyType(
    {
        ARTICLE_RECEIPTS.name: ARTICLE_RECEIPTS,
        ARTICLE_PROJECTION_RUNS.name: ARTICLE_PROJECTION_RUNS,
        ARTICLE_OBSERVATIONS.name: ARTICLE_OBSERVATIONS,
        ARTICLE_EXTRACTION_ARTIFACTS.name: ARTICLE_EXTRACTION_ARTIFACTS,
        EXTERNAL_ASSESSMENT_ANALYST_FORECAST_OBSERVATIONS.name: (
            EXTERNAL_ASSESSMENT_ANALYST_FORECAST_OBSERVATIONS
        ),
        JQUANTS_SOURCE_PAGES.name: JQUANTS_SOURCE_PAGES,
        EDINET_LIST_RESPONSES.name: EDINET_LIST_RESPONSES,
        JPX_SOURCE_BUNDLES.name: JPX_SOURCE_BUNDLES,
        JPX_SOURCE_RESPONSES.name: JPX_SOURCE_RESPONSES,
        TRADINGVIEW_SOURCE_RECEIPTS.name: TRADINGVIEW_SOURCE_RECEIPTS,
        EQUITY_WEB_SOURCE_RESPONSES.name: EQUITY_WEB_SOURCE_RESPONSES,
        EDINET_SOURCE_ARCHIVES.name: EDINET_SOURCE_ARCHIVES,
        EXTERNAL_ASSESSMENT_QUANTS_SCORE_OBSERVATIONS.name: (
            EXTERNAL_ASSESSMENT_QUANTS_SCORE_OBSERVATIONS
        ),
        EXTERNAL_ASSESSMENT_MODEL_VALUATION_OBSERVATIONS.name: (
            EXTERNAL_ASSESSMENT_MODEL_VALUATION_OBSERVATIONS
        ),
        EXTERNAL_ASSESSMENT_ANALYST_RATING_OBSERVATIONS.name: (
            EXTERNAL_ASSESSMENT_ANALYST_RATING_OBSERVATIONS
        ),
        TRADINGVIEW_FORECAST_SNAPSHOTS.name: TRADINGVIEW_FORECAST_SNAPSHOTS,
        EDINET_DEBT_SCHEDULE.name: EDINET_DEBT_SCHEDULE,
        EDINET_SEGMENT_FACTS.name: EDINET_SEGMENT_FACTS,
        JQUANTS_DAILY_BARS.name: JQUANTS_DAILY_BARS,
        JQUANTS_SHORT_SALE_REPORTS.name: JQUANTS_SHORT_SALE_REPORTS,
        JQUANTS_WEEKLY_MARGIN.name: JQUANTS_WEEKLY_MARGIN,
        JQUANTS_ALL_ISSUES_DAILY_MARGIN.name: JQUANTS_ALL_ISSUES_DAILY_MARGIN,
        JQUANTS_MASTER_SNAPSHOTS.name: JQUANTS_MASTER_SNAPSHOTS,
        JQUANTS_FIN_SUMMARIES.name: JQUANTS_FIN_SUMMARIES,
        JQUANTS_MARKET_CALENDAR.name: JQUANTS_MARKET_CALENDAR,
        JPX_EARNINGS_CALENDAR.name: JPX_EARNINGS_CALENDAR,
        JQUANTS_MARGIN_ALERTS.name: JQUANTS_MARGIN_ALERTS,
        EDINET_DOCUMENTS.name: EDINET_DOCUMENTS,
        EDINET_METRICS.name: EDINET_METRICS,
        EDINET_DOCUMENT_LISTS.name: EDINET_DOCUMENT_LISTS,
        JPX_REGULATION_FLAGS.name: JPX_REGULATION_FLAGS,
        JPX_REGULATION_SOURCES.name: JPX_REGULATION_SOURCES,
        JPX_DELISTINGS.name: JPX_DELISTINGS,
        TSE_CAPITAL_POLICY_SNAPSHOTS.name: TSE_CAPITAL_POLICY_SNAPSHOTS,
        TENDER_OFFER_EXIT_VALUES.name: TENDER_OFFER_EXIT_VALUES,
    }
)


def require_lake_dataset(name: str) -> LakeDataset:
    try:
        return LAKE_DATASETS[name]
    except KeyError as exc:
        allowed = ", ".join(sorted(LAKE_DATASETS))
        raise ValueError(f"unsupported lake dataset {name!r}; expected one of: {allowed}") from exc
