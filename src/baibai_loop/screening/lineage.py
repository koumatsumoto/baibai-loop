from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .config import JQUANTS_CLIENT_V2_METHODS, ScreeningConfig
from .filesystem import write_text_atomic
from .providers.edinet import EDINET_API_BASE

_HEX_LENGTH = 16
_RUN_ID_SUFFIX_LENGTH = 8
# 現状 J-Quants Light tier 固定。Pro 移行時はここを変更し、その変化が config_hash
# として lineage 履歴に出ることで tier 切替を traceable にする。
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


@dataclass(slots=True)
class _SqliteCoverageAccumulator:
    source: str
    windows: int = 0
    records: int = 0
    raw_records: int = 0
    normalized_records: int = 0
    skipped_records: int = 0
    rejected_records: int = 0
    excluded_records: int = 0
    statuses: set[str] = field(default_factory=set)
    non_ok_windows: int = 0
    errors: set[str] = field(default_factory=set)

    def as_payload(self) -> dict[str, object]:
        return {
            "source": self.source,
            "windows": self.windows,
            "records": self.records,
            "raw_records": self.raw_records,
            "normalized_records": self.normalized_records,
            "skipped_records": self.skipped_records,
            "rejected_records": self.rejected_records,
            "excluded_records": self.excluded_records,
            "statuses": sorted(self.statuses),
            "non_ok_windows": self.non_ok_windows,
            "errors": sorted(self.errors),
        }


def build_provider_settings(config: ScreeningConfig) -> dict[str, object]:
    settings: dict[str, object] = {
        "jquants": {
            "tier": _PROVIDER_TIER,
            "client": "ClientV2",
            "methods": list(JQUANTS_CLIENT_V2_METHODS),
        },
        "jpx": {
            "regulation_urls": dict(sorted(config.jpx_regulation_urls.items())),
            "special_caution_index_url": config.jpx_special_caution_index_url,
        },
    }
    if config.edinet_api_key:
        settings["edinet"] = {
            # api_version は EDINET_API_BASE に既に含まれるため別 key としては持たない。
            # API バージョンを上げる際は URL 側を変更すれば config_hash に出る。
            "api_base": EDINET_API_BASE,
        }
    return settings


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

    for path in root.rglob("*"):
        if not path.is_file():
            continue
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
    # POSIX 文字列順で安定 sort する。CacheManifest は「files が文字列順 sorted」を
    # 契約として保つため、後段の hash / payload は再 sort 不要。
    records.sort(key=lambda record: record.path)
    return CacheManifest(cache_root=root, files=tuple(records))


def compute_cache_manifest_hash(manifest: CacheManifest) -> str:
    return _short_sha256(_manifest_files_payload(manifest))


def compute_sqlite_fingerprint(sqlite_path: Path) -> str:
    """Return a lightweight deterministic fingerprint for the SQLite store."""
    summary = compute_sqlite_summary(sqlite_path)
    if summary is None:
        return _short_sha256({"sqlite": "missing", "path": sqlite_path.as_posix()})
    return _short_sha256(summary)


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
    sqlite_path: Path | None = None,
) -> None:
    payload: dict[str, object] = {
        "run_id": run_id,
        "asof_date": asof_date.isoformat(),
        "cache_root": manifest.cache_root.as_posix(),
        "config_hash": config_hash,
        "cache_manifest_hash": manifest_hash,
        "generated_at": generated_at.isoformat(),
        "files": _manifest_files_payload(manifest),
    }
    sqlite_summary = compute_sqlite_summary(sqlite_path) if sqlite_path else None
    if sqlite_summary is not None:
        payload["sqlite_cache"] = sqlite_summary
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def compute_sqlite_summary(sqlite_path: Path | None) -> dict[str, object] | None:
    """Snapshot the canonical SQLite state at run time."""
    if sqlite_path is None or not sqlite_path.exists():
        return None
    conn = sqlite3.connect(sqlite_path)
    try:
        try:
            schema_version_row = conn.execute(
                "SELECT value FROM cache_metadata WHERE key = 'schema_version'"
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        try:
            coverage_rows = conn.execute(
                "SELECT source, record_count, raw_record_count, normalized_record_count, "
                "skipped_record_count, rejected_record_count, excluded_record_count, "
                "status, error FROM source_coverage ORDER BY source"
            ).fetchall()
        except sqlite3.OperationalError:
            coverage_rows = []
    finally:
        conn.close()

    coverage_by_source: dict[str, _SqliteCoverageAccumulator] = {}
    for (
        source,
        record_count,
        raw_record_count,
        normalized_record_count,
        skipped_record_count,
        rejected_record_count,
        excluded_record_count,
        status,
        error,
    ) in coverage_rows:
        source_key = str(source)
        entry = coverage_by_source.setdefault(
            source_key, _SqliteCoverageAccumulator(source=source_key)
        )
        records = int(record_count or 0)
        entry.windows += 1
        entry.records += records
        entry.raw_records += records if raw_record_count is None else int(raw_record_count)
        entry.normalized_records += (
            records if normalized_record_count is None else int(normalized_record_count)
        )
        entry.skipped_records += int(skipped_record_count or 0)
        entry.rejected_records += int(rejected_record_count or 0)
        entry.excluded_records += int(excluded_record_count or 0)
        entry.statuses.add(str(status or "ok"))
        if status != "ok":
            entry.non_ok_windows += 1
        if error:
            entry.errors.add(str(error))

    return {
        "path": sqlite_path.as_posix(),
        "schema_version": schema_version_row[0] if schema_version_row else None,
        "coverage": [entry.as_payload() for entry in coverage_by_source.values()],
    }


def _traceable_config_payload(config: ScreeningConfig) -> dict[str, object]:
    rules_hash = None
    if config.rules_path.exists():
        rules_hash = hashlib.sha256(config.rules_path.read_bytes()).hexdigest()
    return {
        "cache_dir": config.cache_dir.as_posix(),
        "rules_path": config.rules_path.as_posix(),
        "rules_sha256": rules_hash,
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
    # CacheManifest.files は compute_cache_manifest が POSIX 文字列順で sorted 済み。
    return [
        {"path": record.path, "sha256": record.sha256, "size": record.size}
        for record in manifest.files
    ]


def _short_sha256(payload: object) -> str:
    normalized = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:_HEX_LENGTH]
