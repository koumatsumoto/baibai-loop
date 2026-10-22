"""Single wiring point for repository-rooted application sources.

The FastAPI server and the read-model export consume identical store wiring;
keeping every store path literal here prevents the two composition points from
drifting apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from baibai_engine.read_api import (
    APPLICATION_DB_PATH,
    ER_LEVEL_CALIBRATION_CONTEXT_PATH,
    INDICATORS_DB_PATH,
    MARKET_DB_PATH,
    READING_RULES_PATH,
    RUNS_DB_PATH,
    screening_calibration_method_identity,
    validate_application_store_schema,
    validate_market_store_schema,
)
from baibai_engine.read_api.er_calibration_context import ErCalibrationContextArtifact
from baibai_web.repository_layout import INDICATOR_PANEL_CONFIG_PATH
from baibai_web.sources.calibration_context import load_er_level_calibration_context
from baibai_web.sources.db_sources import (
    DbExternalAssessmentSource,
    DbLedgerSource,
    DbMarketContextSource,
    DbMarketPriceSource,
    DbMetaSource,
    DbOperationsSource,
    DbResearchSource,
    DbScreeningSource,
    DbTaskSource,
    load_indicator_panel_config,
)
from baibai_web.sources.types import IndicatorGroupConfig


@dataclass(frozen=True, slots=True)
class Sources:
    """DB-backed application sources wired for one repository root."""

    external_assessment: DbExternalAssessmentSource
    ledger: DbLedgerSource
    research: DbResearchSource
    tasks: DbTaskSource
    screening: DbScreeningSource
    market_context: DbMarketContextSource
    operations: DbOperationsSource
    market: DbMarketPriceSource
    meta: DbMetaSource
    er_level_calibration: ErCalibrationContextArtifact | None
    app_db_path: Path
    runs_db_path: Path
    market_db_path: Path


def load_indicator_groups(root: Path) -> tuple[IndicatorGroupConfig, ...]:
    """Load the indicator panel config from its canonical repository location."""

    return load_indicator_panel_config(root / INDICATOR_PANEL_CONFIG_PATH)


def build_sources(
    root: Path,
    *,
    db_path: Path | None = None,
    runs_db_path: Path | None = None,
    indicator_groups: tuple[IndicatorGroupConfig, ...] | None = None,
) -> Sources:
    """Wire every application source for ``root``.

    ``db_path`` / ``runs_db_path`` override the repository defaults. ``indicator_groups``
    lets a caller reuse an already-loaded dashboard config (the FastAPI app loads it
    once at startup and fails fast on a bad file); by default the config is read
    from the repository.
    """

    resolved_db = (db_path or root / APPLICATION_DB_PATH).resolve()
    resolved_runs = (runs_db_path or root / RUNS_DB_PATH).resolve()
    resolved_market = (root / MARKET_DB_PATH).resolve()
    indicators_db = root / INDICATORS_DB_PATH
    groups = indicator_groups if indicator_groups is not None else load_indicator_groups(root)
    validate_application_store_schema(resolved_db)
    validate_market_store_schema(resolved_market)
    screening = DbScreeningSource(resolved_runs)
    return Sources(
        external_assessment=DbExternalAssessmentSource(resolved_market),
        ledger=DbLedgerSource(resolved_db, resolved_market),
        research=DbResearchSource(resolved_db),
        tasks=DbTaskSource(resolved_db),
        screening=screening,
        market_context=DbMarketContextSource(
            resolved_db, indicators_db, groups, root / READING_RULES_PATH
        ),
        operations=DbOperationsSource(resolved_db),
        market=DbMarketPriceSource(resolved_market),
        meta=DbMetaSource(resolved_db, screening, indicators_db),
        er_level_calibration=load_er_level_calibration_context(
            root / ER_LEVEL_CALIBRATION_CONTEXT_PATH,
            expected_method_identity=screening_calibration_method_identity(root),
        ),
        app_db_path=resolved_db,
        runs_db_path=resolved_runs,
        market_db_path=resolved_market,
    )
