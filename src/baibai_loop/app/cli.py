"""CLI for serving the local read-only cockpit."""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

import uvicorn

from baibai_loop.app.api.server import create_app

_HOST = "127.0.0.1"
_DEFAULT_PORT = 8712


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-loop-app")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="serve the local read-only cockpit")
    serve.add_argument("--root", type=Path, default=Path.cwd())
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
    url = f"http://{_HOST}:{args.port}"
    if args.open_browser:
        webbrowser.open(url)
    uvicorn.run(create_app(root), host=_HOST, port=args.port)
    return 0


def _root_error(root: Path) -> str | None:
    if not (root / "pyproject.toml").is_file():
        return f"--root does not contain pyproject.toml: {root}"
    if not (root / "records").is_dir():
        return f"--root does not contain records/: {root}"
    return None


if __name__ == "__main__":
    raise SystemExit(main())
