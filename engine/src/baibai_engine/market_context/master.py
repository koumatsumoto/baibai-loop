"""共通L1の最新master原本から、主銘柄lineと商品区分を投影する。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from baibai_engine.foundation.sqlite_read import read_rows
from baibai_engine.market.master_snapshot import masters_from_payloads


@dataclass(frozen=True, slots=True)
class MasterSnapshot:
    as_of: date
    securities: dict[str, dict[str, object]]


def read_master_snapshot(path: Path, *, as_of: date) -> MasterSnapshot | None:
    rows = read_rows(
        path,
        "SELECT snapshot_date, record_json "
        "FROM jquants_master_snapshots WHERE snapshot_date = ("
        "SELECT MAX(snapshot_date) FROM jquants_master_snapshots "
        "WHERE snapshot_date <= ?) ORDER BY security_code",
        (as_of.isoformat(),),
    )
    if not rows:
        return None
    return MasterSnapshot(
        date.fromisoformat(str(rows[0]["snapshot_date"])),
        {
            security.ticker: {
                "name": security.name,
                "market_segment": security.market_segment,
                "sector_33": security.sector_33,
                "is_common_stock": security.is_common_stock,
                "snapshot_date": str(rows[0]["snapshot_date"]),
            }
            for security in masters_from_payloads([row["record_json"] for row in rows])
        },
    )
