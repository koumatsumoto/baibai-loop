"""Local read-only HTTP API served by `baibai-app`."""

from .server import create_app

__all__ = ["create_app"]
