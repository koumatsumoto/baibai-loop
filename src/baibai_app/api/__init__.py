"""Local read-only HTTP API for the Baibai Loop UI."""

from .server import create_app

__all__ = ["create_app"]
