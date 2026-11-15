from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.helpers.network import _no_real_network as _no_real_network

# Redirect App/run defaults through env; explicit paths and shell transfers can bypass
# that redirect. `r2_transfer.sh` resolves store paths from its own checkout, so the
# session fingerprint below also watches all canonical DB paths. It detects common
# mutations after the test run; it neither prevents I/O nor proves no store was read.
_REAL_DB_DEFAULTS = (
    Path("stores/application/baibai.sqlite"),
    Path("stores/screening/runs.sqlite"),
    Path("stores/market_context/indicators.sqlite"),
    Path("stores/market/market.sqlite"),
)


@pytest.fixture(autouse=True)
def _isolate_databases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point env-resolved DB paths at a per-test tmp location.

    A test that omits an explicit path resolves the default operational DB. Forcing
    ``BAIBAI_DB`` / ``BAIBAI_RUNS_DB`` to tmp guarantees such a test opens an isolated
    file instead. Explicit path arguments still take precedence, so fixtures that pass
    tmp paths are unaffected.
    """
    db_root = tmp_path / "isolated-db"
    db_root.mkdir()
    monkeypatch.setenv("BAIBAI_DB", str(db_root / "app.sqlite"))
    monkeypatch.setenv("BAIBAI_RUNS_DB", str(db_root / "runs.sqlite"))


def _database_fingerprint(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_size, stat.st_mtime_ns)


@pytest.fixture(scope="session", autouse=True)
def _guard_real_databases() -> Iterator[None]:
    """Detect size/mtime changes to the default operational database files.

    This catches common isolation gaps outside the env redirect, but does not
    prevent writes or prove that every possible mutation was detected.
    """
    before = {path: _database_fingerprint(path) for path in _REAL_DB_DEFAULTS}
    yield
    mutated = [
        str(path) for path in _REAL_DB_DEFAULTS if _database_fingerprint(path) != before[path]
    ]
    if mutated:
        raise AssertionError("tests mutated real operational database(s): " + ", ".join(mutated))
