"""Single wiring point for repository-rooted application sources.

The FastAPI server and the read-model export consume identical store wiring;
keeping every store path literal here prevents the two composition points from
drifting apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from baibai_app.sources.db_sources import (
    DbCandidatesSource,
    DbLedgerSource,
    DbMacroSource,
    DbMarketPriceSource,
    DbMetaSource,
    DbOperationsSource,
    DbResearchSource,
    DbTaskSource,
    load_macro_panel_config,
)
from baibai_app.sources.types import MacroGroupConfig
from baibai_engine.read_api import MACRO_READING_RULES_PATH

_APP_DB = "data/app/baibai.sqlite"
_RUNS_DB = "data/screening/runs.sqlite"
_INDICATORS_DB = "data/indicators/macro.sqlite"
_MARKET_DB = "data/screening/market.sqlite"
_MACRO_PANEL_CONFIG = "method/macro-panel.yaml"


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
    runs_db_path: Path


def load_macro_groups(root: Path) -> tuple[MacroGroupConfig, ...]:
    """Load the macro panel config from its canonical repository location."""

    return load_macro_panel_config(root / _MACRO_PANEL_CONFIG)


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

    resolved_db = (db_path or root / _APP_DB).resolve()
    resolved_runs = (runs_db_path or root / _RUNS_DB).resolve()
    indicators_db = root / _INDICATORS_DB
    groups = macro_groups if macro_groups is not None else load_macro_groups(root)
    return Sources(
        ledger=DbLedgerSource(resolved_db),
        research=DbResearchSource(resolved_db),
        tasks=DbTaskSource(resolved_db),
        candidates=DbCandidatesSource(resolved_runs, resolved_db),
        macro=DbMacroSource(
            resolved_db, indicators_db, groups, root / MACRO_READING_RULES_PATH
        ),
        operations=DbOperationsSource(resolved_db),
        market=DbMarketPriceSource(root / _MARKET_DB),
        meta=DbMetaSource(resolved_db, resolved_runs, indicators_db),
        runs_db_path=resolved_runs,
    )
