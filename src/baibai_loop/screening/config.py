from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .jpx_sources import JPX_SPECIAL_CAUTION_SOURCE_NAME

# Git-tracked raw JSON cache root. Each per-fetch JSON file is stored verbatim
# under this path so that another machine can reconstruct the screening input
# from a fresh `git clone` without re-hitting J-Quants / EDINET / JPX (issue
# #45). Each file is expected to stay below 50MB so it fits standard Git.
DEFAULT_CACHE_DIR = Path("records/_data/raw/screening")
# Gitignored derived caches (SQLite, rebuild temp). Built from the raw JSON
# under DEFAULT_CACHE_DIR; safe to delete and rebuild on any machine.
DEFAULT_SQLITE_CACHE_DIR = Path("records/_data/cache/screening")
LEGACY_CACHE_DIR = Path(".cache/screening")
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
JPX_REGULATION_ENV_MAP = {
    "特別注意銘柄": "JPX_SPECIAL_CAUTION_URL",
    "整理銘柄": "JPX_REORGANIZATION_URL",
    "取引停止": "JPX_TRADING_HALT_URL",
    "上場廃止警告": "JPX_DELISTING_WARNING_URL",
}
JPX_SPECIAL_CAUTION_INDEX_ENV = "JPX_SPECIAL_CAUTION_INDEX_URL"


class ConfigError(ValueError):
    """Raised when required screening configuration is missing."""


class ScreeningConfig(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    jquants_refresh_token: str = Field(min_length=1)
    edinet_api_key: str = Field(min_length=1)
    cache_dir: Path = DEFAULT_CACHE_DIR
    sqlite_cache_dir: Path = DEFAULT_SQLITE_CACHE_DIR
    jpx_regulation_urls: Mapping[str, str] = Field(default_factory=dict)
    jpx_special_caution_index_url: str | None = None

    def __init__(
        self,
        jquants_refresh_token: str | None = None,
        edinet_api_key: str | None = None,
        /,
        **data: Any,
    ) -> None:
        if jquants_refresh_token is not None:
            data["jquants_refresh_token"] = jquants_refresh_token
        if edinet_api_key is not None:
            data["edinet_api_key"] = edinet_api_key
        super().__init__(**data)

    @field_validator("cache_dir", "sqlite_cache_dir", mode="before")
    @classmethod
    def _coerce_dir(cls, value: object) -> Path:
        if isinstance(value, Path):
            return value
        if isinstance(value, str) and value:
            return Path(value)
        raise ValueError("path must be a non-empty string or Path")

    @field_validator("jpx_regulation_urls")
    @classmethod
    def _validate_jpx_urls(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        for source_name, url in value.items():
            if not source_name:
                raise ValueError("JPX source name must not be empty")
            if not url.startswith("https://"):
                raise ValueError(f"JPX URL must use https: {source_name}")
        return value

    @field_validator("jpx_special_caution_index_url")
    @classmethod
    def _validate_special_caution_index_url(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("https://"):
            raise ValueError("JPX special caution index URL must use https")
        return value

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ScreeningConfig:
        source = env if env is not None else os.environ
        missing = [
            name for name in ("JQUANTS_REFRESH_TOKEN", "EDINET_API_KEY") if not source.get(name)
        ]
        if missing:
            missing_names = ", ".join(missing)
            raise ConfigError(f"missing required env vars: {missing_names}")

        # cache_dir は records/_data/raw/screening 固定 (git 追跡対象)。
        # 過去 SCREENING_CACHE_DIR で override 可能だったが、.env 値が
        # 古い `.cache/screening` を指したまま残ると新しい canonical
        # ツリーが無視され、chunk 整合のとれない split cache を抱える
        # regression を起こすので env override を廃止する。
        cache_dir_value = str(DEFAULT_CACHE_DIR)
        sqlite_cache_dir_value = str(DEFAULT_SQLITE_CACHE_DIR)
        jpx_regulation_urls = {
            source_name: source[env_name]
            for source_name, env_name in JPX_REGULATION_ENV_MAP.items()
            if source.get(env_name)
        }
        special_caution_index_url = source.get(JPX_SPECIAL_CAUTION_INDEX_ENV) or None
        if special_caution_index_url and JPX_SPECIAL_CAUTION_SOURCE_NAME not in jpx_regulation_urls:
            jpx_regulation_urls[JPX_SPECIAL_CAUTION_SOURCE_NAME] = special_caution_index_url

        try:
            return cls(
                jquants_refresh_token=source["JQUANTS_REFRESH_TOKEN"],
                edinet_api_key=source["EDINET_API_KEY"],
                cache_dir=cache_dir_value,
                sqlite_cache_dir=sqlite_cache_dir_value,
                jpx_regulation_urls=jpx_regulation_urls,
                jpx_special_caution_index_url=special_caution_index_url,
            )
        except ValidationError as exc:
            raise ConfigError(str(exc)) from exc
