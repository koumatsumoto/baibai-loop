"""Application database infrastructure.

This package owns writable SQLite connections and current-schema initialization.
Domain packages own mutations on connections that admit only the current schema.
"""
