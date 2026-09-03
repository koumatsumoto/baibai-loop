"""Publish calibration cohorts from partial row dictionaries.

Measurement tools read the calibration store through its public API, so their
fixtures have to be published builds rather than files placed in a directory. Each
helper fills the fields a test does not name with the row type's own defaults, so a
fixture states only what its assertion depends on.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from functools import cache
from pathlib import Path
from typing import Any, get_args, get_type_hints

from baibai_engine.screening.calibration.forward import (
    DEFAULT_FORWARD_OBSERVATION_POLICY,
    RESOLVED_STATUSES,
    ForwardObservationPolicy,
    ForwardReturnRow,
)
from baibai_engine.screening.calibration.panel import PanelDiagnostics, PanelRow
from baibai_engine.screening.calibration.store import write_forward, write_panel

_PANEL_REQUIRED: Mapping[str, Any] = {
    "sector_33": "サービス業",
    "in_population": True,
    "market_cap_oku": None,
    "avg_turnover_oku": None,
    "listing_span_days": None,
    "close": None,
    "per_forward": None,
    "per_trailing": None,
    "pbr": None,
    "ev_ebitda": None,
    "p_s": None,
    "pcfr": None,
    "ocf_yield": None,
    "fcf_yield": None,
    "net_cash_to_market_cap": None,
    "cash_to_market_cap": None,
    "investment_securities": None,
    "asset_backed_ratio": None,
    "equity_ratio": None,
    "dividend_yield": None,
    "eps_yoy": None,
    "sales_yoy": None,
    "operating_profit_yoy": None,
    "cfo_yoy": None,
    "accruals_to_assets": None,
    "net_share_change_yoy": None,
    "tradable_share_change_yoy": None,
    "ttm_quality_per_trailing": "exact",
    "ttm_quality_ocf_yield": "exact",
    "price_change_60d": None,
    "gap_from_52w_low": None,
    "price_history_coverage_750d": None,
    "smg_per_forward": None,
    "smg_per_trailing": None,
    "smg_pbr": None,
    "smg_ev_ebitda": None,
    "smg_p_s": None,
    "srp_per_forward": None,
    "srp_per_trailing": None,
    "srp_pbr": None,
    "srp_ev_ebitda": None,
    "srp_p_s": None,
    "er_annual": None,
    "er_reversion_annual": None,
    "er_carry_annual": None,
    "er_upside_capped": None,
    "reported_short_ratio": None,
    "reported_short_breadth": None,
    "reported_short_latest_disclosed_at": None,
    "margin_week_end": None,
    "margin_long_to_adv": None,
    "margin_long_share": None,
    "margin_long_delta_26w": None,
    "margin_std_long_share": None,
    "in_review_set": False,
    "valuation_approaches": "",
    "valuation_approach_ranks": "",
    "smg_market_fallback": "",
}

_DIAGNOSTICS_REQUIRED: Mapping[str, Any] = {
    "universe_size": 0,
    "population_size": 0,
    "security_analyses": 0,
    "nominated_candidates": 0,
    "bars_tickers_not_in_master": 0,
    "effective_bars_start": "2020-01-01",
    "effective_fin_start": "2020-01-01",
    "bars_window_clamped": False,
    "fin_window_clamped": False,
    "population_per_trailing_nonnull": 0,
    "population_pbr_nonnull": 0,
    "population_ocf_yield_nonnull": 0,
    "population_per_trailing_exact": 0,
}


def _coerce(value: Any, annotation: Any) -> Any:
    """Read a fixture's loose value as the field's declared type.

    Fixtures are written for legibility, so they carry ``"0.1"`` and ``500`` where the
    row declares ``float | None``. The store is typed and rejects the mismatch, so the
    conversion belongs here rather than in every fixture.
    """

    if value is None or value == "":
        return None
    members = [item for item in get_args(annotation) if item is not type(None)]
    target = members[0] if members else annotation
    if target is bool:
        return value if isinstance(value, bool) else str(value).strip().lower() in {"true", "1"}
    if target is float:
        return float(value)
    if target is int:
        return int(float(value))
    if target is str:
        return str(value)
    return value


@cache
def _hints(model: type) -> Mapping[str, Any]:
    """`get_type_hints` resolves the whole module namespace; the answer never moves."""

    return get_type_hints(model)


def panel_row(asof: str, ticker: str, **overrides: Any) -> PanelRow:
    """One panel row, with the fields a fixture never varies filled in.

    `in_review_set` is independent from any E[r] ordering: membership is the exact
    valuation-approach nomination union.
    """

    hints = _hints(PanelRow)
    payload: dict[str, Any] = {"asof": asof, "ticker": ticker, **_PANEL_REQUIRED}
    for key, value in overrides.items():
        if key in {"asof", "ticker"}:
            continue
        payload[key] = _coerce(value, hints[key])
    return PanelRow(**payload)


def panel_diagnostics(asof: str, **overrides: Any) -> PanelDiagnostics:
    """The diagnostics block a cohort is written with, minus what a test varies."""

    payload: dict[str, Any] = {
        "asof": asof,
        "rules_hash": "abc123",
        **_DIAGNOSTICS_REQUIRED,
        **overrides,
    }
    return PanelDiagnostics(**payload)


def forward_row(asof: str, ticker: str, horizon: str, **overrides: Any) -> ForwardReturnRow:
    """One forward observation whose resolved flags follow its status.

    ``resolved`` and the total-return fields are derived rather than accepted, so a
    fixture cannot describe a row the store would refuse to read back.
    """

    hints = _hints(ForwardReturnRow)
    status = str(overrides.get("status") or "resolved")
    total_status = str(overrides.get("total_return_status") or "unresolved_price_return")
    payload: dict[str, Any] = {
        "asof": asof,
        "ticker": ticker,
        "horizon": horizon,
        "target_date": overrides.get("target_date", asof),
        "resolved": status in RESOLVED_STATUSES,
        "price_return": _coerce(overrides.get("price_return"), hints["price_return"]),
        "stale_price": bool(_coerce(overrides.get("stale_price"), hints["stale_price"])),
        "entry_date": overrides.get("entry_date", asof),
        "exit_date": overrides.get("exit_date"),
        "status": status,
        "adjustment_factor_coverage": overrides.get("adjustment_factor_coverage", "unknown"),
        "total_return_status": total_status,
    }
    if total_status == "resolved":
        price = float(payload["price_return"] or 0.0)
        total = float(overrides.get("total_return") or price)
        payload["realized_dividend_sum"] = max(0.0, total - price)
        payload["realized_dividend_fy_count"] = 1
        payload["total_return"] = total
    return ForwardReturnRow(**payload)


def _panel(asof: str, row: Mapping[str, Any]) -> PanelRow:
    payload = {key: value for key, value in row.items() if key not in {"ticker", "asof"}}
    return panel_row(asof, str(row["ticker"]), **payload)


def _forward(asof: str, row: Mapping[str, Any]) -> ForwardReturnRow:
    payload = {key: value for key, value in row.items() if key not in {"ticker", "horizon", "asof"}}
    return forward_row(asof, str(row["ticker"]), str(row["horizon"]), **payload)


def calibration_root(tmp_path: Path) -> Path:
    """An empty calibration store directory under a test's tmp_path."""

    directory = tmp_path / "calibration"
    directory.mkdir()
    return directory


