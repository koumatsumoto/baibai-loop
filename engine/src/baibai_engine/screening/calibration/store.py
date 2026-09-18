"""Measure L1 estimate quality in one atomically replaceable current snapshot."""

from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import date
from hashlib import sha256
from math import isfinite
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from baibai_engine.appdb.json import canonical_json
from baibai_engine.foundation.repository_layout import CALIBRATION_DIR

from ..metrics import VALUATION_CALCULATION_REVISION
from .forward import (
    DEFAULT_FORWARD_OBSERVATION_POLICY,
    FORWARD_FIELD_NAMES,
    RESOLVED_STATUSES,
    TOTAL_RETURN_BASIS,
    TOTAL_RETURN_STATUSES,
    ForwardObservationPolicy,
    ForwardReturnRow,
)
from .panel import (
    DIAGNOSTIC_FIELD_NAMES,
    PANEL_FIELD_NAMES,
    PanelDiagnostics,
    PanelRow,
    PopulationCoverageStatus,
)

DEFAULT_CALIBRATION_DIR = CALIBRATION_DIR
CURRENT_SNAPSHOT_NAME = "current.sqlite"
RELAXED: tuple[()] = ()


def _digest(*parts: str) -> str:
    return sha256("|".join(parts).encode()).hexdigest()[:16]


def _derive_cache_schema_version() -> str:
    return _digest(
        ",".join(PANEL_FIELD_NAMES),
        ",".join(DIAGNOSTIC_FIELD_NAMES),
        ",".join(FORWARD_FIELD_NAMES),
        VALUATION_CALCULATION_REVISION,
        repr(RELAXED),
    )


CACHE_SCHEMA_VERSION = _derive_cache_schema_version()

_SCHEMA_SQL = """
CREATE TABLE snapshot_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    contract_version TEXT NOT NULL
) STRICT;
CREATE TABLE cohort (
    asof TEXT PRIMARY KEY,
    diagnostics TEXT NOT NULL CHECK (json_valid(diagnostics)),
    forward_policy TEXT NOT NULL CHECK (json_valid(forward_policy)),
    forward_ready INTEGER NOT NULL CHECK (forward_ready IN (0, 1))
) STRICT;
CREATE TABLE panel_row (
    asof TEXT NOT NULL REFERENCES cohort(asof) ON DELETE CASCADE,
    ticker TEXT NOT NULL,
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    PRIMARY KEY (asof, ticker)
) STRICT, WITHOUT ROWID;
CREATE TABLE forward_row (
    asof TEXT NOT NULL REFERENCES cohort(asof) ON DELETE CASCADE,
    ticker TEXT NOT NULL,
    horizon TEXT NOT NULL,
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    PRIMARY KEY (asof, ticker, horizon)
) STRICT, WITHOUT ROWID;
"""

_POPULATION_COVERAGE_STATUSES = {
    "evaluated",
    "priced_master_without_universe",
    "master_without_universe_unpriced",
}
_CANDIDATE_DISCOVERY_INPUT_STATUSES = {"complete", "incomplete", "unavailable"}


class CalibrationCacheError(RuntimeError):
    """The current calibration snapshot is absent, partial, or incoherent."""


def _path(root: Path) -> Path:
    return root / CURRENT_SNAPSHOT_NAME


def _connect(root: Path, *, create: bool) -> sqlite3.Connection:
    path = _path(root)
    if not path.exists() and not create:
        raise CalibrationCacheError(f"calibration snapshot is missing: {path}")
    root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    tables = connection.execute(
        "SELECT count(*) FROM sqlite_schema WHERE type = 'table' AND name = 'snapshot_meta'"
    ).fetchone()[0]
    if not tables:
        if not create:
            connection.close()
            raise CalibrationCacheError("calibration snapshot has an obsolete schema")
        connection.executescript(_SCHEMA_SQL)
        connection.execute(
            "INSERT INTO snapshot_meta (singleton, contract_version) VALUES (1, ?)",
            (CACHE_SCHEMA_VERSION,),
        )
        connection.commit()
    version = connection.execute(
        "SELECT contract_version FROM snapshot_meta WHERE singleton = 1"
    ).fetchone()
    if version is None or version[0] != CACHE_SCHEMA_VERSION:
        connection.close()
        raise CalibrationCacheError("calibration snapshot contract changed; rebuild it")
    return connection


def copy_current_snapshot(source: Path, target: Path) -> None:
    """Seed a work snapshot from current without exposing partial writes."""
    source_path = _path(source)
    if not source_path.exists():
        return
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, _path(target))


