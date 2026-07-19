"""Local read-only HTTP API for the cockpit."""

from .server import create_app

__all__ = ["create_app"]