def liquid_panel_row(ticker: str, asof: str, **overrides: Any) -> dict[str, Any]:
    """A panel row that clears every liquidity floor a measurement applies.

    Measurement tools drop illiquid names before they group anything, so a fixture row
    that means "included" has to state all four of those fields. What a test varies is
    what it passes.
    """

    row: dict[str, Any] = {
        "asof": asof,
        "ticker": ticker,
        "market_cap_oku": 500,
        "avg_turnover_oku": 5,
        "listing_span_days": 900,
        "per_forward": 10,
        "per_trailing": 11,
        "dividend_yield": 0.03,
        "net_share_change_yoy": 0.0,
        "er_annual": 0.05,
        "er_reversion_annual": 0.02,
        "er_carry_annual": 0.03,
        "er_upside_capped": 0.2,
    }
    row.update(overrides)
    return row


def store_panel(root: Path, asof: date, *args: Any, **kwargs: Any) -> None:
    """`write_panel` with the source and cutoff every fixture cohort declares.

    A cohort that names no source is one the store refuses to read back, so the two
    arguments no test varies are filled here rather than at each call.
    """

    write_panel(
        root,
        asof,
        *args,
        **kwargs,
    )


def store_forward(root: Path, asof: date, *args: Any, **kwargs: Any) -> None:
    """`write_forward` with the source and cutoff every fixture cohort declares."""

    write_forward(
        root,
        asof,
        *args,
        **kwargs,
    )


def publish_panel(
    directory: Path,
    asof: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    rules_hash: str = "abc123",
    exclusion_counts: Mapping[str, int] | None = None,
    forward_policy: ForwardObservationPolicy = DEFAULT_FORWARD_OBSERVATION_POLICY,
) -> None:
    diagnostics = PanelDiagnostics(
        asof=asof,
        rules_hash=rules_hash,
        policy_exclusion_reason_counts=None if exclusion_counts is None else dict(exclusion_counts),
        **_DIAGNOSTICS_REQUIRED,
    )
    write_panel(
        directory,
        date.fromisoformat(asof),
        tuple(_panel(asof, row) for row in rows),
        diagnostics,
        forward_policy=forward_policy,
    )


def publish_forward(
    directory: Path,
    asof: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    forward_policy: ForwardObservationPolicy = DEFAULT_FORWARD_OBSERVATION_POLICY,
) -> None:
    write_forward(
        directory,
        date.fromisoformat(asof),
        [_forward(asof, row) for row in rows],
        forward_policy=forward_policy,
    )
