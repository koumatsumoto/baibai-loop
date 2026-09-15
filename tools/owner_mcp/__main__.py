"""Research 用の read-only owner tools を stdio で見せる。stdout は MCP 専用。"""

from __future__ import annotations

import os
import sys

from tools.l1_mcp.server import Adapter

from .server import create_server


def main() -> None:
    token = os.environ.pop("READ_ACCESS_TOKEN", "")
    if not token:
        print("READ_ACCESS_TOKEN is required", file=sys.stderr)
        raise SystemExit(1)
    adapter = Adapter(token)
    try:
        create_server(adapter).run(transport="stdio")
    finally:
        adapter.close()


if __name__ == "__main__":
    main()
