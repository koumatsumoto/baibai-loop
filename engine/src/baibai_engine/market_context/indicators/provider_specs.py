"""Provider capability contracts that are safe for read-only consumers to import."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderSpec:
    """A provider's fetch, store, and point-in-time read capabilities."""

    name: str
    # ``http`` acquires source observations; ``local`` describes a virtual L2
    # series computed from stored inputs, not another acquisition or stored history.
    kind: Literal["http", "local"] = "http"
    # All-history refresh floor. ``all_history_start`` is the reproducible fixed
    # start a bulk source exposes; ``all_history_rolling_years`` derives the floor
    # from today instead (a licensed rolling window such as J-Quants' 10 years).
    all_history_start: date | None = None
    all_history_rolling_years: int | None = None
    # Reads clamp the provider's publication-date vintage to the read cutoff.
    # This is not proof of exact historical API availability: J-Quants flows
    # can serve corrections later, and older periods may retain only the latest revision.
    point_in_time_vintage: bool = False
    # Credential env var names required to fetch (diagnostics only; values are
    # read from the environment by the provider, never stored in the registry).
    required_env: tuple[str, ...] = ()


__all__ = ["ProviderSpec"]
