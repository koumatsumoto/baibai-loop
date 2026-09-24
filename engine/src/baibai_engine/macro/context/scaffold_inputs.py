"""Scaffold the ``inputs.indicator_series`` block of a macro context draft.

A strategy-grade report cites on the order of a hundred indicator series, and every
citation carries provenance the store already knows: the provider, the latest
observation at or before the as-of, its vintage, and the effective percentile window.
Hand-copying those fields is where citation trails go wrong, so this tool derives the
whole block from the L1 store and the reading computation — the same sources the
published numbers come from.

The input is a spec mapping section names to the series each section examines:

    asof: 2026-07-27
    sections:
      金利・金融政策:
        - us.10y
        - jp.10y
      為替:
        - usd_jpy

The output is a YAML list of ``IndicatorSeriesInput`` entries (one per distinct
series, ``used_for`` aggregated across the naming sections), ready to be placed under
``inputs.indicator_series`` in the draft. The tool reads the store read-only and never
writes anywhere but the requested output.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import yaml

from baibai_engine.foundation.time import JST
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.macro.indicators.db import (
    DEFAULT_DB_PATH,
    IndicatorsSchemaError,
    open_read_only_connection,
)
from baibai_engine.macro.indicators.definitions import SeriesDefinition, load_definitions
from baibai_engine.macro.reading.compute import compute_reading
from baibai_engine.macro.reading.reader import build_store_observation_reader
from baibai_engine.macro.reading.rules import (
    DEFAULT_RULES_PATH,
    ReadingRulesError,
    load_reading_rules,
    rules_revision,
)


@dataclass(frozen=True, slots=True)
class _Spec:
    asof: date
    accessed_at: datetime
    sections: dict[str, tuple[str, ...]]


def _load_spec(path: Path) -> _Spec:
    raw = safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"spec must be a mapping: {path}")
    asof = raw.get("asof")
    if not isinstance(asof, date):
        raise ValueError("spec.asof must be an ISO date")
    accessed_raw = raw.get("accessed_at")
    if accessed_raw is None:
        accessed_at = datetime.now(JST).replace(microsecond=0)
    elif isinstance(accessed_raw, datetime) and accessed_raw.tzinfo is not None:
        accessed_at = accessed_raw
    else:
        raise ValueError("spec.accessed_at must be a timezone-aware datetime when given")
    sections_raw = raw.get("sections")
    if not isinstance(sections_raw, dict) or not sections_raw:
        raise ValueError("spec.sections must be a non-empty mapping of section name to series")
    sections: dict[str, tuple[str, ...]] = {}
    for name, series_list in sections_raw.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("spec.sections keys must be non-blank section names")
        if (
            not isinstance(series_list, list)
            or not series_list
            or not all(isinstance(item, str) for item in series_list)
        ):
            raise ValueError(f"spec.sections[{name!r}] must be a non-empty list of series IDs")
        sections[name] = tuple(series_list)
    return _Spec(asof=asof, accessed_at=accessed_at, sections=sections)


def _input_id(series_id: str) -> str:
    return "series-" + series_id.replace(".", "-").replace("_", "-")


def build_indicator_inputs(
    spec: _Spec,
    *,
    db_path: Path,
    rules_path: Path,
) -> list[dict[str, object]]:
    definitions = load_definitions()
    by_id: dict[str, SeriesDefinition] = {
        definition.series_id: definition for definition in definitions.series
    }
    used_for: dict[str, list[str]] = {}
    for section, series_list in spec.sections.items():
        for series_id in series_list:
            names = used_for.setdefault(series_id, [])
            if section not in names:
                names.append(section)
    unknown = sorted(series_id for series_id in used_for if series_id not in by_id)
    if unknown:
        raise ValueError("spec names unregistered series: " + ", ".join(unknown))

    rules = load_reading_rules(rules_path)
    connection = open_read_only_connection(db_path)
    try:
        connection.execute("BEGIN")
        requested = tuple(by_id[series_id] for series_id in sorted(used_for))
        reader = build_store_observation_reader(
            connection,
            series=requested,
            vintage_cutoff=spec.asof,
        )
        snapshot = compute_reading(
            series=requested,
            reader=reader,
            rules=rules,
            rules_revision=rules_revision(rules_path),
            asof=spec.asof,
        )
        entries: list[dict[str, object]] = []
        for reading in snapshot.series:
            if reading.observed_at is None:
                raise ValueError(
                    f"no observation at or before asof: {reading.series_id} @ {spec.asof}"
                )
            selected = reader(reading.series_id, reading.observed_at, reading.observed_at)
            vintage = selected[0].vintage_at if selected else None
            if vintage is None:
                raise ValueError(
                    f"no eligible vintage in the store: {reading.series_id} @ {reading.observed_at}"
                )
            entries.append(
                {
                    "input_id": _input_id(reading.series_id),
                    "provider": by_id[reading.series_id].provider,
                    "series_id": reading.series_id,
                    "window": (
                        f"percentile 実効窓 {reading.window_years}y"
                        f"（観測 {reading.window_observations} 件）/ asof {spec.asof.isoformat()}"
                    ),
                    "observation_as_of": reading.observed_at.isoformat(),
                    "published_at": vintage.isoformat(),
                    "accessed_at": spec.accessed_at.isoformat(),
                    "status": "ok",
                    "used_for": "・".join(used_for[reading.series_id]) + " の fact 根拠",
                }
            )
        return entries
    finally:
        connection.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "derive inputs.indicator_series for a macro context draft from the L1 "
            "store and the reading computation"
        )
    )
    parser.add_argument("spec", type=Path, help="YAML spec: asof + sections -> series IDs")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES_PATH)
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="write the YAML list here instead of stdout",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        spec = _load_spec(args.spec)
        entries = build_indicator_inputs(spec, db_path=args.db, rules_path=args.rules)
    except (OSError, ValueError, IndicatorsSchemaError, ReadingRulesError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    rendered = yaml.safe_dump(entries, sort_keys=False, allow_unicode=True)
    if args.output is None:
        sys.stdout.write(rendered)
    else:
        args.output.write_text(rendered, encoding="utf-8")
        print(f"wrote {len(entries)} indicator inputs to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
