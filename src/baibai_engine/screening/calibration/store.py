"""Versioned local cache for point-in-time panel and forward observations."""

from __future__ import annotations

import csv
from collections.abc import Mapping
from dataclasses import asdict, fields
from datetime import date
from pathlib import Path

import yaml

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.market.config import DEFAULT_SQLITE_CACHE_DIR

from .forward import ForwardReturnRow
from .panel import PanelDiagnostics, PanelRow

DEFAULT_CALIBRATION_DIR = DEFAULT_SQLITE_CACHE_DIR / "calibration"
CACHE_SCHEMA_VERSION = 2

_BOOL_TRUE = "true"
_BOOL_FALSE = "false"


class CalibrationCacheError(RuntimeError):
    """The local cache cannot prove that it uses the current contract."""


def cache_meta_path(root: Path) -> Path:
    return root / "calibration.meta.yaml"


def _write_cache_meta(root: Path) -> None:
    cache_meta_path(root).write_text(
        yaml.safe_dump({"cache_schema_version": CACHE_SCHEMA_VERSION}, sort_keys=False),
        encoding="utf-8",
    )


def _require_current_cache(root: Path) -> None:
    path = cache_meta_path(root)
    if not path.exists():
        raise CalibrationCacheError(
            "calibration cache version is missing; run calibration-build --force"
        )
    try:
        payload = safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CalibrationCacheError(
            "calibration cache version is invalid; run calibration-build --force"
        ) from exc
    version = payload.get("cache_schema_version") if isinstance(payload, dict) else None
    if version != CACHE_SCHEMA_VERSION:
        raise CalibrationCacheError(
            "calibration cache version is incompatible; run calibration-build --force"
        )


def panel_path(root: Path, asof: date) -> Path:
    return root / f"panel-{asof.isoformat()}.csv"


def panel_meta_path(root: Path, asof: date) -> Path:
    return root / f"panel-{asof.isoformat()}.meta.yaml"


def forward_path(root: Path, asof: date) -> Path:
    return root / f"forward-{asof.isoformat()}.csv"


