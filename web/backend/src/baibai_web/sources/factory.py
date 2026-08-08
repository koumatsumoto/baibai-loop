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
    MACRO_DB_PATH,
    MACRO_READING_RULES_PATH,
    MARKET_DB_PATH,
    RUNS_DB_PATH,
    screening_calibration_method_identity,
)
from baibai_web.repository_layout import (
    ER_LEVEL_CALIBRATION_CONTEXT_PATH,
    MACRO_PANEL_CONFIG_PATH,
)
from baibai_web.sources.calibration_context import load_er_level_calibration_context
from baibai_web.sources.db_sources import (
    DbCandidatesSource,
    DbLedgerSource,
    DbMacroSource,
    DbMarketPriceSource,
    DbMetaSource,
    DbOperationsSource,
    DbResearchSource,
    DbSystemSource,
    DbTaskSource,
    load_macro_panel_config,
)
from baibai_web.sources.types import ErLevelCalibrationContext, MacroGroupConfig


@dataclass(frozen=True, slots=True)
class Sources:
    """DB-backed application sources wired for one repository root."""

    ledger: DbLedgerSource
    research: DbResearchSource
    tasks: DbTaskSource
    candidates: DbCandidatesSource
    macro: DbMacroSource
    operations: DbOperationsSource
    market: DbMarketPriceSource
    meta: DbMetaSource
    system: DbSystemSource
    er_level_calibration: ErLevelCalibrationContext | None
    app_db_path: Path
    runs_db_path: Path


def load_macro_groups(root: Path) -> tuple[MacroGroupConfig, ...]:
    """Load the macro panel config from its canonical repository location."""

    return load_macro_panel_config(root / MACRO_PANEL_CONFIG_PATH)


def build_sources(
    root: Path,
    *,
    db_path: Path | None = None,
    runs_db_path: Path | None = None,
    macro_groups: tuple[MacroGroupConfig, ...] | None = None,
) -> Sources:
    """Wire every application source for ``root``.

    ``db_path`` / ``runs_db_path`` override the repository defaults. ``macro_groups``
    lets a caller reuse an already-loaded dashboard config (the FastAPI app loads it
    once at startup and fails fast on a bad file); by default the config is read
    from the repository.
    """

    resolved_db = (db_path or root / APPLICATION_DB_PATH).resolve()
    resolved_runs = (runs_db_path or root / RUNS_DB_PATH).resolve()
    indicators_db = root / MACRO_DB_PATH
    groups = macro_groups if macro_groups is not None else load_macro_groups(root)
    return Sources(
        ledger=DbLedgerSource(resolved_db),
        research=DbResearchSource(resolved_db),
        tasks=DbTaskSource(resolved_db),
        candidates=DbCandidatesSource(resolved_runs, resolved_db),
        macro=DbMacroSource(resolved_db, indicators_db, groups, root / MACRO_READING_RULES_PATH),
        operations=DbOperationsSource(resolved_db),
        market=DbMarketPriceSource(root / MARKET_DB_PATH),
        meta=DbMetaSource(resolved_db, resolved_runs, indicators_db),
        system=DbSystemSource(resolved_db, resolved_runs, indicators_db, root / MARKET_DB_PATH),
        er_level_calibration=load_er_level_calibration_context(
            root / ER_LEVEL_CALIBRATION_CONTEXT_PATH,
            expected_method_identity=screening_calibration_method_identity(root),
        ),
        app_db_path=resolved_db,
        runs_db_path=resolved_runs,
    )
