from __future__ import annotations

from dataclasses import dataclass, field

from tools.diagnostics.provision_duckdb_httpfs import provision_httpfs


@dataclass
class RecordingConnection:
    statements: list[str] = field(default_factory=list)
    closed: bool = False

    def execute(self, statement: str) -> RecordingConnection:
        self.statements.append(statement)
        return self

    def close(self) -> None:
        self.closed = True


def test_provision_installs_then_smokes_a_fresh_download_disabled_connection() -> None:
    connections: list[RecordingConnection] = []

    def connect(_database: str) -> RecordingConnection:
        connection = RecordingConnection()
        connections.append(connection)
        return connection

    version = provision_httpfs(connect=connect)  # type: ignore[arg-type]

    assert version
    assert connections[0].statements == ["INSTALL httpfs"]
    assert connections[1].statements == [
        "SET autoinstall_known_extensions = false",
        "SET autoload_known_extensions = false",
        "LOAD httpfs",
    ]
    assert all(connection.closed for connection in connections)
