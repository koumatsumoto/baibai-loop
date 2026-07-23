from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from functools import cache
from itertools import pairwise
from pathlib import Path
from typing import cast

from baibai_engine.foundation.yaml_io import strict_safe_load

from ..db import ObservationRecord
from ..definitions import IndicatorDefinitions, SeriesDefinition
from .base import FetchContext, HttpSession, IndicatorsProviderError

MANUAL_DATA_PATH = Path(__file__).with_name("manual_data.yaml")
PMI_RELEASE_URLS_PATH = Path(__file__).with_name("pmi_release_urls.yaml")
_ENTRY_KEYS = {
    "series_id",
    "observed_at",
    "value",
    "unit",
    "source_url",
    "entered_at",
}
_SOURCE_URL_PATTERNS = {
    "jp.pmi_manufacturing": re.compile(
        r"https://www\.pmi\.spglobal\.com/Public/Home/PressRelease/[0-9a-f]{32}\Z"
    ),
}


class ManualProvider:
    """File-backed provider for series with no clean free API."""

    name = "manual"

    def fetch(
        self,
        series: SeriesDefinition,
        *,
        start: date,
        end: date,
        session: HttpSession,
        context: FetchContext | None = None,
    ) -> list[ObservationRecord]:
        del session, context
        raw = strict_safe_load(MANUAL_DATA_PATH.read_text(encoding="utf-8"))
        return parse_manual_entries(series, raw, start=start, end=end)


def parse_manual_seed(
    definitions: IndicatorDefinitions,
    raw_seed: object,
) -> list[ObservationRecord]:
    """Validate the complete manual seed against the series registry."""

    by_id = definitions.by_id()
    manual_ids = {item.series_id for item in definitions.series if item.provider == "manual"}
    observations: list[ObservationRecord] = []
    seen_keys: set[tuple[str, date, datetime]] = set()
    for raw_entry in _seed_entries(raw_seed):
        entry = _entry_mapping(raw_entry)
        series_id = _entry_string(entry, "series_id")
        series = by_id.get(series_id)
        if series is None:
            raise IndicatorsProviderError(f"manual seed references unknown series {series_id}")
        if series.provider != "manual":
            raise IndicatorsProviderError(f"manual seed series is not manual: {series_id}")
        observation = _parse_entry(series, entry)
        key = (
            observation.series_id,
            observation.observed_at,
            cast(datetime, observation.vintage_at),
        )
        if key in seen_keys:
            raise IndicatorsProviderError(
                "manual seed has duplicate (series_id, observed_at, entered_at): "
                f"{series_id}, {observation.observed_at}, {observation.vintage_at}"
            )
        seen_keys.add(key)
        observations.append(observation)
    seeded_ids = {item.series_id for item in observations}
    if missing := sorted(manual_ids - seeded_ids):
        raise IndicatorsProviderError(f"manual seed is missing series: {', '.join(missing)}")
    _validate_pmi_history(observations)
    return observations


def parse_manual_entries(
    series: SeriesDefinition,
    raw_seed: object,
    *,
    start: date,
    end: date,
) -> list[ObservationRecord]:
    """Return one manual series from the validated seed date range."""

    observations: list[ObservationRecord] = []
    for raw_entry in _seed_entries(raw_seed):
        entry = _entry_mapping(raw_entry)
        if entry.get("series_id") == series.series_id:
            observations.append(_parse_entry(series, entry))
    if not observations:
        raise IndicatorsProviderError(f"manual data missing series {series.series_id}")
    return [item for item in observations if start <= item.observed_at <= end]


def _seed_entries(raw: object) -> list[object]:
    if not isinstance(raw, dict):
        raise IndicatorsProviderError("manual seed root must be a mapping")
    version = raw.get("schema_version")
    if version != 1:
        raise IndicatorsProviderError("manual seed schema_version must be 1")
    entries = raw.get("observations")
    if not isinstance(entries, list):
        raise IndicatorsProviderError("manual seed observations must be a list")
    return cast(list[object], entries)


