"""Structural outlook annotation: AI tailwind / neutral / structural decline.

A config-driven selection-layer annotation. It marks whether a candidate's
business sits in a sector with a long-run AI structural tailwind, is
structurally neutral, or is a secularly declining business, so the research
layer can tilt toward durable long-hold quality and away from shrinking demand.

It is deliberately **not** a screen gate, **not** a ranking sort-key component,
and **not** a validator rule. The portfolio policy keeps AI a strategic lens for
long-hold quality (never a sole adoption / sizing / ranking / validator rule),
and production ranking changes require forward measurement; an annotation
respects both. The taxonomy lives in a curated config file so it can be edited
without code changes, like the screening thresholds and the macro sector tilts.

Classification precedence (first match wins):

1. ``ticker_overrides`` — an explicit per-ticker judgment (highest precedence)
2. ``sector_defaults`` — the East-exchange 33-sector default tilt
3. otherwise ``neutral``
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_STRUCTURAL_OUTLOOK_PATH = Path(
    "records/_config/structural-outlook/2026-06-15T000000+0900.yaml"
)


class StructuralOutlook(StrEnum):
    AI_TAILWIND = "ai_tailwind"
    NEUTRAL = "neutral"
    STRUCTURAL_DECLINE = "structural_decline"


class TickerStructuralOverride(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outlook: StructuralOutlook
    note: str = Field(min_length=1)


class StructuralOutlookConfig(BaseModel):
    """Curated AI / structural-decline taxonomy by sector and by ticker."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    notes: str | None = None
    sector_defaults: Mapping[str, StructuralOutlook] = Field(default_factory=dict)
    ticker_overrides: Mapping[str, TickerStructuralOverride] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StructuralOutlookResult:
    outlook: StructuralOutlook
    basis: str
    note: str | None

    def to_dict(self) -> dict[str, object]:
        return {"outlook": self.outlook.value, "basis": self.basis, "note": self.note}


def classify_structural_outlook(
    *,
    ticker: str,
    sector_33: str,
    config: StructuralOutlookConfig,
) -> StructuralOutlookResult:
    """Classify one candidate's structural outlook from the curated config."""
    override = config.ticker_overrides.get(ticker)
    if override is not None:
        return StructuralOutlookResult(override.outlook, "ticker_override", override.note)
    sector_default = config.sector_defaults.get(sector_33)
    if sector_default is not None:
        return StructuralOutlookResult(sector_default, "sector_default", None)
    return StructuralOutlookResult(StructuralOutlook.NEUTRAL, "default", None)


def load_structural_outlook_config(
    path: Path = DEFAULT_STRUCTURAL_OUTLOOK_PATH,
) -> StructuralOutlookConfig:
    if not path.exists() and not path.is_absolute():
        path = Path(__file__).resolve().parents[4] / path
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"structural outlook config root must be a mapping: {path}")
    return StructuralOutlookConfig.model_validate(payload)
