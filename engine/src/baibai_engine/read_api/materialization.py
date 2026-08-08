"""Read-only preconditions used before publishing Web projections."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from baibai_engine.appdb.schema import APPLICATION_SCHEMA_VERSION
from baibai_engine.macro.indicators.definitions import load_definitions
from baibai_engine.macro.reading.rules import ReadingRulesError, load_reading_rules


class MaterializationPreconditionError(RuntimeError):
    """A source cannot produce a complete, current Web projection."""


def validate_application_store_schema(path: Path) -> None:
    """Require a present application store to use the schema this code reads."""

    if not path.is_file():
        return
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != APPLICATION_SCHEMA_VERSION:
        raise MaterializationPreconditionError(
            f"application store schema is {version} but this code expects "
            f"{APPLICATION_SCHEMA_VERSION}: {path} "
            "(publish a store migrated by the matching application release)"
        )


def validate_macro_reading_rules(path: Path) -> None:
    """Require the trusted reading rules to resolve every registered series."""

    try:
        rules = load_reading_rules(path)
        for definition in load_definitions().series:
            rules.resolve(series_id=definition.series_id, frequency=definition.frequency)
    except ReadingRulesError as error:
        raise MaterializationPreconditionError(str(error)) from error


__all__ = [
    "MaterializationPreconditionError",
    "validate_application_store_schema",
    "validate_macro_reading_rules",
]
