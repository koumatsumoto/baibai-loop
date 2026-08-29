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
    StoreLayoutError,
    reject_noncanonical_store_paths,
    repository_root_error,
)
from baibai_engine.macro.context.models import (
    MACRO_CONTEXT_SCHEMA_VERSION,
    MacroContextDocument,
    cited_series_ids,
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
from baibai_engine.market.lake.datasets import LAKE_DATASETS
from baibai_engine.market.lake.identity import verified_git_commit as lake_verified_git_commit
from baibai_engine.market.lake.keys import (
    current_l1_pointer_key as lake_current_l1_pointer_key,
)
from baibai_engine.market.lake.keys import dataset_manifest_key as lake_dataset_manifest_key
from baibai_engine.market.lake.keys import release_manifest_key as lake_release_manifest_key
from baibai_engine.market.lake.models import DatasetManifest as LakeDatasetManifest
from baibai_engine.market.lake.models import ReleaseManifest as LakeReleaseManifest
from baibai_engine.market.lake.models import SQLiteSnapshotSourceRef as LakeSQLiteSnapshotSourceRef
from baibai_engine.market.lake.models import (
    canonical_lake_model_bytes,
    load_lake_model_json,
)
from baibai_engine.market.lake.models import validate_release_policy as validate_lake_release_policy
from baibai_engine.market.lake.objects import LocalMirrorSource
from baibai_engine.market.lake.objects import mirror_path as lake_mirror_path
from baibai_engine.market.lake.reader import FixedRelease as LakeFixedRelease
from baibai_engine.market.lake.reader import LakeReadError, resolve_release
from baibai_engine.market.lake.release import L1ReleasePointer
from baibai_engine.market.lake.release import create_l1_release as create_lake_l1_release
from baibai_engine.market.lake.writer import (
    LakeBuildError,
    LakeBuildReport,
    export_lake_legacy,
)
from baibai_engine.market.sqlite import open_connection as open_market_store
from baibai_engine.market.sqlite.lake_origin import (
    LakeStoreOrigin,
    LakeStoreOriginError,
    advance_lake_store_origin,
    read_lake_store_origin,
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
from baibai_engine.screening.run_store.schema import RUN_STORE_SCHEMA_VERSION

__all__ = [
    "APPLICATION_DB_PATH",
    "APPLICATION_SCHEMA_VERSION",
    "CALIBRATION_DIR",
    "DEFAULT_LATEST_LOOKBACK_DAYS",
    "DEFAULT_MACRO_DB_PATH",
    "LAKE_DATASETS",
    "LATEST_FETCH_LOOKBACK_DAYS",
    "MACRO_CONTEXT_SCHEMA_VERSION",
    "MACRO_DB_PATH",
    "MACRO_SCHEMA_VERSION",
    "MARKET_DB_PATH",
    "MARKET_SCHEMA_VERSION",
    "RUNS_DB_PATH",
    "RUN_STORE_SCHEMA_VERSION",
    "IndicatorDefinitions",
    "IndicatorsSchemaError",
    "L1ReleasePointer",
    "LakeBuildError",
    "LakeBuildReport",
    "LakeDatasetManifest",
    "LakeFixedRelease",
    "LakeReadError",
    "LakeReleaseManifest",
    "LakeSQLiteSnapshotSourceRef",
    "LakeStoreOrigin",
    "LakeStoreOriginError",
    "LocalMirrorSource",
    "MacroContextDocument",
    "MarketSchemaError",
    "StoreLayoutError",
    "advance_lake_store_origin",
    "canonical_lake_model_bytes",
    "cited_series_ids",
    "connect_read_only",
    "create_lake_l1_release",
    "create_market_snapshot",
    "database_path",
    "export_lake_legacy",
    "lake_current_l1_pointer_key",
    "lake_dataset_manifest_key",
    "lake_mirror_path",
    "lake_release_manifest_key",
    "lake_verified_git_commit",
    "load_definitions",
    "load_lake_model_json",
    "open_macro_store",
    "open_market_store",
    "parse_refresh_failure_count",
    "read_lake_store_origin",
    "reject_noncanonical_store_paths",
    "repository_root_error",
    "resolve_release",
    "scorecard_series_ids",
    "validate_lake_release_policy",
    "validate_macro_schema",
    "validate_market_schema",
]
