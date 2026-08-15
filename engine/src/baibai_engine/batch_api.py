"""Explicit engine boundary used by production batch orchestration."""

from baibai_engine.appdb.paths import database_path
from baibai_engine.appdb.read import connect_read_only
from baibai_engine.appdb.schema import APPLICATION_SCHEMA_VERSION
from baibai_engine.foundation.repository_layout import (
    APPLICATION_DB_PATH,
    CALIBRATION_DIR,
    MACRO_DB_PATH,
    MARKET_DB_PATH,
    RUNS_DB_PATH,
    STORE_LAYOUT_MAPPINGS,
    LegacyStorePathError,
    reject_legacy_store_paths,
)
from baibai_engine.macro.context.models import (
    MACRO_CONTEXT_SCHEMA_VERSION,
    MacroContextDocument,
    cited_series_ids,
    monitoring_condition_series_ids,
    scorecard_series_ids,
)
from baibai_engine.macro.indicators.cli import parse_refresh_failure_count
from baibai_engine.macro.indicators.db import (
    DEFAULT_DB_PATH as DEFAULT_MACRO_DB_PATH,
)
from baibai_engine.macro.indicators.db import (
    SQLITE_SCHEMA_VERSION as MACRO_SCHEMA_VERSION,
)
from baibai_engine.macro.indicators.db import (
    IndicatorsSchemaError,
)
from baibai_engine.macro.indicators.db import (
    open_connection as open_macro_store,
)
from baibai_engine.macro.indicators.db import (
    validate_current_schema as validate_macro_schema,
)
from baibai_engine.macro.indicators.definitions import IndicatorDefinitions, load_definitions
from baibai_engine.macro.indicators.service import (
    DEFAULT_LATEST_LOOKBACK_DAYS,
    LATEST_FETCH_LOOKBACK_DAYS,
)
from baibai_engine.market.lake.keys import (
    current_calibration_bundle_pointer_key as lake_current_calibration_bundle_pointer_key,
)
from baibai_engine.market.lake.keys import (
    current_l1_pointer_key as lake_current_l1_pointer_key,
)
from baibai_engine.market.lake.keys import dataset_manifest_key as lake_dataset_manifest_key
from baibai_engine.market.lake.keys import release_manifest_key as lake_release_manifest_key
from baibai_engine.market.lake.models import (
    CalibrationBundleManifest,
    CalibrationBundlePointer,
    CalibrationBundleRef,
    CalibrationInputManifest,
    CalibrationInputSourceRef,
    load_lake_model_json,
)
from baibai_engine.market.lake.models import DatasetManifest as LakeDatasetManifest
from baibai_engine.market.lake.models import RawArchiveMetadata as LakeRawArchiveMetadata
from baibai_engine.market.lake.models import RawIngestSourceRef as LakeRawIngestSourceRef
from baibai_engine.market.lake.models import ReleaseManifest as LakeReleaseManifest
from baibai_engine.market.lake.models import SQLiteSnapshotSourceRef as LakeSQLiteSnapshotSourceRef
from baibai_engine.market.lake.models import retained_sources as lake_retained_sources
from baibai_engine.market.lake.models import validate_release_policy as validate_lake_release_policy
from baibai_engine.market.lake.release import L1ReleasePointer, canonical_json_bytes
from baibai_engine.market.lake.sources import resolve_source_ref as resolve_lake_source_ref
from baibai_engine.market.sqlite import open_connection as open_market_store
from baibai_engine.market.sqlite.schema import (
    EDINET_DOCUMENT_DESCRIPTIVE_COLUMNS,
    EDINET_DOCUMENT_LIFECYCLE_COLUMNS,
)
from baibai_engine.market.sqlite.schema import (
    SQLITE_SCHEMA_VERSION as MARKET_SCHEMA_VERSION,
)
from baibai_engine.market.sqlite.schema import (
    SQLiteSchemaError as MarketSchemaError,
)
from baibai_engine.market.sqlite.schema import (
    validate_current_schema as validate_market_schema,
)
from baibai_engine.market.sqlite.snapshot import create_snapshot as create_market_snapshot
from baibai_engine.screening.calibration.lake import (
    canonical_manifest_bytes,
)
from baibai_engine.screening.run_store.migrations import RUN_STORE_SCHEMA_VERSION

__all__ = [
    "APPLICATION_DB_PATH",
    "APPLICATION_SCHEMA_VERSION",
    "CALIBRATION_DIR",
    "DEFAULT_LATEST_LOOKBACK_DAYS",
    "DEFAULT_MACRO_DB_PATH",
    "EDINET_DOCUMENT_DESCRIPTIVE_COLUMNS",
    "EDINET_DOCUMENT_LIFECYCLE_COLUMNS",
    "LATEST_FETCH_LOOKBACK_DAYS",
    "MACRO_CONTEXT_SCHEMA_VERSION",
    "MACRO_DB_PATH",
    "MACRO_SCHEMA_VERSION",
    "MARKET_DB_PATH",
    "MARKET_SCHEMA_VERSION",
    "RUNS_DB_PATH",
    "RUN_STORE_SCHEMA_VERSION",
    "STORE_LAYOUT_MAPPINGS",
    "CalibrationBundleManifest",
    "CalibrationBundlePointer",
    "CalibrationBundleRef",
    "CalibrationInputManifest",
    "CalibrationInputSourceRef",
    "IndicatorDefinitions",
    "IndicatorsSchemaError",
    "L1ReleasePointer",
    "LakeDatasetManifest",
    "LakeRawArchiveMetadata",
    "LakeRawIngestSourceRef",
    "LakeReleaseManifest",
    "LakeSQLiteSnapshotSourceRef",
    "LegacyStorePathError",
    "MacroContextDocument",
    "MarketSchemaError",
    "canonical_json_bytes",
    "canonical_manifest_bytes",
    "cited_series_ids",
    "connect_read_only",
    "create_market_snapshot",
    "database_path",
    "lake_current_calibration_bundle_pointer_key",
    "lake_current_l1_pointer_key",
    "lake_dataset_manifest_key",
    "lake_release_manifest_key",
    "lake_retained_sources",
    "load_definitions",
    "load_lake_model_json",
    "monitoring_condition_series_ids",
    "open_macro_store",
    "open_market_store",
    "parse_refresh_failure_count",
    "reject_legacy_store_paths",
    "resolve_lake_source_ref",
    "scorecard_series_ids",
    "validate_lake_release_policy",
    "validate_macro_schema",
    "validate_market_schema",
]