@contextmanager
def fixed_current_snapshot(root: Path) -> Iterator[Path]:
    """Give one reader a stable copy of the current atomic snapshot.

    Publication replaces ``current.sqlite`` as one filesystem operation, but a reader
    opens the file once per cohort. Copying the immutable file at entry keeps a
    publication between those opens from mixing two snapshots in one measurement.
    """
    with TemporaryDirectory(prefix="calibration-read-") as raw:
        fixed = Path(raw)
        copy_current_snapshot(root, fixed)
        if not _path(fixed).exists():
            raise CalibrationCacheError(f"calibration snapshot is missing: {_path(root)}")
        with closing(_connect(fixed, create=False)) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise CalibrationCacheError("calibration snapshot integrity_check failed")
        yield fixed


def publish_current_snapshot(root: Path, built_root: Path) -> None:
    """Validate one coherent build and atomically replace current.sqlite."""
    built = _path(built_root)
    with closing(_connect(built_root, create=False)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise CalibrationCacheError("calibration snapshot integrity_check failed")
        rows = connection.execute("SELECT diagnostics FROM cohort ORDER BY asof").fetchall()
        if not rows:
            raise CalibrationCacheError("calibration snapshot has no cohorts")
        rules = {json.loads(row[0]).get("rules_hash") for row in rows}
        if len(rules) != 1 or None in rules:
            raise CalibrationCacheError("calibration snapshot mixes measurement methods")
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / f".{CURRENT_SNAPSHOT_NAME}.{uuid.uuid4().hex}.tmp"
    shutil.copy2(built, temporary)
    temporary.replace(_path(root))


def write_panel(
    root: Path,
    asof: date,
    rows: tuple[PanelRow, ...],
    diagnostics: PanelDiagnostics,
    *,
    forward_policy: ForwardObservationPolicy = DEFAULT_FORWARD_OBSERVATION_POLICY,
) -> None:
    if diagnostics.asof != asof.isoformat() or any(row.asof != asof.isoformat() for row in rows):
        raise CalibrationCacheError("panel cohort as-of differs from its rows")
    for row in rows:
        panel_row_from_mapping(asdict(row))
    with closing(_connect(root, create=True)) as connection:
        existing_rules = {
            json.loads(item[0]).get("rules_hash")
            for item in connection.execute(
                "SELECT diagnostics FROM cohort WHERE asof <> ?", (asof.isoformat(),)
            )
        }
        if existing_rules and existing_rules != {diagnostics.rules_hash}:
            raise CalibrationCacheError("calibration snapshot cannot mix measurement policies")
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("DELETE FROM cohort WHERE asof = ?", (asof.isoformat(),))
            connection.execute(
                "INSERT INTO cohort "
                "(asof, diagnostics, forward_policy, forward_ready) VALUES (?, ?, ?, 0)",
                (
                    asof.isoformat(),
                    canonical_json(asdict(diagnostics)),
                    canonical_json(asdict(forward_policy)),
                ),
            )
            connection.executemany(
                "INSERT INTO panel_row (asof, ticker, payload) VALUES (?, ?, ?)",
                ((asof.isoformat(), row.ticker, canonical_json(asdict(row))) for row in rows),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


def write_forward(
    root: Path,
    asof: date,
    rows: list[ForwardReturnRow],
    *,
    forward_policy: ForwardObservationPolicy = DEFAULT_FORWARD_OBSERVATION_POLICY,
) -> None:
    for row in rows:
        if row.asof != asof.isoformat():
            raise CalibrationCacheError("forward cohort as-of differs from its rows")
        forward_row_from_mapping(asdict(row))
    with closing(_connect(root, create=False)) as connection:
        cohort = connection.execute(
            "SELECT forward_policy FROM cohort WHERE asof = ?", (asof.isoformat(),)
        ).fetchone()
        if cohort is None:
            raise CalibrationCacheError("forward cohort has no panel")
        if json.loads(cohort[0]) != json.loads(canonical_json(asdict(forward_policy))):
            raise CalibrationCacheError("forward observation policy differs from its panel")
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("DELETE FROM forward_row WHERE asof = ?", (asof.isoformat(),))
            connection.executemany(
                "INSERT INTO forward_row (asof, ticker, horizon, payload) VALUES (?, ?, ?, ?)",
                (
                    (asof.isoformat(), row.ticker, row.horizon, canonical_json(asdict(row)))
                    for row in rows
                ),
            )
            connection.execute(
                "UPDATE cohort SET forward_ready = 1 WHERE asof = ?", (asof.isoformat(),)
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


def has_cohort(root: Path, asof: date) -> bool:
    if not _path(root).exists():
        return False
    with closing(_connect(root, create=False)) as connection:
        return (
            connection.execute(
                "SELECT 1 FROM cohort WHERE asof = ?", (asof.isoformat(),)
            ).fetchone()
            is not None
        )


def published_cohorts(root: Path) -> list[date]:
    if not _path(root).exists():
        return []
    with closing(_connect(root, create=False)) as connection:
        return [
            date.fromisoformat(row[0])
            for row in connection.execute("SELECT asof FROM cohort ORDER BY asof")
        ]


def read_panel(root: Path, asof: date) -> list[PanelRow]:
    with closing(_connect(root, create=False)) as connection:
        rows = connection.execute(
            "SELECT payload FROM panel_row WHERE asof = ? ORDER BY ticker", (asof.isoformat(),)
        ).fetchall()
        exists = connection.execute(
            "SELECT 1 FROM cohort WHERE asof = ?", (asof.isoformat(),)
        ).fetchone()
    if exists is None:
        raise CalibrationCacheError("calibration panel cohort is missing")
    return [panel_row_from_mapping(json.loads(row[0])) for row in rows]


def read_panel_meta(root: Path, asof: date) -> dict[str, object]:
    with closing(_connect(root, create=False)) as connection:
        row = connection.execute(
            "SELECT diagnostics FROM cohort WHERE asof = ?", (asof.isoformat(),)
        ).fetchone()
    if row is None:
        raise CalibrationCacheError("calibration diagnostics cohort is missing")
    payload = json.loads(row[0])
    return validate_panel_meta(payload)


def validate_panel_meta(payload: object) -> dict[str, object]:
    """Shared diagnostics structure for the writer's consumers and query-only readers."""
    if not isinstance(payload, dict) or not isinstance(payload.get("rules_hash"), str):
        raise CalibrationCacheError("calibration diagnostics are invalid")
    if (
        payload.get("candidate_discovery_jpx_input_status")
        not in _CANDIDATE_DISCOVERY_INPUT_STATUSES
    ):
        raise CalibrationCacheError("calibration Candidate Discovery input status is invalid")
    return payload


def read_forward(root: Path, asof: date) -> list[ForwardReturnRow]:
    with closing(_connect(root, create=False)) as connection:
        cohort = connection.execute(
            "SELECT forward_ready FROM cohort WHERE asof = ?", (asof.isoformat(),)
        ).fetchone()
        rows = connection.execute(
            "SELECT payload FROM forward_row WHERE asof = ? ORDER BY ticker, horizon",
            (asof.isoformat(),),
        ).fetchall()
    if cohort is None:
        raise CalibrationCacheError("calibration forward cohort is missing")
    if cohort[0] != 1:
        raise CalibrationCacheError("calibration snapshot is partial; rebuild it")
    return [forward_row_from_mapping(json.loads(row[0])) for row in rows]


def panel_row_from_mapping(raw: Mapping[str, object]) -> PanelRow:
    row = PanelRow(**cast(dict[str, object], dict(raw)))  # type: ignore[arg-type]
    _population_coverage_status(str(row.population_coverage_status))
    _validate_asset_backed(row)
    _validate_shareholder_return_change(row)
    _validate_margin_supply_demand(row)
    _validate_normalized_profit(row)
    _validate_profitability_levels(row)
    return row


def forward_row_from_mapping(raw: Mapping[str, object]) -> ForwardReturnRow:
    row = ForwardReturnRow(**cast(dict[str, object], dict(raw)))  # type: ignore[arg-type]
    _validate_total_return_contract(row)
    return row


def _validate_total_return_contract(row: ForwardReturnRow) -> None:
    if row.resolved is not (row.status in RESOLVED_STATUSES):
        raise ValueError(f"resolved flag disagrees with status: {row.status!r}")
    if row.total_return_basis != TOTAL_RETURN_BASIS:
        raise ValueError(f"invalid total return basis: {row.total_return_basis!r}")
    if row.total_return_status not in TOTAL_RETURN_STATUSES:
        raise ValueError(f"invalid total return status: {row.total_return_status!r}")
    if row.total_return_status == "resolved":
        values = (row.price_return, row.realized_dividend_sum, row.total_return)
        if (
            not row.resolved
            or row.realized_dividend_fy_count <= 0
            or any(value is None or not isfinite(value) for value in values)
            or (row.realized_dividend_sum or 0.0) < 0
            or (row.total_return or 0.0) < -1
            or (row.total_return or 0.0) < (row.price_return or 0.0)
        ):
            raise ValueError("resolved total return fields are inconsistent")
    elif (
        row.realized_dividend_sum is not None
        or row.realized_dividend_fy_count != 0
        or row.total_return is not None
    ):
        raise ValueError("unresolved total return carries resolved values")


def _validate_asset_backed(row: PanelRow) -> None:
    investment = row.investment_securities
    if investment is not None and (not isfinite(investment) or investment < 0):
        raise ValueError("investment securities must be finite and non-negative")
    ratio = row.asset_backed_ratio
    if ratio is None:
        return
    if not isfinite(ratio):
        raise ValueError("asset-backed ratio must be finite")
    if investment is None or row.net_cash_to_market_cap is None:
        raise ValueError("asset-backed ratio requires its source fields")
    if row.market_cap_oku is None:
        if row.in_population:
            raise ValueError("population asset-backed ratio requires market cap")
        if ratio < row.net_cash_to_market_cap - 1e-12:
            raise ValueError("asset-backed ratio is inconsistent with non-negative investment")
        return
    if row.market_cap_oku <= 0:
        raise ValueError("asset-backed ratio requires positive market cap")
    component = ratio - row.net_cash_to_market_cap
    lower_market_cap = max((row.market_cap_oku - 0.5) * 100_000_000, 1.0)
    upper_market_cap = (row.market_cap_oku + 0.5) * 100_000_000
    component_min = investment / upper_market_cap
    component_max = investment / lower_market_cap
    tolerance = 1e-12 * max(1.0, abs(component), abs(component_max))
    if component < component_min - tolerance or component > component_max + tolerance:
        raise ValueError("asset-backed ratio is inconsistent with its source fields")


def _validate_shareholder_return_change(row: PanelRow) -> None:
    if row.dps_yoy_latest is not None and not isfinite(row.dps_yoy_latest):
        raise ValueError("DPS YoY must be finite")
    streak = row.share_count_reduction_streak
    if streak is not None and streak not in {0, 1, 2}:
        raise ValueError("share count reduction streak must be between zero and two")
    positive = (
        (row.dps_yoy_latest is not None and row.dps_yoy_latest > 0)
        or row.dps_guidance_up is True
        or row.dividend_initiation is True
        or (streak is not None and streak >= 1)
    )
    negative = (
        row.dps_yoy_latest is not None
        and row.dps_yoy_latest <= 0
        and row.dps_guidance_up is False
        and row.dividend_initiation is False
        and streak == 0
    )
    expected = True if positive else False if negative else None
    if row.shareholder_return_change is not expected:
        raise ValueError("shareholder return change is inconsistent with its components")


def _validate_margin_supply_demand(row: PanelRow) -> None:
    if row.margin_short_to_adv is not None and (
        not isfinite(row.margin_short_to_adv)
        or row.margin_short_to_adv < 0
        or row.margin_week_end is None
    ):
        raise ValueError("margin short to ADV requires a dated non-negative value")
    if row.realized_volatility_60d is not None and (
        not isfinite(row.realized_volatility_60d) or row.realized_volatility_60d < 0
    ):
        raise ValueError("realized volatility must be finite and non-negative")


def _validate_normalized_profit(row: PanelRow) -> None:
    for value in (row.normalized_per_3fy, row.normalized_per_5fy):
        if value is not None and (not isfinite(value) or value <= 0):
            raise ValueError("normalized PER must be finite and positive")
    if row.self_range_observed_sessions < 0:
        raise ValueError("self-range observed sessions must be non-negative")


def _validate_profitability_levels(row: PanelRow) -> None:
    levels = (row.operating_profit_to_assets, row.operating_margin, row.asset_turnover)
    if any(value is not None and not isfinite(value) for value in levels):
        raise ValueError("profitability levels must be finite")
    if all(value is not None for value in levels):
        assert row.operating_margin is not None
        assert row.asset_turnover is not None
        assert row.operating_profit_to_assets is not None
        expected = row.operating_margin * row.asset_turnover
        if not abs(row.operating_profit_to_assets - expected) <= 1e-12 * max(1.0, abs(expected)):
            raise ValueError("profitability levels violate the accounting identity")


def _population_coverage_status(value: str) -> PopulationCoverageStatus:
    if value not in _POPULATION_COVERAGE_STATUSES:
        raise ValueError(f"invalid population coverage status: {value!r}")
    return cast(PopulationCoverageStatus, value)


__all__ = [
    "CACHE_SCHEMA_VERSION",
    "CURRENT_SNAPSHOT_NAME",
    "DEFAULT_CALIBRATION_DIR",
    "CalibrationCacheError",
    "copy_current_snapshot",
    "fixed_current_snapshot",
    "has_cohort",
    "panel_row_from_mapping",
    "publish_current_snapshot",
    "published_cohorts",
    "read_forward",
    "read_panel",
    "read_panel_meta",
    "write_forward",
    "write_panel",
]
