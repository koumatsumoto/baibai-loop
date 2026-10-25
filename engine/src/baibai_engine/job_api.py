"""Supply engine-owned facts and writes that let batch produce and stop loop outputs."""

from __future__ import annotations

from baibai_engine.data_collection.economic.acquisition import (
    DEFAULT_LATEST_LOOKBACK_DAYS,
    LATEST_FETCH_LOOKBACK_DAYS,
)
from baibai_engine.data_collection.economic.acquisition import (
    SourceRefreshFailure as EconomicSourceRefreshFailure,
)
from baibai_engine.data_collection.economic.acquisition import (
    refresh_sources as refresh_economic_sources,
)
from baibai_engine.data_collection.economic.handoff import export_receipts as export_economic_receipts
from baibai_engine.data_collection.economic.receipt_policy import (
    retain_rechecks as retain_economic_rechecks,
)
from baibai_engine.data_collection.economic.records import (
    decode_records as decode_economic_source_records,
)
from baibai_engine.data_collection.economic.refresh_policy import (
    acquisition_groups as economic_acquisition_groups,
)
from baibai_engine.data_collection.economic.refresh_policy import (
    explicit_recovery_ranges as economic_explicit_recovery_ranges,
)
from baibai_engine.data_collection.economic.refresh_policy import (
    source_policy as economic_source_policy,
)
from baibai_engine.data_collection.economic.sources import load_sources as load_economic_sources
from baibai_engine.data_collection.economic.store import (
    DEFAULT_DB_PATH as DEFAULT_ECONOMIC_DB_PATH,
)
from baibai_engine.data_collection.economic.store import EconomicSchemaError
from baibai_engine.data_collection.economic.store import (
    validate_current_schema as validate_economic_schema,
)
from baibai_engine.data_collection.equity_web.http import (
    HostRateLimiter as ExternalAssessmentHostRateLimiter,
)
from baibai_engine.data_collection.equity_web.models import EquityWebSourceResponse
from baibai_engine.data_collection.equity_web.models import Source as ExternalAssessmentSource
from baibai_engine.data_collection.equity_web.receipts import (
    read_responses as equity_web_read_responses,
)
from baibai_engine.data_collection.equity_web.receipts import (
    validate_response as equity_web_validate_response,
)
from baibai_engine.data_collection.jquants.dependencies import (
    export_dependencies as export_master_calendar_dependencies,
)
from baibai_engine.data_collection.external_assessment.cli import (
    main as external_assessment_acquisition_main,
)
from baibai_engine.data_collection.external_assessment.collection import (
    PARSE_FAILURE_REASONS as external_assessment_parse_failure_reasons,
)
from baibai_engine.data_collection.external_assessment.collection import (
    CollectionOutcome as ExternalAssessmentCollectionOutcome,
)
from baibai_engine.data_collection.external_assessment.collection import (
    CollectionResult as ExternalAssessmentCollectionResult,
)
from baibai_engine.data_collection.external_assessment.collection import (
    collect_pair as external_assessment_collect_pair,
)
from baibai_engine.data_collection.external_assessment.collection import (
    collector_binding as external_assessment_collector_binding,
)
from baibai_engine.data_collection.external_assessment.universe import (
    calendar_master_asof as external_assessment_calendar_master_asof,
)
from baibai_engine.data_collection.external_assessment.universe import (
    frozen_lake_universe as external_assessment_frozen_lake_universe,
)
from baibai_engine.data_collection.external_assessment.universe import (
    received_calendar as external_assessment_received_calendar,
)
from baibai_engine.data_collection.external_assessment.universe import (
    received_master_universe as external_assessment_received_master_universe,
)
from baibai_engine.data_collection.external_assessment.verification import (
    history_digests as external_assessment_history_digests,
)
from baibai_engine.data_collection.external_assessment.verification import (
    verify_release_history as external_assessment_verify_release_history,
)
from baibai_engine.machine_analysis.external_assessment.normalization.decode import (
    decode_attempt as external_assessment_decode_attempt,
)
from baibai_engine.machine_analysis.external_assessment.observations.identity import (
    ANALYST_FORECAST_DATASET as EXTERNAL_ASSESSMENT_ANALYST_FORECAST_DATASET,
)
from baibai_engine.machine_analysis.external_assessment.observations.identity import (
    ANALYST_RATING_DATASET as EXTERNAL_ASSESSMENT_ANALYST_RATING_DATASET,
)
from baibai_engine.machine_analysis.external_assessment.observations.identity import (
    MODEL_VALUATION_DATASET as EXTERNAL_ASSESSMENT_MODEL_VALUATION_DATASET,
)
from baibai_engine.machine_analysis.external_assessment.observations.identity import (
    QUANTS_SCORE_DATASET as EXTERNAL_ASSESSMENT_QUANTS_SCORE_DATASET,
)
from baibai_engine.machine_analysis.external_assessment.observations.models import (
    AnalystForecastObservation as ExternalAssessmentAnalystForecastObservation,
)
from baibai_engine.machine_analysis.external_assessment.observations.models import (
    AnalystRatingObservation as ExternalAssessmentAnalystRatingObservation,
)
from baibai_engine.machine_analysis.external_assessment.observations.models import (
    ModelValuationObservation as ExternalAssessmentModelValuationObservation,
)
from baibai_engine.machine_analysis.external_assessment.observations.models import (
    Observation as ExternalAssessmentObservation,
)
from baibai_engine.machine_analysis.external_assessment.observations.models import (
    QuantsScoreObservation as ExternalAssessmentQuantsScoreObservation,
)
from baibai_engine.machine_analysis.external_assessment.store import (
    save_observations as external_assessment_save_observations,
)
from baibai_engine.data_collection.tradingview.collector import (
    preflight_snapshot as tradingview_preflight,
)
from baibai_engine.machine_analysis.external_assessment.tradingview.diagnostics import (
    failure_line as tradingview_failure_line,
)
from baibai_engine.machine_analysis.external_assessment.tradingview.diagnostics import (
    safe_progress as tradingview_safe_progress,
)
from baibai_engine.data_collection.tradingview.handoff import TradingViewHandoff
from baibai_engine.data_collection.tradingview.handoff import (
    export_handoff as export_tradingview_handoff,
)
from baibai_engine.data_collection.tradingview.handoff import (
    import_handoff as import_tradingview_handoff,
)
from baibai_engine.machine_analysis.external_assessment.tradingview.observations import (
    FetchError as TradingViewFetchError,
)
from baibai_engine.foundation.repository_layout import (
    MARKET_DB_PATH,
    RUNS_DB_PATH,
    StoreLayoutError,
    reject_noncanonical_store_paths,
    repository_root_error,
)
from baibai_engine.market.edinet_acquisition import acquire_archives as acquire_edinet_archives
from baibai_engine.market.edinet_handoff import archive_keys as edinet_archive_keys
from baibai_engine.market.edinet_handoff import export_handoff as export_edinet_handoff
from baibai_engine.market.edinet_handoff import import_handoff as import_edinet_handoff
from baibai_engine.market.edinet_integration import extract_saved_edinet
from baibai_engine.market.jpx_handoff import export_handoff as export_jpx_handoff
from baibai_engine.market.jpx_handoff import import_handoff as import_jpx_handoff
from baibai_engine.market.jquants_decode import JQuantsProviderError
from baibai_engine.market.jquants_handoff import FINANCIAL_SOURCES as JQUANTS_FINANCIAL_SOURCES
from baibai_engine.market.jquants_handoff import MARKET_SOURCES as JQUANTS_MARKET_SOURCES
from baibai_engine.market.jquants_handoff import export_handoff as export_jquants_handoff
from baibai_engine.market.jquants_handoff import import_handoff as import_jquants_handoff
from baibai_engine.market.source_inputs import SOURCE_INPUT_GROUPS
from baibai_engine.market.source_inputs import export_source_input as export_market_source_input
from baibai_engine.machine_analysis.screening.discovery.contracts import (
    validate_candidate_run as validate_candidate_run,
)
from baibai_engine.storage.lake.duck import lake_session
from baibai_engine.storage.lake.equity_web_verification import (
    history_digests as equity_web_history_digests,
)
from baibai_engine.storage.lake.equity_web_verification import (
    response_digest as equity_web_response_digest,
)
from baibai_engine.storage.lake.equity_web_verification import (
    verify_release_history as equity_web_verify_release_history,
)
from baibai_engine.storage.lake.identity import verified_git_commit as lake_verified_git_commit
from baibai_engine.storage.lake.keys import (
    current_release_pointer_key as lake_current_release_pointer_key,
)
from baibai_engine.storage.lake.keys import dataset_manifest_key as lake_dataset_manifest_key
from baibai_engine.storage.lake.keys import release_manifest_key as lake_release_manifest_key
from baibai_engine.storage.lake.models import DatasetManifest as LakeDatasetManifest
from baibai_engine.storage.lake.models import ReleaseManifest as LakeReleaseManifest
from baibai_engine.storage.lake.models import SQLiteSnapshotSourceRef as LakeSQLiteSnapshotSourceRef
from baibai_engine.storage.lake.models import canonical_lake_model_bytes, load_lake_model_json
from baibai_engine.storage.lake.models import (
    validate_release_policy as validate_lake_release_policy,
)
from baibai_engine.storage.lake.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.lake.objects import mirror_path as lake_mirror_path
from baibai_engine.storage.lake.reader import FixedRelease as LakeFixedRelease
from baibai_engine.storage.lake.reader import LakeReadError, resolve_release
from baibai_engine.storage.lake.release import LakeReleasePointer
from baibai_engine.storage.lake.release import create_release as create_lake_release
from baibai_engine.storage.lake.writer import (
    LakeBuildError,
    LakeBuildReport,
    export_market_store,
)
from baibai_engine.storage.observations.equity_web_import import (
    import_responses as equity_web_import_responses,
)
from baibai_engine.storage.observations.lake_origin import (
    LakeStoreOrigin,
    LakeStoreOriginError,
    advance_lake_store_origin,
    read_lake_store_origin,
)
from baibai_engine.storage.observations.schema import SQLITE_SCHEMA_VERSION as MARKET_SCHEMA_VERSION
from baibai_engine.storage.observations.schema import SQLiteSchemaError as MarketSchemaError
from baibai_engine.storage.observations.schema import (
    validate_current_schema as validate_market_schema,
)
from baibai_engine.storage.observations.snapshot import create_snapshot as create_market_snapshot

