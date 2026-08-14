"""Publish calibration cohorts from partial row dictionaries.

Measurement tools read the calibration store through its public API, so their
fixtures have to be published builds rather than files placed in a directory. Each
helper fills the fields a test does not name with the row type's own defaults, so a
fixture states only what its assertion depends on.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any, get_args, get_type_hints

from baibai_engine.foundation.filesystem import write_bytes_atomic
from baibai_engine.market.lake.models import (
    CalibrationInputFile,
    CalibrationInputManifest,
    CalibrationInputSourceRef,
)
from baibai_engine.market.lake.objects import sha256_bytes
from baibai_engine.screening.calibration.forward import (
    DEFAULT_FORWARD_OBSERVATION_POLICY,
    RESOLVED_STATUSES,
    ForwardObservationPolicy,
    ForwardReturnRow,
)
from baibai_engine.screening.calibration.lake import canonical_manifest_bytes
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
    "pass_screen": False,
    "evidence_playbooks": "",
    "selection_rank": None,
    "recommended_rank": None,
}

_DIAGNOSTICS_REQUIRED: Mapping[str, Any] = {
    "universe_size": 0,
    "population_size": 0,
    "candidates": 0,
    "evidence_candidates": 0,
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

_TEST_PRODUCER_COMMIT = "a" * 40


def synthetic_calibration_source(
    directory: Path, *, label: str = "default"
) -> CalibrationInputSourceRef:
    """Create an explicit, closed input generation for store-level tests."""

    payload = f"synthetic calibration test input: {label}\n".encode()
    digest = sha256_bytes(payload)
    input_id = f"synthetic-test-{digest[:24]}"
    file_key = f"lake/l2/calibration-legacy/{input_id}/input.txt"
    write_bytes_atomic(directory / file_key, payload)
    manifest = CalibrationInputManifest(
        manifest_version=1,
        input_id=input_id,
        input_type="local_operation",
        files={
            "input.txt": CalibrationInputFile(
                key=file_key,
                sha256=digest,
                bytes=len(payload),
            )
        },
    )
    manifest_key = f"lake/manifests/calibration-inputs/{input_id}.json"
    manifest_payload = canonical_manifest_bytes(manifest)
    write_bytes_atomic(directory / manifest_key, manifest_payload)
    return CalibrationInputSourceRef(
        kind="calibration_input",
        source_id=input_id,
        key=manifest_key,
        sha256=sha256_bytes(manifest_payload),
        input_type="local_operation",
        manifest_version=1,
    )


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


def panel_row(asof: str, ticker: str, **overrides: Any) -> PanelRow:
    hints = get_type_hints(PanelRow)
    payload: dict[str, Any] = {"asof": asof, "ticker": ticker, **_PANEL_REQUIRED}
    for key, value in overrides.items():
        if key in {"asof", "ticker"}:
            continue
        payload[key] = _coerce(value, hints[key])
    return PanelRow(**payload)


def forward_row(asof: str, ticker: str, horizon: str, **overrides: Any) -> ForwardReturnRow:
    """One forward observation whose resolved flags follow its status.

    ``resolved`` and the total-return fields are derived rather than accepted, so a
    fixture cannot describe a row the store would refuse to read back.
    """

    hints = get_type_hints(ForwardReturnRow)
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


def publish_panel(
    directory: Path,
    asof: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    rules_hash: str = "abc123",
    exclusion_counts: Mapping[str, int] | None = None,
    source_label: str = "default",
    producer_commit: str = _TEST_PRODUCER_COMMIT,
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
        source=synthetic_calibration_source(directory, label=source_label),
        input_cutoff=date.fromisoformat(asof),
        producer_commit=producer_commit,
        test_only=True,
        forward_policy=forward_policy,
    )


def publish_forward(
    directory: Path,
    asof: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    source_label: str = "default",
    input_cutoff: date | None = None,
    producer_commit: str = _TEST_PRODUCER_COMMIT,
    forward_policy: ForwardObservationPolicy = DEFAULT_FORWARD_OBSERVATION_POLICY,
) -> None:
    write_forward(
        directory,
        date.fromisoformat(asof),
        [_forward(asof, row) for row in rows],
        source=synthetic_calibration_source(directory, label=source_label),
        input_cutoff=input_cutoff or date.fromisoformat(asof),
        producer_commit=producer_commit,
        test_only=True,
        forward_policy=forward_policy,
    )
