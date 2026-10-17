"""Supply engine-owned facts and writes that let batch produce and stop loop outputs."""

from __future__ import annotations

from baibai_engine.collection.economic_acquisition import (
    DEFAULT_LATEST_LOOKBACK_DAYS,
    LATEST_FETCH_LOOKBACK_DAYS,
)
from baibai_engine.collection.economic_records import (
    decode_records as decode_economic_source_records,
)
from baibai_engine.collection.economic_store import (
    DEFAULT_DB_PATH as DEFAULT_ECONOMIC_DB_PATH,
)
from baibai_engine.collection.economic_store import (
    SQLITE_SCHEMA_VERSION as ECONOMIC_SCHEMA_VERSION,
)
from baibai_engine.collection.economic_store import EconomicSchemaError
from baibai_engine.collection.economic_store import (
    validate_current_schema as validate_economic_schema,
)
from baibai_engine.collection.equity_web.models import EquityWebSourceResponse
from baibai_engine.collection.equity_web.models import Source as ExternalAssessmentSource
from baibai_engine.collection.equity_web.receipts import (
    read_responses as equity_web_read_responses,
)
from baibai_engine.collection.equity_web.receipts import (
    validate_response as equity_web_validate_response,
)
from baibai_engine.external_assessment.acquisition.cli import (
    main as external_assessment_acquisition_main,
)
from baibai_engine.external_assessment.acquisition.collection import (
    PARSE_FAILURE_REASONS as external_assessment_parse_failure_reasons,
)
from baibai_engine.external_assessment.acquisition.collection import (
    CollectionOutcome as ExternalAssessmentCollectionOutcome,
)
from baibai_engine.external_assessment.acquisition.collection import (
    CollectionResult as ExternalAssessmentCollectionResult,
)
from baibai_engine.external_assessment.acquisition.collection import (
    HostRateLimiter as ExternalAssessmentHostRateLimiter,
)
from baibai_engine.external_assessment.acquisition.collection import (
    collect_pair as external_assessment_collect_pair,
)
from baibai_engine.external_assessment.acquisition.collection import (
    collector_binding as external_assessment_collector_binding,
)
from baibai_engine.external_assessment.acquisition.universe import (
    calendar_master_asof as external_assessment_calendar_master_asof,
)
from baibai_engine.external_assessment.acquisition.universe import (
    frozen_lake_universe as external_assessment_frozen_lake_universe,
)
from baibai_engine.external_assessment.acquisition.universe import (
    frozen_universe as external_assessment_frozen_universe,
)
from baibai_engine.external_assessment.acquisition.verification import (
    history_digests as external_assessment_history_digests,
)
from baibai_engine.external_assessment.acquisition.verification import (
    verify_observations as external_assessment_verify_observations,
)
from baibai_engine.external_assessment.acquisition.verification import (
    verify_release_history as external_assessment_verify_release_history,
)
from baibai_engine.external_assessment.normalization.decode import (
    decode_attempt as external_assessment_decode_attempt,
)
from baibai_engine.external_assessment.observations.identity import (
    ANALYST_RATING_DATASET as EXTERNAL_ASSESSMENT_ANALYST_RATING_DATASET,
)
from baibai_engine.external_assessment.observations.identity import (
    MODEL_VALUATION_DATASET as EXTERNAL_ASSESSMENT_MODEL_VALUATION_DATASET,
)
from baibai_engine.external_assessment.observations.identity import (
    QUANTS_SCORE_DATASET as EXTERNAL_ASSESSMENT_QUANTS_SCORE_DATASET,
)
from baibai_engine.external_assessment.observations.models import (
    AnalystRatingObservation as ExternalAssessmentAnalystRatingObservation,
)
from baibai_engine.external_assessment.observations.models import (
    ModelValuationObservation as ExternalAssessmentModelValuationObservation,
)
from baibai_engine.external_assessment.observations.models import (
    Observation as ExternalAssessmentObservation,
)
from baibai_engine.external_assessment.observations.models import (
    QuantsScoreObservation as ExternalAssessmentQuantsScoreObservation,
)
from baibai_engine.external_assessment.store import (
    save_observations as external_assessment_save_observations,
)
from baibai_engine.external_assessment.tradingview.accepted import (
    accept_observations as tradingview_accept_observations,
)
from baibai_engine.external_assessment.tradingview.accepted import (
    load_observations as tradingview_load_observations,
)
from baibai_engine.external_assessment.tradingview.accepted import (
    verify_release_observations as tradingview_verify_release_observations,
)
from baibai_engine.external_assessment.tradingview.collector import (
    preflight_snapshot as tradingview_preflight,
)
from baibai_engine.external_assessment.tradingview.collector import (
    validate_time as tradingview_validate_time,
)
from baibai_engine.external_assessment.tradingview.diagnostics import (
    failure_line as tradingview_failure_line,
)
from baibai_engine.external_assessment.tradingview.diagnostics import (
    safe_progress as tradingview_safe_progress,
)
from baibai_engine.foundation.repository_layout import (
    APPLICATION_DB_PATH,
    CALIBRATION_DIR,
    INDICATORS_DB_PATH,
    MARKET_DB_PATH,
    RUNS_DB_PATH,
    StoreLayoutError,
    reject_noncanonical_store_paths,
    repository_root_error,
)
from baibai_engine.market_context.indicators.definitions import (
    IndicatorDefinitions,
    load_definitions,
)
from baibai_engine.research.market_context.models import (
    ASSESSMENT_SCHEMA_VERSION,
    ASSESSMENT_STALE_DAYS,
    StoredAssessment,
    require_aware,
)
from baibai_engine.research.market_context.projection import (
    assessment_projection,
    assessment_series_ids,
)
from baibai_engine.screening.discovery.contracts import (
    validate_candidate_run as validate_candidate_run,
)
from baibai_engine.screening.run_store.schema import RUN_STORE_SCHEMA_VERSION
from baibai_engine.storage.application.paths import database_path
from baibai_engine.storage.application.read import connect_read_only
from baibai_engine.storage.application.schema import APPLICATION_SCHEMA_VERSION
from baibai_engine.storage.lake.datasets import LAKE_DATASETS
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
from baibai_engine.storage.lake.hydrate import hydrate_market_store
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
from baibai_engine.storage.observations.schema import open_connection as open_market_store
from baibai_engine.storage.observations.schema import (
    validate_current_schema as validate_market_schema,
)
from baibai_engine.storage.observations.snapshot import create_snapshot as create_market_snapshot

