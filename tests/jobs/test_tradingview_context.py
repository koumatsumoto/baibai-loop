"""Historical receipt checks have no live clock, provider, or acquisition plan."""

from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest
from tests.helpers.master import insert_master_rows
from tests.helpers.tradingview import DAY, NOW, seed_snapshot, store

from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.maintenance.tradingview_context import snapshot_state, universe, validate_time
from baibai_jobs.observability.tradingview import SourceDataError, TimeGuardError


def test_saved_completeness_preserves_partial_quality_and_receive_time(tmp_path):
    path = store(tmp_path / "market.sqlite", 3)
    seed_snapshot(path, ["TSE:1000", "TSE:1001"], missing=("TSE:1001",))
    before = path.read_bytes()
    state = snapshot_state(path, DAY)
    assert (state["rows"], state["remaining"], state["unresolved"]) == (2, 1, 1)
    assert state["normal_nulls"]["price_target_average"] == 1
    assert state["first_fetched_at_utc"] == NOW.isoformat()
    assert state["last_fetched_at_utc"] == NOW.isoformat()
    assert "needs_fetch" not in state
    assert path.read_bytes() == before


def test_saved_rows_outside_the_recorded_universe_are_not_accepted(tmp_path):
    path = store(tmp_path / "market.sqlite", 1)
    seed_snapshot(path, ["TSE:1000"])
    with closing(open_connection(path)) as conn, conn:
        conn.execute("UPDATE tradingview_forecast_snapshots SET ticker='9999'")
    with pytest.raises(SourceDataError, match="universe"):
        snapshot_state(path, DAY)


def test_original_receipt_clock_is_checked_not_replaced_with_today():
    validate_time(DAY, NOW)
    with pytest.raises(TimeGuardError):
        validate_time(DAY, NOW + timedelta(days=1))
    with pytest.raises(TimeGuardError):
        validate_time(DAY, NOW - timedelta(hours=1))
    with pytest.raises(ValueError, match="Timezone-aware"):
        validate_time(DAY, NOW.replace(tzinfo=None))


def test_universe_excludes_secondary_instruments_without_bar_history(tmp_path: Path) -> None:
    path = store(tmp_path / "market.sqlite", 1)
    conn = open_connection(path)
    with conn:
        insert_master_rows(
            conn,
            [
                (DAY.isoformat(), "2000", "fund", "プライム", "その他", "014"),
                (DAY.isoformat(), "2001", "pro", "TOKYO PRO MARKET", "機械", "011"),
                (DAY.isoformat(), "2002", "Nagoya", "名証", "機械", "011"),
            ],
        )
    assert universe(conn, DAY) == ["TSE:1000"]
    with conn:
        conn.execute(
            "UPDATE jquants_master_snapshots SET record_json=json_set(record_json,'$.ProdCat',NULL) WHERE security_code='10000'"
        )
    with pytest.raises(JQuantsProviderError, match="product category"):
        universe(conn, DAY)
    conn.close()


def test_native_product_category_does_not_depend_on_sector_spelling(tmp_path):
    path = store(tmp_path / "market.sqlite", 1)
    with closing(open_connection(path)) as conn, conn:
        conn.execute(
            "UPDATE jquants_master_snapshots "
            "SET record_json=json_set(record_json,'$.S33Nm','新業種')"
        )
        assert universe(conn, DAY) == ["TSE:1000"]
