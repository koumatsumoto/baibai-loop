"""Small shared helpers for the screening CLI commands."""

from __future__ import annotations

from datetime import date

import yaml


class _NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: object) -> bool:
        return True


def _parse_iso_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise SystemExit(f"invalid ISO date: {raw}") from exc


def _parse_profiles_arg(raw: str) -> tuple[str, ...]:
    return tuple(profile for item in raw.split(",") if (profile := item.strip()))


def _date_iso(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None
