"""Macro analysis records: dated, sourced research outputs that name trade levers.

A macro analysis answers a macro/market question, grounded in stats series, and
states the trade-relevant levers it drives (sector tilt, risk posture, theme,
timing). It is the bridge from the macro capability to the operating loop's
judgment layers: `select` reads sector tilts to deprioritise candidates in
tilted-away sectors (soft, never a hard gate), and the portfolio risk posture
informs sizing. The mechanical screen `run` stays macro-blind by construction.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from baibai_loop.yaml_io import safe_load

MACRO_ANALYSIS_ROOT = Path("records/02-macro-analysis")


@dataclass(frozen=True, slots=True)
class TradeLever:
    lever: str
    target: str
    direction: str
    rationale: str


@dataclass(frozen=True, slots=True)
class RiskPosture:
    stance: str
    rationale: str
    gross_guidance: str | None = None
    cash_floor_pct: float | None = None


@dataclass(frozen=True, slots=True)
class MacroAnalysis:
    analysis_id: str
    path: Path
    as_of: date
    question: str
    summary: str
    horizon: str
    forward_view: str
    confidence: str
    risk_posture: RiskPosture
    trade_levers: tuple[TradeLever, ...]
    payload: Mapping[str, Any]


def discover_macro_analysis_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.glob("*/*/macro-analysis-*.yaml") if path.is_file())


def find_latest_macro_analysis(root: Path, asof_date: date) -> Path | None:
    eligible = [
        path
        for path in discover_macro_analysis_files(root)
        if (parsed := _date_from_name(path.name)) is not None and parsed <= asof_date
    ]
    return eligible[-1] if eligible else None


def load_macro_analysis(path: Path) -> MacroAnalysis:
    payload = safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"macro analysis YAML root must be a mapping: {path}")
    if payload.get("kind") != "macro-analysis":
        raise ValueError(f"macro analysis kind must be macro-analysis: {path}")
    return MacroAnalysis(
        analysis_id=_required_str(payload, "analysis_id", path),
        path=path,
        as_of=_required_date(payload, "as_of", path),
        question=_required_str(payload, "question", path),
        summary=_required_str(payload, "summary", path),
        horizon=_required_str(payload, "horizon", path),
        forward_view=_required_str(payload, "forward_view", path),
        confidence=_required_str(payload, "confidence", path),
        risk_posture=_parse_risk_posture(payload.get("risk_posture"), path),
        trade_levers=tuple(_parse_levers(payload.get("trade_levers"))),
        payload=payload,
    )


def sector_lever(analysis: MacroAnalysis, sector: str) -> TradeLever | None:
    """Return the sector_tilt lever whose target matches the candidate sector.

    `select` uses this to surface (and soft-deprioritise) candidates whose sector
    the macro read tilts away from; it is never a hard gate.
    """
    for lever in analysis.trade_levers:
        if lever.lever == "sector_tilt" and lever.target == sector:
            return lever
    return None


def risk_posture_sizing_factor(posture: RiskPosture) -> float:
    """Multiplier the portfolio policy applies to starter size for the macro posture.

    risk_off shrinks new starters, neutral trims them, risk_on allows full
    starter size. This is the policy-side connection: the macro read scales how
    much risk the operating loop takes, without ever touching the mechanical
    screen.
    """
    match posture.stance:
        case "risk_on":
            return 1.0
        case "neutral":
            return 0.75
        case "risk_off":
            return 0.5
        case _:
            return 0.5


def _parse_levers(raw: object) -> Sequence[TradeLever]:
    if not isinstance(raw, list):
        return ()
    levers: list[TradeLever] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        levers.append(
            TradeLever(
                lever=str(item.get("lever") or ""),
                target=str(item.get("target") or ""),
                direction=str(item.get("direction") or ""),
                rationale=str(item.get("rationale") or ""),
            )
        )
    return levers


def _parse_risk_posture(raw: object, path: Path) -> RiskPosture:
    if not isinstance(raw, Mapping):
        raise ValueError(f"macro analysis risk_posture must be a mapping: {path}")
    cash_floor = raw.get("cash_floor_pct")
    gross = raw.get("gross_guidance")
    return RiskPosture(
        stance=str(raw.get("stance") or ""),
        rationale=str(raw.get("rationale") or ""),
        gross_guidance=str(gross) if isinstance(gross, str) and gross else None,
        cash_floor_pct=float(cash_floor) if isinstance(cash_floor, (int, float)) else None,
    )


def _required_str(payload: Mapping[str, Any], key: str, path: Path) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"macro analysis {key} must be a non-empty string: {path}")
    return value


def _required_date(payload: Mapping[str, Any], key: str, path: Path) -> date:
    raw = _required_str(payload, key, path)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"macro analysis {key} must be YYYY-MM-DD: {path}") from exc


def _date_from_name(name: str) -> date | None:
    marker = "macro-analysis-"
    if marker not in name:
        return None
    raw = name.split(marker, 1)[1][:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None
