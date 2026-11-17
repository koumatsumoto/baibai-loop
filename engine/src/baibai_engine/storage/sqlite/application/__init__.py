"""Application database infrastructure.

This package owns writable SQLite connections and current-schema initialization.
Domain packages own mutations; the temporary 29/30 bridge does not migrate a store.
"""
