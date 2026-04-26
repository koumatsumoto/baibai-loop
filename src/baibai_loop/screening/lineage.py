from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .config import JQUANTS_CLIENT_V2_METHODS, ScreeningConfig
from .filesystem import write_text_atomic
from .providers.edinet import EDINET_API_BASE

_HEX_LENGTH = 16
_RUN_ID_SUFFIX_LENGTH = 8
_PROVIDER_TIER = "j-quants-light"


@dataclass(frozen=True, slots=True)
class CacheManifestRecord:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class CacheManifest:
    cache_root: Path
    files: tuple[CacheManifestRecord, ...]


def build_provider_settings(config: ScreeningConfig) -> dict[str, object]:
    return {
        "jquants": {
            "tier": _PROVIDER_TIER,
            "client": "ClientV2",
            "methods": list(JQUANTS_CLIENT_V2_METHODS),
        },
        "edinet": {
            "api_base": EDINET_API_BASE,
            "api_version": "v2",
        },
        "jpx": {
            "regulation_urls": dict(sorted(config.jpx_regulation_urls.items())),
            "special_caution_index_url": config.jpx_special_caution_index_url,
        },
    }


def compute_config_hash(
    config: ScreeningConfig,
    provider_settings: Mapping[str, object],
) -> str:
    payload = {
        "config": _traceable_config_payload(config),
        "provider_settings": _normalize_mapping(provider_settings),
    }
    return _short_sha256(payload)


def compute_cache_manifest(cache_root: Path) -> CacheManifest:
    root = Path(cache_root)
    records: list[CacheManifestRecord] = []
    if not root.exists():
        return CacheManifest(cache_root=root, files=())

    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative_path = path.relative_to(root)
        if relative_path.parts and relative_path.parts[0] == "manifests":
            continue
        content = path.read_bytes()
        records.append(
            CacheManifestRecord(
                path=relative_path.as_posix(),
                sha256=hashlib.sha256(content).hexdigest(),
                size=len(content),
            )
        )
    return CacheManifest(cache_root=root, files=tuple(records))


def compute_cache_manifest_hash(manifest: CacheManifest) -> str:
    return _short_sha256(_manifest_files_payload(manifest))


def build_run_id(asof_date: date, config_hash: str) -> str:
    suffix = config_hash[:_RUN_ID_SUFFIX_LENGTH]
    return f"screening-{asof_date:%Y%m%d}-{suffix}"


def write_manifest(
    path: Path,
    manifest: CacheManifest,
    *,
    run_id: str,
    asof_date: date,
    config_hash: str,
    manifest_hash: str,
    generated_at: datetime,
) -> None:
    payload = {
        "run_id": run_id,
        "asof_date": asof_date.isoformat(),
        "config_hash": config_hash,
        "cache_manifest_hash": manifest_hash,
        "generated_at": generated_at.isoformat(),
        "files": _manifest_files_payload(manifest),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _traceable_config_payload(config: ScreeningConfig) -> dict[str, object]:
    return {
        "cache_dir": config.cache_dir.as_posix(),
        "jpx_regulation_urls": dict(sorted(config.jpx_regulation_urls.items())),
        "jpx_special_caution_index_url": config.jpx_special_caution_index_url,
    }


def _normalize_mapping(value: Mapping[str, object]) -> dict[str, object]:
    return {key: _normalize_value(value[key]) for key in sorted(value)}


def _normalize_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _normalize_mapping(value)
    if isinstance(value, tuple | list):
        return [_normalize_value(item) for item in value]
    if isinstance(value, Path):
        return value.as_posix()
    return value


def _manifest_files_payload(manifest: CacheManifest) -> list[dict[str, object]]:
    return [
        {"path": record.path, "sha256": record.sha256, "size": record.size}
        for record in sorted(manifest.files, key=lambda item: item.path)
    ]


def _short_sha256(payload: object) -> str:
    normalized = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:_HEX_LENGTH]
