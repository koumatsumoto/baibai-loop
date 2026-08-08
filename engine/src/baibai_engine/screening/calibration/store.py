"""Versioned local cache for point-in-time panel and forward observations."""

from __future__ import annotations

import csv
from collections.abc import Mapping
from dataclasses import asdict, fields
from datetime import date
from math import isfinite
from pathlib import Path
from typing import cast

import yaml

from baibai_engine.foundation.repository_layout import CALIBRATION_DIR
from baibai_engine.foundation.yaml_io import safe_load

from .forward import TOTAL_RETURN_BASIS, TOTAL_RETURN_STATUSES, ForwardReturnRow
from .panel import PanelDiagnostics, PanelRow, PopulationCoverageStatus

DEFAULT_CALIBRATION_DIR = CALIBRATION_DIR
CACHE_SCHEMA_VERSION = 12

_BOOL_TRUE = "true"
_BOOL_FALSE = "false"
_POPULATION_COVERAGE_STATUSES = {
    "evaluated",
    "priced_master_without_universe",
    "master_without_universe_unpriced",
}


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
    row = PanelRow(
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
        investment_securities=_opt_float(raw, "investment_securities"),
        asset_backed_ratio=_opt_float(raw, "asset_backed_ratio"),
        equity_ratio=_opt_float(raw, "equity_ratio"),
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
        margin_week_end=raw.get("margin_week_end") or None,
        margin_long_to_adv=_opt_float(raw, "margin_long_to_adv"),
        margin_long_share=_opt_float(raw, "margin_long_share"),
        margin_long_delta_26w=_opt_float(raw, "margin_long_delta_26w"),
        margin_std_long_share=_opt_float(raw, "margin_std_long_share"),
        pass_screen=raw["pass_screen"] == _BOOL_TRUE,
        evidence_playbooks=raw["evidence_playbooks"],
        selection_rank=_opt_int(raw, "selection_rank"),
        recommended_rank=_opt_int(raw, "recommended_rank"),
        population_coverage_status=_population_coverage_status(raw["population_coverage_status"]),
        self_range_degraded=raw["self_range_degraded"] == _BOOL_TRUE,
        dps_streak_up=_opt_bool(raw, "dps_streak_up"),
        dps_yoy_latest=_opt_float(raw, "dps_yoy_latest"),
        dps_guidance_up=_opt_bool(raw, "dps_guidance_up"),
        dividend_initiation=_opt_bool(raw, "dividend_initiation"),
        share_count_reduction_streak=_opt_int(raw, "share_count_reduction_streak"),
        shareholder_return_change=_opt_bool(raw, "shareholder_return_change"),
        margin_short_to_adv=_opt_float(raw, "margin_short_to_adv"),
        realized_volatility_60d=_opt_float(raw, "realized_volatility_60d"),
        normalized_per_3fy=_opt_float(raw, "normalized_per_3fy"),
        normalized_per_5fy=_opt_float(raw, "normalized_per_5fy"),
        self_range_observed_sessions=int(raw["self_range_observed_sessions"]),
    )
    _validate_asset_backed(row)
    _validate_shareholder_return_change(row)
    _validate_margin_supply_demand(row)
    _validate_normalized_profit(row)
    return row


def _forward_row_from_csv(raw: Mapping[str, str]) -> ForwardReturnRow:
    row = ForwardReturnRow(
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
        adjustment_factor_coverage=raw["adjustment_factor_coverage"],
        realized_dividend_sum=_opt_float(raw, "realized_dividend_sum"),
        realized_dividend_fy_count=_opt_int(raw, "realized_dividend_fy_count") or 0,
        total_return=_opt_float(raw, "total_return"),
        total_return_status=raw["total_return_status"],
        total_return_basis=raw["total_return_basis"],
    )
    _validate_total_return_contract(row)
    return row


def _validate_total_return_contract(row: ForwardReturnRow) -> None:
    if row.total_return_basis != TOTAL_RETURN_BASIS:
        raise ValueError(f"invalid total return basis: {row.total_return_basis!r}")
    if row.total_return_status not in TOTAL_RETURN_STATUSES:
        raise ValueError(f"invalid total return status: {row.total_return_status!r}")
    if row.total_return_status == "resolved":
        values = (row.price_return, row.realized_dividend_sum, row.total_return)
        if (
            row.status != "resolved"
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


def _opt_float(raw: Mapping[str, str], key: str) -> float | None:
    text = raw.get(key, "")
    return float(text) if text else None


def _opt_int(raw: Mapping[str, str], key: str) -> int | None:
    text = raw.get(key, "")
    return int(text) if text else None


def _opt_bool(raw: Mapping[str, str], key: str) -> bool | None:
    text = raw.get(key, "")
    if not text:
        return None
    if text == _BOOL_TRUE:
        return True
    if text == _BOOL_FALSE:
        return False
    raise ValueError(f"invalid boolean value for {key}: {text!r}")


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
    # market_cap_oku is the liquidity snapshot rounded to whole oku, while both
    # ratios use the exact close * shares market cap. Validate the investment
    # component against the exact-value interval represented by that rounded fact.
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

    observed_positive = (
        (row.dps_yoy_latest is not None and row.dps_yoy_latest > 0)
        or row.dps_guidance_up is True
        or row.dividend_initiation is True
        or (streak is not None and streak >= 1)
    )
    all_observed_negative = (
        row.dps_yoy_latest is not None
        and row.dps_yoy_latest <= 0
        and row.dps_guidance_up is False
        and row.dividend_initiation is False
        and streak == 0
    )
    expected = True if observed_positive else False if all_observed_negative else None
    if row.shareholder_return_change is not expected:
        raise ValueError("shareholder return change is inconsistent with its components")


def _validate_margin_supply_demand(row: PanelRow) -> None:
    short_to_adv = row.margin_short_to_adv
    if short_to_adv is not None and (
        not isfinite(short_to_adv) or short_to_adv < 0 or row.margin_week_end is None
    ):
        raise ValueError("margin short to ADV requires a dated non-negative value")

    volatility = row.realized_volatility_60d
    if volatility is not None and (not isfinite(volatility) or volatility < 0):
        raise ValueError("realized volatility must be finite and non-negative")


def _validate_normalized_profit(row: PanelRow) -> None:
    for value in (row.normalized_per_3fy, row.normalized_per_5fy):
        if value is not None and (not isfinite(value) or value <= 0):
            raise ValueError("normalized PER must be finite and positive")
    if row.self_range_observed_sessions < 0:
        raise ValueError("self-range observed sessions must be non-negative")


def _population_coverage_status(value: str) -> PopulationCoverageStatus:
    if value not in _POPULATION_COVERAGE_STATUSES:
        raise ValueError(f"invalid population coverage status: {value!r}")
    return cast(PopulationCoverageStatus, value)


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
        expected_names = {field.name for field in fields(row_type)}
        # Every column the current contract reads must be present, while a column
        # the contract no longer reads is ignored. Parsing is by name, so an extra
        # column cannot shift a value into the wrong field, and the cache version
        # remains the guard against a column whose meaning changed.
        missing = sorted(expected_names.difference(reader.fieldnames or ()))
        if missing:
            raise CalibrationCacheError(
                f"calibration cache is missing {', '.join(missing)}; run calibration-build --force"
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
