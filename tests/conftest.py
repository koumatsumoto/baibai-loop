from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

# Real operational stores that a test must never open. App DB and run store resolve
# their default from these env vars; the indicators and market stores have no env
# override and are guarded by fingerprint detection alone. The market store is the one
# a shell test can reach without opening a database at all: `r2_transfer.sh` resolves
# every store path from its own location, so a pull run from the checkout replaces all
# four files whatever the fake CLI hands back.
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
