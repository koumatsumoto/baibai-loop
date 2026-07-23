"""Local read-only HTTP API for the Baibai App."""

from .server import create_app

__all__ = ["create_app"]
