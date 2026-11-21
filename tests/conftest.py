from __future__ import annotations

import socket
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

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
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*args: object, **kwargs: object) -> None:
        pytest.fail("unit test attempted real network access; mock the provider transport")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


@pytest.fixture(autouse=True)
def _isolate_databases(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The owners create this private directory on the first write. Pure tests
    # need neither a pytest tmp_path nor an empty directory for the two defaults.
    if request.config.getini("tmp_path_retention_policy") == "all":
        db_root = tmp_path_factory.getbasetemp() / f"db-input-{uuid4().hex}"
    else:
        # Let pytest own per-case cleanup for an explicitly shorter retention policy.
        db_root = request.getfixturevalue("tmp_path") / "isolated-db"
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
