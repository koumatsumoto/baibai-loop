"""Install DuckDB httpfs during deployment, then prove offline-style loading works."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable

import duckdb


class HttpfsProvisionError(RuntimeError):
    """The pinned DuckDB runtime cannot install and load its httpfs extension."""


def provision_httpfs(
    connect: Callable[[str], duckdb.DuckDBPyConnection] = duckdb.connect,
) -> str:
    """Install for this DuckDB version, then LOAD from a fresh locked-down session."""
    installer = connect(":memory:")
    try:
        installer.execute("INSTALL httpfs")
    except duckdb.Error as exc:
        raise HttpfsProvisionError(f"DuckDB httpfs installation failed: {exc}") from None
    finally:
        installer.close()

    smoke = connect(":memory:")
    try:
        smoke.execute("SET autoinstall_known_extensions = false")
        smoke.execute("SET autoload_known_extensions = false")
        smoke.execute("LOAD httpfs")
    except duckdb.Error as exc:
        raise HttpfsProvisionError(
            f"DuckDB httpfs is not loadable from the installed extension cache: {exc}"
        ) from None
    finally:
        smoke.close()
    return duckdb.__version__


def main() -> int:
    try:
        version = provision_httpfs()
    except HttpfsProvisionError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"duckdb_version": version, "httpfs": "installed_and_loadable"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