__all__ = [
    "APPLICATION_DB_PATH",
    "APPLICATION_SCHEMA_VERSION",
    "ASSESSMENT_SCHEMA_VERSION",
    "ASSESSMENT_STALE_DAYS",
    "CALIBRATION_DIR",
    "DEFAULT_ECONOMIC_DB_PATH",
    "DEFAULT_LATEST_LOOKBACK_DAYS",
    "ECONOMIC_SCHEMA_VERSION",
    "EXTERNAL_ASSESSMENT_ANALYST_RATING_DATASET",
    "EXTERNAL_ASSESSMENT_MODEL_VALUATION_DATASET",
    "EXTERNAL_ASSESSMENT_QUANTS_SCORE_DATASET",
    "INDICATORS_DB_PATH",
    "LAKE_DATASETS",
    "LATEST_FETCH_LOOKBACK_DAYS",
    "MARKET_DB_PATH",
    "MARKET_SCHEMA_VERSION",
    "RUNS_DB_PATH",
    "RUN_STORE_SCHEMA_VERSION",
    "EconomicSchemaError",
    "EquityWebSourceResponse",
    "ExternalAssessmentAnalystRatingObservation",
    "ExternalAssessmentCollectionOutcome",
    "ExternalAssessmentCollectionResult",
    "ExternalAssessmentHostRateLimiter",
    "ExternalAssessmentModelValuationObservation",
    "ExternalAssessmentObservation",
    "ExternalAssessmentQuantsScoreObservation",
    "ExternalAssessmentSource",
    "IndicatorDefinitions",
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
    "StoredAssessment",
    "advance_lake_store_origin",
    "assessment_projection",
    "assessment_series_ids",
    "canonical_lake_model_bytes",
    "connect_read_only",
    "create_lake_release",
    "create_market_snapshot",
    "database_path",
    "decode_economic_source_records",
    "equity_web_history_digests",
    "equity_web_import_responses",
    "equity_web_read_responses",
    "equity_web_response_digest",
    "equity_web_validate_response",
    "equity_web_verify_release_history",
    "export_market_store",
    "external_assessment_acquisition_main",
    "external_assessment_calendar_master_asof",
    "external_assessment_collect_pair",
    "external_assessment_collector_binding",
    "external_assessment_decode_attempt",
    "external_assessment_frozen_lake_universe",
    "external_assessment_frozen_universe",
    "external_assessment_history_digests",
    "external_assessment_parse_failure_reasons",
    "external_assessment_save_observations",
    "external_assessment_verify_observations",
    "external_assessment_verify_release_history",
    "hydrate_market_store",
    "lake_current_release_pointer_key",
    "lake_dataset_manifest_key",
    "lake_mirror_path",
    "lake_release_manifest_key",
    "lake_session",
    "lake_verified_git_commit",
    "load_definitions",
    "load_lake_model_json",
    "open_market_store",
    "read_lake_store_origin",
    "reject_noncanonical_store_paths",
    "repository_root_error",
    "require_aware",
    "resolve_release",
    "tradingview_accept_observations",
    "tradingview_failure_line",
    "tradingview_load_observations",
    "tradingview_preflight",
    "tradingview_safe_progress",
    "tradingview_validate_time",
    "tradingview_verify_release_observations",
    "validate_candidate_run",
    "validate_economic_schema",
    "validate_lake_release_policy",
    "validate_market_schema",
]
