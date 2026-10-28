"""Keep native short-sale evidence without inferring a cancellation from free text."""

from datetime import date

import pyarrow.parquet as pq
import pytest

from baibai_engine.machine_analysis.observations.jquants_writer import (
    store_jquants_short_sale_reports,
)
from baibai_engine.machine_analysis.observations.trading_activity.margin_reader import (
    read_short_sale_reports,
)
from baibai_engine.storage.snapshots.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_jobs.maintenance.jquants_inputs import JQuantsReader


@pytest.mark.parametrize("missing", [False, True])
def test_native_short_fields_survive_provider_sqlite_and_lake(tmp_path, missing):
    day = date(2026, 10, 14)
    record = {
        "DiscDate": day.isoformat(),
        "CalcDate": "2026-10-13",
        "Code": "13010",
        "SSName": "Example Reporter",
        "DICName": "Example Contractor",
        "FundName": "Example Fund",
        "SSAddr": None if missing else "Example Seller Address",
        "DICAddr": None if missing else "Example Contractor Address",
        "ShrtPosToSO": None if missing else 0.006,
        "ShrtPosShares": None if missing else 600,
        "ShrtPosUnits": None if missing else 6,
        "PrevRptDate": None if missing else "2026-10-10",
        "PrevRptRatio": None if missing else 0.007,
        "Notes": "corrected report",
    }
    database = tmp_path / "market.sqlite"
    store_jquants_short_sale_reports(database, [record], requested_start=day, requested_end=day)
    stored = read_short_sale_reports(database, day, day)
    assert stored == JQuantsReader(database).get_mkt_short_sale_report_range(day, day)
    assert stored is not None
    assert len(stored) == 1
    assert stored[0].short_ratio == record["ShrtPosToSO"]
    assert stored[0].short_seller_address == record["SSAddr"]
    assert stored[0].discretionary_investment_contractor_address == record["DICAddr"]
    mirror = tmp_path / "lake"
    with sealed_sqlite_snapshot(sqlite_path=database, mirror_root=mirror) as snapshot:
        report = export_dataset_snapshot(
            dataset_name="jquants.short_sale_reports",
            mirror_root=mirror,
            source_snapshot=snapshot,
            producer_git_commit="a" * 40,
            build_id="short-source-values",
        )
    obj = report.manifest.partitions[0].objects[0]
    rows = pq.ParquetFile(mirror / obj.key).read().to_pylist()
    assert len(rows) == 1
    expected = {
        "disclosed_at": day.isoformat(),
        "source_ordinal": 0,
        "calculated_at": "2026-10-13",
        "ticker": "1301",
        "short_seller_name": record["SSName"],
        "discretionary_investment_contractor_name": record["DICName"],
        "investment_fund_name": record["FundName"],
        "short_seller_address": record["SSAddr"],
        "discretionary_investment_contractor_address": record["DICAddr"],
        "short_ratio": record["ShrtPosToSO"],
        "short_shares": record["ShrtPosShares"],
        "short_trading_units": record["ShrtPosUnits"],
        "previous_reported_at": record["PrevRptDate"],
        "previous_short_ratio": record["PrevRptRatio"],
        "notes": record["Notes"],
    }
    assert rows[0] == expected
