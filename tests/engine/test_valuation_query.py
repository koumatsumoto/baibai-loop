"""保存済み機械推計の対象集合・cutoff・副作用と、Valuation の依存境界。"""

import hashlib
import socket
import sqlite3
import subprocess
import sys
from datetime import date

import pytest
from tests.helpers.screening_run import screening_run_payload, security_analysis

from baibai_engine.read_api import stored_machine_valuation
from baibai_engine.read_api.ticker_profile import _load_security_analysis_context
from baibai_engine.screening.run_store import ScreeningRunStore
from baibai_engine.valuation.query import anchor_gap_pct, machine_valuation_from_analysis

CUTOFF = date(2026, 9, 19)


def publish(path, day, revision, *, price=1000, anchor=1500, run_at=None):
    ScreeningRunStore(path).publish_run(
        screening_run_payload(
            as_of=day,
            run_at=run_at,
            security_analyses=[
                security_analysis(
                    "1234",
                    metrics={
                        "market_price_yen": price,
                        "fv_sector_median_yen": anchor,
                        "fv_self_range_yen": 1300,
                        "er_annual": 0.075,
                        "er_reversion_annual": 0.025,
                        "er_carry_annual": 0.05,
                        "er_model_version": "expected-return-v1",
                        "er_unit": "annual_ratio",
                    },
                )
            ],
        ),
        run_revision_id=revision,
    )


def test_saved_non_candidate_and_profile_use_latest_run_before_cutoff(tmp_path):
    path = tmp_path / "runs.sqlite"
    publish(path, "2026-09-18", "old")
    publish(path, "2026-09-19", "early", anchor=1400, run_at="2026-09-19T09:00:00+09:00")
    publish(path, "2026-09-19", "last", anchor=1600)
    publish(path, "2026-09-20", "future", anchor=2000)
    # No Review Set exists; membership cannot authorize or suppress this read.
    result = stored_machine_valuation(path, ticker="1234", as_of=CUTOFF)
    assert result.run_revision_id == "last"
    assert result.as_of == CUTOFF
    assert result.valuation.sector_median_anchor_yen == 1600
    assert result.valuation.er_annual == 0.075
    assert anchor_gap_pct(result.valuation) == 60
    context = _load_security_analysis_context(path, "1234", CUTOFF, run_revision_id=None)
    assert context["screening_run_revision_id"] == result.run_revision_id
    assert context["security_analysis"]["metrics"]["fv_sector_median_yen"] == 1600
    assert stored_machine_valuation(path, ticker="1234", as_of=date(2026, 9, 17)) is None


@pytest.mark.parametrize("entry", [stored_machine_valuation, _load_security_analysis_context])
def test_fixed_run_checks_cutoff_and_identity(tmp_path, entry):
    path = tmp_path / "runs.sqlite"
    publish(path, "2026-09-19", "fixed")
    publish(path, "2026-09-20", "future")

    def read(revision):
        if entry is stored_machine_valuation:
            return entry(path, ticker="1234", as_of=CUTOFF, run_revision_id=revision)
        return entry(path, "1234", CUTOFF, run_revision_id=revision)

    assert read("fixed") is not None
    with pytest.raises(ValueError, match="after cutoff"):
        read("future")
    with pytest.raises(ValueError, match="unknown run_revision_id"):
        read("absent")


def test_missing_store_scope_and_coordinates_are_distinct(tmp_path):
    path = tmp_path / "runs.sqlite"
    assert stored_machine_valuation(path, ticker="1234", as_of=CUTOFF) is None
    assert not path.exists()
    sqlite3.connect(path).close()
    assert stored_machine_valuation(path, ticker="1234", as_of=CUTOFF) is None
    publish(path, "2026-09-19", "run")
    result = stored_machine_valuation(path, ticker="9999", as_of=CUTOFF)
    assert result.run_revision_id == "run"
    assert result.valuation is None
    publish(path, "2026-09-20", "missing-anchor", anchor=None)
    result = stored_machine_valuation(path, ticker="1234", as_of=date(2026, 9, 20))
    assert result.valuation.sector_median_anchor_yen is None
    assert result.valuation.er_annual == 0.075
    assert anchor_gap_pct(result.valuation) is None
    for raw in ({}, {"metrics": {}}, {"metrics": None}):
        assert machine_valuation_from_analysis(raw).sector_median_anchor_yen is None
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE security_analysis")
    connection.close()
    with pytest.raises(sqlite3.OperationalError):
        stored_machine_valuation(path, ticker="1234", as_of=CUTOFF)


def test_read_has_no_network_or_store_writes(tmp_path, monkeypatch):
    path = tmp_path / "runs.sqlite"
    publish(path, "2026-09-19", "run")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    original = sqlite3.connect
    connections = []

    def connect(database, *args, **kwargs):
        assert "mode=ro" in database
        connection = original(database, *args, **kwargs)
        connections.append(connection)

        # Assert that no INSERT/UPDATE/DELETE or schema mutation is even attempted.
        def authorize(action, _a, _b, _db, _trigger):
            assert action not in {
                sqlite3.SQLITE_INSERT,
                sqlite3.SQLITE_UPDATE,
                sqlite3.SQLITE_DELETE,
                sqlite3.SQLITE_CREATE_TABLE,
                sqlite3.SQLITE_DROP_TABLE,
            }
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection

    def forbidden(*_args, **_kwargs):
        pytest.fail("valuation read attempted network I/O")

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(socket, "socket", forbidden)
    assert stored_machine_valuation(path, ticker="1234", as_of=CUTOFF).valuation.er_annual == 0.075
    assert connections
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_valuation_import_does_not_start_research_or_aggregate_read_api():
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from baibai_engine.valuation.models import Valuation
from baibai_engine.valuation.estimates import estimate_expected_return
from baibai_engine.valuation.query import machine_valuation_from_analysis
assert not any(name.startswith(("baibai_engine.research", "baibai_engine.read_api", "baibai_engine.screening")) for name in sys.modules)
""",
        ],
        check=True,
    )
