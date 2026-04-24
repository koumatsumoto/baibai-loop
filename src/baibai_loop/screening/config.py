from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

DEFAULT_CACHE_DIR = Path(".cache/screening")
JQUANTS_CLIENT_V2_METHODS = (
    "get_eq_master",
    "get_eq_bars_daily_range",
    "get_fin_summary_range",
    "get_eq_earnings_cal",
    "get_mkt_calendar",
)
YOY_DETERIORATION_THRESHOLD = -0.30
PARTIAL_WARNING_TTM_RATIO = 0.05
PARTIAL_WARNING_TTM_COUNT = 20
PARTIAL_WARNING_YOY_MISSING_RATIO = 0.10


class ConfigError(ValueError):
    """Raised when required screening configuration is missing."""


@dataclass(frozen=True)
class ScreeningConfig:
    jquants_refresh_token: str
    edinet_api_key: str
    cache_dir: Path = DEFAULT_CACHE_DIR

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ScreeningConfig":
        source = env if env is not None else os.environ
        missing = [
            name
            for name in ("JQUANTS_REFRESH_TOKEN", "EDINET_API_KEY")
            if not source.get(name)
        ]
        if missing:
            missing_names = ", ".join(missing)
            raise ConfigError(f"missing required env vars: {missing_names}")

        cache_dir_value = source.get("SCREENING_CACHE_DIR", str(DEFAULT_CACHE_DIR))
        return cls(
            jquants_refresh_token=source["JQUANTS_REFRESH_TOKEN"],
            edinet_api_key=source["EDINET_API_KEY"],
            cache_dir=Path(cache_dir_value),
        )
