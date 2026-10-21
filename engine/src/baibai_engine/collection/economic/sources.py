"""External economic source requests, independent of their L2 series uses."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from baibai_engine.foundation.yaml_io import strict_safe_load

DOCUMENT_PROVIDERS = frozenset({"spglobal_pmi", "tsr_bankruptcies"})

SOURCE_CATALOG = Path(__file__).with_suffix(".yaml")


@dataclass(frozen=True, slots=True)
class SourceDefinition:
    source_id: str
    provider: str
    source_url: str
    frequency: str
    provider_series_id: str = ""


def load_sources(path: Path = SOURCE_CATALOG) -> dict[str, SourceDefinition]:
    raw = strict_safe_load(path.read_text(encoding="utf-8"))
    entries = raw.get("sources") if isinstance(raw, dict) else None
    if not isinstance(entries, dict) or not entries:
        raise ValueError("economic source catalog must contain sources")
    result = {}
    for source_id, entry in entries.items():
        if not isinstance(source_id, str) or not source_id.strip() or not isinstance(entry, dict):
            raise ValueError("economic source identity and definition are required")
        fields = {}
        for name in ("provider", "source_url", "frequency"):
            value = entry.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"economic source {source_id} requires {name}")
            fields[name] = value
        code = entry.get("provider_series_id", "")
        if not isinstance(code, str):
            raise ValueError(f"economic source {source_id} has an invalid provider series code")
        if fields["provider"] == "derived":
            raise ValueError("a derived calculation is not an external collection source")
        result[source_id] = SourceDefinition(
            source_id=source_id,
            provider=fields["provider"],
            source_url=fields["source_url"],
            frequency=fields["frequency"],
            provider_series_id=code,
        )
    return result


def require_source(source_id: str) -> SourceDefinition:
    try:
        return load_sources()[source_id]
    except KeyError as error:
        raise ValueError(f"unknown economic collection source: {source_id}") from error