__all__ = [
    "DEFAULT_ECONOMIC_DB_PATH",
    "DEFAULT_LATEST_LOOKBACK_DAYS",
    "EXTERNAL_ASSESSMENT_ANALYST_FORECAST_DATASET",
    "EXTERNAL_ASSESSMENT_ANALYST_RATING_DATASET",
    "EXTERNAL_ASSESSMENT_MODEL_VALUATION_DATASET",
    "EXTERNAL_ASSESSMENT_QUANTS_SCORE_DATASET",
    "JQUANTS_FINANCIAL_SOURCES",
    "JQUANTS_MARKET_SOURCES",
    "LATEST_FETCH_LOOKBACK_DAYS",
    "MARKET_DB_PATH",
    "MARKET_SCHEMA_VERSION",
    "RUNS_DB_PATH",
    "SOURCE_INPUT_GROUPS",
    "EconomicSchemaError",
    "EconomicSourceRefreshFailure",
    "EquityWebSourceResponse",
    "ExternalAssessmentAnalystForecastObservation",
    "ExternalAssessmentAnalystRatingObservation",
    "ExternalAssessmentCollectionOutcome",
    "ExternalAssessmentCollectionResult",
    "ExternalAssessmentHostRateLimiter",
    "ExternalAssessmentModelValuationObservation",
    "ExternalAssessmentObservation",
    "ExternalAssessmentQuantsScoreObservation",
    "ExternalAssessmentSource",
    "JQuantsProviderError",
    "LakeBuildError",
    "LakeBuildReport",
    "LakeDatasetManifest",
    "LakeFixedRelease",
    "LakeObjectCache",
    "LakeReadError",
    "LakeReleaseManifest",
    "LakeReleasePointer",
    "LakeSQLiteSnapshotSourceRef",
    "LakeStoreOrigin",
    "LakeStoreOriginError",
    "LocalMirrorSource",
    "MarketSchemaError",
    "StoreLayoutError",
    "TradingViewFetchError",
    "TradingViewHandoff",
    "acquire_edinet_archives",
    "advance_lake_store_origin",
    "canonical_lake_model_bytes",
    "create_lake_release",
    "create_market_snapshot",
    "decode_economic_source_records",
    "economic_acquisition_groups",
    "economic_explicit_recovery_ranges",
    "economic_source_policy",
    "edinet_archive_keys",
    "equity_web_history_digests",
    "equity_web_import_responses",
    "equity_web_read_responses",
    "equity_web_response_digest",
    "equity_web_validate_response",
    "equity_web_verify_release_history",
    "export_economic_receipts",
    "export_edinet_handoff",
    "export_jpx_handoff",
    "export_jquants_handoff",
    "export_market_source_input",
    "export_market_store",
    "export_master_calendar_dependencies",
    "export_tradingview_handoff",
    "external_assessment_acquisition_main",
    "external_assessment_calendar_master_asof",
    "external_assessment_collect_pair",
    "external_assessment_collector_binding",
    "external_assessment_decode_attempt",
    "external_assessment_frozen_lake_universe",
    "external_assessment_history_digests",
    "external_assessment_parse_failure_reasons",
    "external_assessment_received_calendar",
    "external_assessment_received_master_universe",
    "external_assessment_save_observations",
    "external_assessment_verify_release_history",
    "extract_saved_edinet",
    "import_edinet_handoff",
    "import_jpx_handoff",
    "import_jquants_handoff",
    "import_tradingview_handoff",
    "lake_current_release_pointer_key",
    "lake_dataset_manifest_key",
    "lake_mirror_path",
    "lake_release_manifest_key",
    "lake_session",
    "lake_verified_git_commit",
    "load_economic_sources",
    "load_lake_model_json",
    "read_lake_store_origin",
    "refresh_economic_sources",
    "reject_noncanonical_store_paths",
    "repository_root_error",
    "resolve_release",
    "retain_economic_rechecks",
    "tradingview_failure_line",
    "tradingview_preflight",
    "tradingview_safe_progress",
    "validate_candidate_run",
    "validate_economic_schema",
    "validate_lake_release_policy",
    "validate_market_schema",
]
