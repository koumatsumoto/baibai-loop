"""Application database infrastructure.

This package owns writable SQLite connections and current-schema initialization.
Domain packages expose all business mutations; older stores require an explicit cutover.
"""

from baibai_engine.storage.sqlite.application.schema import (
    APPLICATION_SCHEMA_VERSION as LATEST_VERSION,
)

__all__ = ["LATEST_VERSION"]
