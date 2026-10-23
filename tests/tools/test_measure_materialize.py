from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest
from tools.diagnostics.measure_materialize import Probe, deny_external_io, output_inventory

from baibai_engine.market_context import security


def test_digest_keeps_clocks_and_paths_but_ignores_object_key_order(tmp_path: Path) -> None:
    path = tmp_path / "view.json"
    path.write_text('{"generated_at":"2026-10-20","value":1}', encoding="utf-8")
    first = output_inventory(tmp_path)
    path.write_text('{"value":1,"generated_at":"2026-10-20"}', encoding="utf-8")
    reordered = output_inventory(tmp_path)
    assert first["semantic_json_tree_sha256"] == reordered["semantic_json_tree_sha256"]
    assert first["raw_tree_sha256"] != reordered["raw_tree_sha256"]
    path.write_text('{"value":1,"generated_at":"2026-10-21"}', encoding="utf-8")
    changed = output_inventory(tmp_path)
    assert changed["semantic_json_tree_sha256"] != first["semantic_json_tree_sha256"]
    path.rename(tmp_path / "renamed.json")
    assert output_inventory(tmp_path)["semantic_json_tree_sha256"] != changed[
        "semantic_json_tree_sha256"
    ]


def test_probe_counts_peers_and_restores_functions_after_failure() -> None:
    original_connect = sqlite3.connect
    original_endpoint = security._endpoint_return
    original_median = security.statistics.median
    probe = Probe()
    endpoints = (date(2026, 10, 1), date(2026, 10, 20))

    def sector_work() -> None:
        for _ in range(3):  # 2 peers + own
            assert security._endpoint_return(dict.fromkeys(endpoints, 1.0), endpoints) == 0
        assert security.statistics.median([0.0, 0.0]) == 0
        with closing(sqlite3.connect(":memory:")):
            pass
        raise RuntimeError("intentional")

    with pytest.raises(RuntimeError, match="intentional"), probe.install():
        probe.wrap("security.sector", sector_work)()
    assert probe.report()["peer_return_attempts"] == 2
    assert probe.report()["sector_medians"] == 1
    assert probe.connections["security.sector::memory:"] == 1
    assert probe.stages["security.sector"]["calls"] == 1
    assert sqlite3.connect is original_connect
    assert security._endpoint_return is original_endpoint
    assert security.statistics.median is original_median
    assert probe.stack == []


@pytest.mark.parametrize("event", ["socket.connect", "socket.getaddrinfo", "subprocess.Popen"])
def test_external_io_guard_fails_closed(event: str) -> None:
    with pytest.raises(RuntimeError, match="forbids external I/O"):
        deny_external_io(event, ())