def _parse_entry(
    series: SeriesDefinition,
    entry: Mapping[str, object],
) -> ObservationRecord:
    unexpected = sorted(set(entry) - _ENTRY_KEYS)
    missing = sorted(_ENTRY_KEYS - set(entry))
    if unexpected or missing:
        raise IndicatorsProviderError(
            f"manual seed fields differ for {series.series_id}: "
            f"missing={missing}, unexpected={unexpected}"
        )
    series_id = _entry_string(entry, "series_id")
    if series_id != series.series_id:
        raise IndicatorsProviderError(
            f"manual seed series mismatch: expected {series.series_id}, got {series_id}"
        )
    observed_at = _entry_date(entry.get("observed_at"), label="observed_at")
    entered_at = _entry_datetime(entry.get("entered_at"))
    unit = _entry_string(entry, "unit")
    if unit != series.unit:
        raise IndicatorsProviderError(
            f"manual seed unit differs from series.yaml for {series_id}: {unit} != {series.unit}"
        )
    source_url = _entry_string(entry, "source_url")
    source_pattern = _SOURCE_URL_PATTERNS.get(series_id)
    source_matches = (
        source_pattern.fullmatch(source_url) is not None
        if source_pattern is not None
        else source_url == series.source_url
    )
    if not source_matches:
        raise IndicatorsProviderError(
            "manual seed source_url differs from series.yaml for "
            f"{series_id}: {source_url} != {series.source_url}"
        )
    value = _entry_value(entry.get("value"))
    if series_id == "jp.pmi_manufacturing":
        if observed_at.day != 1:
            raise IndicatorsProviderError("manual PMI observed_at must be the first of a month")
        expected_url = _pmi_release_urls().get(observed_at)
        if expected_url is None or source_url != expected_url:
            raise IndicatorsProviderError(
                f"manual PMI source does not match manifest for {observed_at}"
            )
        if not 30 <= value <= 70:
            raise IndicatorsProviderError(
                f"manual PMI value outside plausible range for {observed_at}: {value}"
            )
    return ObservationRecord(
        series_id=series_id,
        observed_at=observed_at,
        period_start=observed_at,
        period_end=observed_at,
        value=value,
        unit=unit,
        vintage_at=entered_at,
        fetch_status="ok",
        source_url=source_url,
    )


def _entry_mapping(raw: object) -> Mapping[str, object]:
    if not isinstance(raw, dict):
        raise IndicatorsProviderError("manual seed entry must be a mapping")
    return cast(Mapping[str, object], raw)


def _entry_string(entry: Mapping[str, object], key: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise IndicatorsProviderError(f"manual seed field {key!r} must be a non-empty string")
    return value


def _entry_date(value: object, *, label: str) -> date:
    if isinstance(value, datetime):
        raise IndicatorsProviderError(f"manual seed {label!r} must be a date")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise IndicatorsProviderError(
                f"manual seed {label!r} must be an ISO date: {value!r}"
            ) from error
    raise IndicatorsProviderError(f"manual seed {label!r} must be a date: {value!r}")


def _entry_datetime(value: object) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise IndicatorsProviderError(
                f"manual seed 'entered_at' must be an ISO datetime: {value!r}"
            ) from error
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise IndicatorsProviderError(f"manual seed 'entered_at' must be a datetime: {value!r}")
    if parsed.tzinfo is None:
        raise IndicatorsProviderError("manual seed 'entered_at' must include a timezone")
    return parsed.astimezone(UTC)


def _entry_value(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise IndicatorsProviderError(f"manual seed 'value' must be numeric: {value!r}")
    try:
        parsed = float(value)
    except OverflowError as error:
        raise IndicatorsProviderError("manual seed 'value' must be finite") from error
    if not math.isfinite(parsed):
        raise IndicatorsProviderError(f"manual seed 'value' must be finite: {value!r}")
    return parsed


@cache
def _pmi_release_urls() -> dict[date, str]:
    raw = strict_safe_load(PMI_RELEASE_URLS_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise IndicatorsProviderError("PMI release manifest must use schema_version 1")
    releases = raw.get("releases")
    if not isinstance(releases, list):
        raise IndicatorsProviderError("PMI release manifest releases must be a list")
    result: dict[date, str] = {}
    for raw_release in releases:
        release = _entry_mapping(raw_release)
        observed_at = _entry_date(release.get("observed_at"), label="observed_at")
        source_url = _entry_string(release, "url")
        if observed_at in result:
            raise IndicatorsProviderError(
                f"PMI release manifest has duplicate month: {observed_at}"
            )
        if _SOURCE_URL_PATTERNS["jp.pmi_manufacturing"].fullmatch(source_url) is None:
            raise IndicatorsProviderError(
                f"PMI release manifest has invalid source URL for {observed_at}"
            )
        result[observed_at] = source_url
    ordered_dates = sorted(result)
    if len(ordered_dates) < 36:
        raise IndicatorsProviderError("PMI release manifest must cover at least 36 months")
    for previous, current in pairwise(ordered_dates):
        expected = (
            date(previous.year + 1, 1, 1)
            if previous.month == 12
            else date(previous.year, previous.month + 1, 1)
        )
        if current != expected:
            raise IndicatorsProviderError(
                f"PMI release manifest has a gap between {previous} and {current}"
            )
    return result


def _validate_pmi_history(observations: list[ObservationRecord]) -> None:
    expected_dates = set(_pmi_release_urls())
    actual_dates = {
        item.observed_at for item in observations if item.series_id == "jp.pmi_manufacturing"
    }
    if actual_dates != expected_dates:
        missing = sorted(expected_dates - actual_dates)
        unexpected = sorted(actual_dates - expected_dates)
        raise IndicatorsProviderError(
            f"manual PMI history differs from manifest: missing={missing}, unexpected={unexpected}"
        )
