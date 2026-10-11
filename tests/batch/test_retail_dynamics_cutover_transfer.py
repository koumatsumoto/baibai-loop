from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

from tests.batch.test_cloud_r2_transfer import (
    _environment,
    _fake_aws,
    _fake_repo,
    _transfer_commands,
)
from tools.cutover.market_28_to_29 import migrate_market_28_to_29

from baibai_engine.market.retail_dynamics.contract import (
    ANALYST_RATING_TABLE,
    MODEL_VALUATION_TABLE,
)
from baibai_engine.market.sqlite import open_connection


def test_cutover_bundle_publishes_schema29_with_pulled_cas_without_cross_schema_merge(
    tmp_path: Path,
) -> None:
    root = _fake_repo(tmp_path)
    source = tmp_path / "market28.sqlite"
    open_connection(source).close()
    with sqlite3.connect(source) as connection:
        connection.execute(f"DROP TABLE {MODEL_VALUATION_TABLE}")
        connection.execute(f"DROP TABLE {ANALYST_RATING_TABLE}")
        connection.execute("PRAGMA user_version=28")
        connection.executemany(
            "INSERT INTO jquants_daily_bars(ticker,traded_at,close) VALUES (?,?,?)",
            [("7203", "2026-09-30", 100), ("7203", "2026-10-01", 101)],
        )
    market = root / "stores/market/market.sqlite"
    market.parent.mkdir(parents=True)
    result = migrate_market_28_to_29(source, market)
    assert result.all_old_rows_equal is True
    for relative in ("stores/screening/runs.sqlite", "stores/macro/macro.sqlite"):
        path = root / relative
        path.parent.mkdir(parents=True)
        sqlite3.connect(path).close()
    bin_dir, log = _fake_aws(tmp_path)
    environment = _environment(bin_dir, log)
    environment["SNAPSHOT_FAKE_COPY"] = "1"
    environment["AWS_FAKE_EXISTING_KEY"] = "lake/pointers/l1/current.json"
    completed = subprocess.run(
        [root / "batch/scripts/r2_transfer.sh", "push-machine"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    commands = _transfer_commands(log)
    assert not any(command.startswith("merge ") for command in commands)
    assert any(command.startswith("dehydrate ") for command in commands)
    assert not any(command.startswith("publish ") for command in commands)
    uploads = [command for command in commands if command.startswith("s3api put-object ")]
    assert len(uploads) == 4
    assert all('--if-match "etag-stable"' in command for command in uploads[:3])
    assert "--key machine-manifest.json" in uploads[-1]
    uploaded = Path(environment["AWS_FAKE_STATE"]) / "put-market.sqlite"
    with sqlite3.connect(uploaded) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 29
        assert connection.execute("SELECT COUNT(*) FROM jquants_daily_bars").fetchone()[0] == 2
    with sqlite3.connect(source) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 28
