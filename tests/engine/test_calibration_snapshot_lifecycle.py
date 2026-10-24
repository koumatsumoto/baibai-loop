"""Read-only access and interrupted publication preserve the current snapshot."""

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import date

import pytest
from tests.helpers.calibration_store import panel_diagnostics

from baibai_engine.screening.calibration import store
from baibai_engine.screening.calibration.forward import ForwardObservationPolicy


def _snapshot(root):
    day = date(2025, 6, 30)
    store.write_panel(root, day, (), panel_diagnostics(day.isoformat(), rules_hash="test"))
    store.write_forward(root, day, [])
    return root / store.CURRENT_SNAPSHOT_NAME


def test_read_connections_cannot_write_and_missing_reads_create_nothing(tmp_path):
    root = tmp_path / "missing"
    with pytest.raises(store.CalibrationCacheError, match="missing"):
        store.read_panel(root, date(2025, 6, 30))
    assert not root.exists()
    path = _snapshot(root)
    before = path.read_bytes()
    with (
        closing(store._connect(root)) as connection,
        pytest.raises(sqlite3.OperationalError, match="readonly"),
    ):
        connection.execute("DELETE FROM cohort")
    assert path.read_bytes() == before


def test_failed_schema_open_closes_the_connection(tmp_path, monkeypatch):
    path = _snapshot(tmp_path)
    original = sqlite3.connect
    opened = []

    class Connection(sqlite3.Connection):
        closed = False

        def execute(self, sql, *args):
            if "sqlite_schema" in sql:
                raise sqlite3.OperationalError("synthetic metadata read failure")
            return super().execute(sql, *args)

        def close(self):
            self.closed = True
            return super().close()

    def connect(*args, **kwargs):
        connection = original(*args, **kwargs, factory=Connection)
        opened.append(connection)
        return connection

    monkeypatch.setattr(store.sqlite3, "connect", connect)
    with pytest.raises(sqlite3.OperationalError, match="synthetic metadata"):
        store._connect(path.parent)
    assert len(opened) == 1
    assert opened[0].closed


@pytest.mark.parametrize("error", [OSError("disk full"), KeyboardInterrupt()])
def test_failed_publication_removes_temporary_copy_without_replacing_current(
    tmp_path, monkeypatch, error
):
    root, built = tmp_path / "current", tmp_path / "built"
    current = _snapshot(root)
    _snapshot(built)
    before = current.read_bytes()

    def fail_copy(source, target):
        target.write_bytes(b"partial copy")
        raise error

    monkeypatch.setattr(store.shutil, "copy2", fail_copy)
    with pytest.raises(type(error)):
        store.publish_current_snapshot(root, built)
    assert current.read_bytes() == before
    assert list(root.glob(".current.sqlite.*.tmp")) == []


@pytest.mark.parametrize("flag", ["use_control_event_exits", "use_failure_exits"])
def test_incremental_cohort_cannot_change_forward_policy(tmp_path, flag):
    root, built = tmp_path / "current", tmp_path / "built"
    current = _snapshot(root)
    before = current.read_bytes()
    store.copy_current_snapshot(root, built)
    second = date(2025, 7, 31)
    with pytest.raises(store.CalibrationCacheError, match="cannot mix measurement policies"):
        store.write_panel(
            built,
            second,
            (),
            panel_diagnostics(second.isoformat(), rules_hash="test"),
            forward_policy=ForwardObservationPolicy(**{flag: False}),
        )
    assert store.published_cohorts(built) == [date(2025, 6, 30)]
    assert current.read_bytes() == before


@pytest.mark.parametrize("flag", ["use_control_event_exits", "use_failure_exits"])
def test_mixed_forward_policies_cannot_publish_or_enter_evaluation(tmp_path, flag):
    root, built = tmp_path / "current", tmp_path / "built"
    current = _snapshot(root)
    before = current.read_bytes()
    store.copy_current_snapshot(root, built)
    second = date(2025, 7, 31)
    store.write_panel(built, second, (), panel_diagnostics(second.isoformat(), rules_hash="test"))
    store.write_forward(built, second, [])
    with closing(sqlite3.connect(built / store.CURRENT_SNAPSHOT_NAME)) as connection, connection:
        connection.execute(
            "UPDATE cohort SET forward_policy = ? WHERE asof = ?",
            (json.dumps(asdict(ForwardObservationPolicy(**{flag: False}))), second.isoformat()),
        )
    with pytest.raises(store.CalibrationCacheError, match="mixes measurement"):
        store.publish_current_snapshot(root, built)
    with (
        pytest.raises(store.CalibrationCacheError, match="mixes measurement"),
        store.fixed_current_snapshot(built),
    ):
        pytest.fail("mixed measurements reached evaluation")
    assert current.read_bytes() == before
