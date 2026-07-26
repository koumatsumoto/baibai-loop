"""Compatibility export for the shared read-only application DB connection."""

from __future__ import annotations

from baibai_engine.appdb.read import connect_read_only as connect_read_only

__all__ = ["connect_read_only"]
