"""CLI for serving the local read-only UI."""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from pathlib import Path

import uvicorn

from baibai_engine.read_api import (
    APPLICATION_DB_PATH,
    RUNS_DB_PATH,
    StoreLayoutError,
    reject_noncanonical_store_paths,
    repository_root_error,
)
from baibai_web.api.server import create_app

_HOST = "127.0.0.1"
_DEFAULT_PORT = 8712


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-web")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="serve the local read-only UI")
    serve.add_argument("--root", type=Path, default=Path.cwd())
    serve.add_argument("--db", type=Path)
    serve.add_argument("--runs-db", type=Path)
    serve.add_argument("--port", type=int, default=_DEFAULT_PORT)
    serve.add_argument("--open", action="store_true", dest="open_browser")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "serve":  # pragma: no cover
        raise AssertionError(f"unreachable command: {args.command!r}")
    root = args.root.resolve()
    error = _root_error(root)
    if error is not None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    db_path = _resolved_store_path(root, args.db, "BAIBAI_DB", APPLICATION_DB_PATH)
    runs_db_path = _resolved_store_path(root, args.runs_db, "BAIBAI_RUNS_DB", RUNS_DB_PATH)
    try:
        reject_noncanonical_store_paths(root)
    except StoreLayoutError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    url = f"http://{_HOST}:{args.port}"
    if args.open_browser:
        webbrowser.open(url)
    uvicorn.run(
        create_app(root, db_path=db_path, runs_db_path=runs_db_path),
        host=_HOST,
        port=args.port,
    )
    return 0


def _root_error(root: Path) -> str | None:
    return repository_root_error(root, label="--root")


def _resolved_store_path(
    root: Path,
    explicit: Path | None,
    environment_name: str,
    default: Path,
) -> Path:
    configured = explicit or (Path(value) if (value := os.environ.get(environment_name)) else None)
    if configured is not None:
        return configured.expanduser().resolve()
    return (root / default).resolve()


if __name__ == "__main__":
    raise SystemExit(main())
