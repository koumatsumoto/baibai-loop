"""Application database infrastructure.

This package owns writable SQLite connections and current-schema initialization.
Domain packages own mutations; the temporary 29/30 bridge does not migrate a store.
"""

from baibai_engine.storage.sqlite.application.schema import (
    APPLICATION_SCHEMA_VERSION as LATEST_VERSION,
)

__all__ = ["LATEST_VERSION"]