def write_panel(
    root: Path, asof: date, rows: tuple[PanelRow, ...], diagnostics: PanelDiagnostics
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _write_cache_meta(root)
    _write_rows(panel_path(root, asof), [asdict(row) for row in rows], PanelRow)
    panel_meta_path(root, asof).write_text(
        yaml.safe_dump(asdict(diagnostics), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def read_panel_meta(root: Path, asof: date) -> dict[str, object]:
    _require_current_cache(root)
    path = panel_meta_path(root, asof)
    if not path.exists():
        raise CalibrationCacheError("calibration cache is partial; run calibration-build --force")
    try:
        payload = safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CalibrationCacheError(
            "calibration cache metadata is invalid; run calibration-build --force"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("rules_hash"), str):
        raise CalibrationCacheError(
            "calibration cache metadata is invalid; run calibration-build --force"
        )
    return payload


def read_panel(root: Path, asof: date) -> list[PanelRow]:
    _require_current_cache(root)
    path = panel_path(root, asof)
    if not path.exists():
        raise CalibrationCacheError("calibration cache is partial; run calibration-build --force")
    try:
        return [_panel_row_from_csv(raw) for raw in _read_rows(path, PanelRow)]
    except (KeyError, ValueError, csv.Error) as exc:
        raise CalibrationCacheError(
            "calibration panel cache is invalid; run calibration-build --force"
        ) from exc


def write_forward(root: Path, asof: date, rows: list[ForwardReturnRow]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _write_cache_meta(root)
    _write_rows(forward_path(root, asof), [asdict(row) for row in rows], ForwardReturnRow)


def read_forward(root: Path, asof: date) -> list[ForwardReturnRow]:
    _require_current_cache(root)
    path = forward_path(root, asof)
    if not path.exists():
        raise CalibrationCacheError("calibration cache is partial; run calibration-build --force")
    try:
        return [_forward_row_from_csv(raw) for raw in _read_rows(path, ForwardReturnRow)]
    except (KeyError, ValueError, csv.Error) as exc:
        raise CalibrationCacheError(
            "calibration forward cache is invalid; run calibration-build --force"
        ) from exc


def _panel_row_from_csv(raw: Mapping[str, str]) -> PanelRow:
    return PanelRow(
        asof=raw["asof"],
        ticker=raw["ticker"],
        sector_33=raw["sector_33"],
        in_population=raw["in_population"] == _BOOL_TRUE,
        market_cap_oku=_opt_float(raw, "market_cap_oku"),
        avg_turnover_oku=_opt_float(raw, "avg_turnover_oku"),
        listing_span_days=_opt_int(raw, "listing_span_days"),
        close=_opt_float(raw, "close"),
        per_forward=_opt_float(raw, "per_forward"),
        per_trailing=_opt_float(raw, "per_trailing"),
        pbr=_opt_float(raw, "pbr"),
        ev_ebitda=_opt_float(raw, "ev_ebitda"),
        p_s=_opt_float(raw, "p_s"),
        pcfr=_opt_float(raw, "pcfr"),
        ocf_yield=_opt_float(raw, "ocf_yield"),
        fcf_yield=_opt_float(raw, "fcf_yield"),
        net_cash_to_market_cap=_opt_float(raw, "net_cash_to_market_cap"),
        cash_to_market_cap=_opt_float(raw, "cash_to_market_cap"),
        equity_ratio=_opt_float(raw, "equity_ratio"),
        price_to_equity=_opt_float(raw, "price_to_equity"),
        dividend_yield=_opt_float(raw, "dividend_yield"),
        eps_yoy=_opt_float(raw, "eps_yoy"),
        sales_yoy=_opt_float(raw, "sales_yoy"),
        operating_profit_yoy=_opt_float(raw, "operating_profit_yoy"),
        cfo_yoy=_opt_float(raw, "cfo_yoy"),
        accruals_to_assets=_opt_float(raw, "accruals_to_assets"),
        net_share_change_yoy=_opt_float(raw, "net_share_change_yoy"),
        ttm_quality_per_trailing=raw["ttm_quality_per_trailing"],
        ttm_quality_ocf_yield=raw["ttm_quality_ocf_yield"],
        price_change_60d=_opt_float(raw, "price_change_60d"),
        gap_from_52w_low=_opt_float(raw, "gap_from_52w_low"),
        price_history_coverage_750d=_opt_float(raw, "price_history_coverage_750d"),
        smg_per_forward=_opt_float(raw, "smg_per_forward"),
        smg_per_trailing=_opt_float(raw, "smg_per_trailing"),
        smg_pbr=_opt_float(raw, "smg_pbr"),
        smg_ev_ebitda=_opt_float(raw, "smg_ev_ebitda"),
        smg_p_s=_opt_float(raw, "smg_p_s"),
        srp_per_forward=_opt_float(raw, "srp_per_forward"),
        srp_per_trailing=_opt_float(raw, "srp_per_trailing"),
        srp_pbr=_opt_float(raw, "srp_pbr"),
        srp_ev_ebitda=_opt_float(raw, "srp_ev_ebitda"),
        srp_p_s=_opt_float(raw, "srp_p_s"),
        er_annual=_opt_float(raw, "er_annual"),
        er_reversion_annual=_opt_float(raw, "er_reversion_annual"),
        er_carry_annual=_opt_float(raw, "er_carry_annual"),
        er_upside_capped=_opt_float(raw, "er_upside_capped"),
        pass_screen=raw["pass_screen"] == _BOOL_TRUE,
        evidence_playbooks=raw["evidence_playbooks"],
        selection_rank=_opt_int(raw, "selection_rank"),
        recommended_rank=_opt_int(raw, "recommended_rank"),
    )


def _forward_row_from_csv(raw: Mapping[str, str]) -> ForwardReturnRow:
    return ForwardReturnRow(
        asof=raw["asof"],
        ticker=raw["ticker"],
        horizon=raw["horizon"],
        target_date=raw["target_date"],
        resolved=raw["resolved"] == _BOOL_TRUE,
        price_return=_opt_float(raw, "price_return"),
        stale_price=raw["stale_price"] == _BOOL_TRUE,
        entry_date=raw["entry_date"] or None,
        exit_date=raw["exit_date"] or None,
        status=str(raw["status"]),
        delisting_coverage_status=raw["delisting_coverage_status"],
        corporate_action_event_coverage_status=raw["corporate_action_event_coverage_status"],
        survivorship_coverage_status=raw["survivorship_coverage_status"],
        adjustment_factor_coverage=raw["adjustment_factor_coverage"],
    )


def _opt_float(raw: Mapping[str, str], key: str) -> float | None:
    text = raw.get(key, "")
    return float(text) if text else None


def _opt_int(raw: Mapping[str, str], key: str) -> int | None:
    text = raw.get(key, "")
    return int(text) if text else None


def _write_rows(
    path: Path, rows: list[dict[str, object]], row_type: type[PanelRow] | type[ForwardReturnRow]
) -> None:
    names = [field.name for field in fields(row_type)]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(names)
        for row in rows:
            writer.writerow([_encode(row[name]) for name in names])


def _read_rows(
    path: Path, row_type: type[PanelRow] | type[ForwardReturnRow]
) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        expected_names = [field.name for field in fields(row_type)]
        if reader.fieldnames != expected_names:
            raise CalibrationCacheError(
                "calibration cache schema is invalid; run calibration-build --force"
            )
        return [dict(raw) for raw in reader]


def _encode(value: object) -> str:
    if value is None:
        return ""
    if value is True:
        return _BOOL_TRUE
    if value is False:
        return _BOOL_FALSE
    if isinstance(value, float):
        return repr(value)
    return str(value)
