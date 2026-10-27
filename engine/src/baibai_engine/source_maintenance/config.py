from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from baibai_engine.market.config import DEFAULT_SQLITE_CACHE_DIR as DEFAULT_SQLITE_CACHE_DIR


class ConfigError(ValueError):
    """Raised when source maintenance configuration is missing."""


class SourceMaintenanceConfig(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    jquants_api_key: str | None = Field(default=None, min_length=1)
    edinet_api_key: str | None = None
    sqlite_cache_dir: Path = DEFAULT_SQLITE_CACHE_DIR

    @field_validator("sqlite_cache_dir", mode="before")
    @classmethod
    def _coerce_dir(cls, value: object) -> Path:
        if isinstance(value, Path):
            return value
        if isinstance(value, str) and value:
            return Path(value)
        raise ValueError("path must be a non-empty string or Path")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SourceMaintenanceConfig:
        source = env if env is not None else os.environ
        # sqlite_cache_dir は repo 内の運用規約に固定する。
        # env override は作業者ごとの local store 分岐を生むため受け付けない。
        try:
            return cls(
                jquants_api_key=source.get("JQUANTS_API_KEY") or None,
                edinet_api_key=source.get("EDINET_API_KEY") or None,
                sqlite_cache_dir=DEFAULT_SQLITE_CACHE_DIR,
            )
        except ValidationError as exc:
            raise ConfigError(str(exc)) from exc
