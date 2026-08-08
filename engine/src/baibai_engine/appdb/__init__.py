"""Application database infrastructure.

This package owns writable SQLite connections and forward-only schema
migrations.  Domain packages expose all business mutations.
"""

from .migrations import LATEST_VERSION
from .write import (
    DEFAULT_DB_PATH,
    backup_database,
    connect_rw,
    database_path,
    initialize_database,
)

__all__ = [
    "DEFAULT_DB_PATH",
    "LATEST_VERSION",
    "backup_database",
    "connect_rw",
    "database_path",
    "initialize_database",
]
